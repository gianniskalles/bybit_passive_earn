"""CARRY_PLAN Phase 4 — carry/execute.py: the two-leg procedure (R19-R24),
exits (decision 13.14), Earn orders, DRY_RUN refusal. Written before the
implementation. A FakeExchange stands in for Bybit; no network.

Acceptance (§7): every failure point is simulated; no scenario leaves an
orphan leg after LEG_TIMEOUT_S.
"""

from types import MappingProxyType

import pytest

from bybit_earn_tool import BybitAPIError
from carry import execute as ex
from carry.client import order_request
from carry.plan import Action, Plan, SymbolBook
from carry_builders import NOW, account, cfg, earn, market, short, snapshot
from fake_exchange import FakeClock, FakeExchange

T = 30          # LEG_TIMEOUT_S of the shipped config


def plan_of(*actions):
    return Plan(tuple(actions), (), MappingProxyType({}), MappingProxyType({}))


def enter(perp=0.03, spot=0.03004, symbol="ETHUSDT"):
    return Action("ENTER", symbol, "test entry", legs=("perp", "spot"), perp_qty=perp,
                  spot_qty=spot, usdt=spot * 2500.2)


OPEN = SymbolBook(status="OPEN", entered_ms=NOW - 400 * 3_600_000, perp_qty=0.03, spot_qty=0.03,
                  entry_times=(NOW - 400 * 3_600_000,))


def run(actions, exch, clock, book=None, state="NORMAL", snap=None, c=None):
    snap = snap or snapshot(acct=account(usdt=100.0))
    c = c or cfg()
    return ex.execute_plan(plan_of(*actions), snap, c, state, book or {}, "c1", exch,
                           clock=clock, sleep=clock.sleep)


@pytest.fixture
def clock():
    return FakeClock()


def hedged(x: FakeExchange, initial_spot=0.0):
    """No orphan: the short never exceeds the spot the system holds."""
    return x.short <= (x.spot - initial_spot) + 1e-9


# --- order requests ------------------------------------------------------------------- #

def test_order_requests():
    r = order_request("linear", "ETHUSDT", "Sell", "Limit", 0.03, 0.01, "cy-a", price=2500.0,
                      tick=0.01)
    assert r["path"] == "/v5/order/create"
    assert r["body"] == {"category": "linear", "symbol": "ETHUSDT", "side": "Sell",
                         "orderType": "Limit", "qty": "0.03", "orderLinkId": "cy-a",
                         "price": "2500", "timeInForce": "IOC", "positionIdx": 0}
    c = order_request("linear", "ETHUSDT", "Buy", "Market", 0.03, 0.01, "cy-b", reduce_only=True)
    assert c["body"]["reduceOnly"] is True and "price" not in c["body"]
    s = order_request("spot", "ETHUSDT", "Sell", "Market", 0.03, 0.00001, "cy-c")
    assert s["body"]["marketUnit"] == "baseCoin" and s["body"]["isLeverage"] == 0
    b = order_request("spot", "ETHUSDT", "Buy", "Limit", 0.03004, 0.00001, "cy-d", price=2500.2,
                      tick=0.01)
    assert b["body"]["isLeverage"] == 0 and b["body"]["qty"] == "0.03004"
    for bad in (dict(category="spot", reduce_only=True), dict(link="x-no-prefix"),
                dict(order_type="Limit", price=None)):
        kw = dict(category="linear", symbol="ETHUSDT", side="Buy", order_type="Market", qty=0.03,
                  qty_step=0.01, order_link_id=bad.pop("link", "cy-e"))
        kw.update(bad)
        with pytest.raises(ValueError):
            order_request(**kw)


def test_limit_price_walks_the_book():
    levels = [["100.0", "1"], ["99.5", "2"], ["99.0", "5"]]
    assert ex.limit_price(levels, 0.5) == 100.0
    assert ex.limit_price(levels, 2.5) == 99.5
    assert ex.limit_price(levels, 50) == 99.0              # not enough depth: last level (IOC)
    with pytest.raises(ValueError):
        ex.limit_price([], 1)


# --- entry: perp first, spot for the filled net quantity ------------------------------------ #

def test_entry_perp_first_then_spot_for_the_filled_quantity(clock):
    x = FakeExchange(clock)
    r = run([enter()], x, clock)
    first, second = x.created()
    assert (first["category"], first["side"], first["orderType"], first["timeInForce"]) == \
        ("linear", "Sell", "Limit", "IOC")
    assert first["qty"] == "0.03" and first["price"] == "2500"
    assert (second["category"], second["side"], second["timeInForce"]) == ("spot", "Buy", "IOC")
    assert second["qty"] == "0.03004"                       # 0.03 / (1 - 0.001), up to the step
    b = r.book_updates["ETHUSDT"]
    assert b.status == "OPEN" and b.perp_qty == pytest.approx(0.03)
    assert b.spot_qty == pytest.approx(0.03004 * 0.999) and b.spot_qty >= 0.03
    assert b.entered_ms == NOW and b.entry_times[-1] == NOW
    assert x.short == pytest.approx(0.03) and hedged(x) and r.escalate is None


def test_partial_fill_reconciles(clock):
    """§9: perp 0.8 of 1.0 -> spot for 0.8."""
    x = FakeExchange(clock).script("linear:Sell", ("partial", 0.8))
    r = run([enter(perp=1.0, spot=1.001)], x, clock)
    spot_buy = x.created("spot:Buy")[0]
    assert spot_buy["qty"] == "0.80081"                     # 0.8 / 0.999 up to the step
    b = r.book_updates["ETHUSDT"]
    assert b.perp_qty == pytest.approx(0.8) and b.spot_qty >= 0.8 and hedged(x)


def test_spot_fee_in_base_adjusts_hedge(clock):
    """§9 / R22: buy 1 BTC, receive 0.999 -> perp 0.999 (the fee the plan
    assumed was 0; the exchange took 0.1 % in the coin)."""
    m = market("BTCUSDT", price=2500.0)
    snap = snapshot(markets={"BTCUSDT": m}, positions={"BTCUSDT": short("BTCUSDT")},
                    acct=account(usdt=3000.0, spot_fee=0.0))
    x = FakeExchange(clock, base="BTC")
    r = run([enter(perp=1.0, spot=1.0, symbol="BTCUSDT")], x, clock, snap=snap,
            c=cfg(SYMBOLS=["BTCUSDT"]))
    assert x.created("spot:Buy")[0]["qty"] == "1"
    closes = x.created("linear:Buy")
    assert len(closes) == 1 and closes[0]["reduceOnly"] is True and closes[0]["qty"] == "0.001"
    b = r.book_updates["BTCUSDT"]
    assert b.perp_qty == pytest.approx(0.999) and b.spot_qty == pytest.approx(0.999)
    assert x.short == pytest.approx(0.999) and hedged(x)


def test_orphan_after_perp_fill_spot_fail(clock):
    """§9 / R19: perp filled, spot fails -> perp closed reduceOnly within
    LEG_TIMEOUT_S, ORPHAN_LEG, NO_NEW_POSITIONS."""
    x = FakeExchange(clock).script("spot:Buy", *["reject"] * 50)
    start = clock()
    r = run([enter()], x, clock)
    close = x.created("linear:Buy")
    assert close and close[0]["reduceOnly"] is True and close[0]["orderType"] == "Market"
    assert x.short == 0 and x.spot == 0
    assert any(a.startswith("ORPHAN_LEG") for a in r.alerts)
    assert r.escalate == "NO_NEW_POSITIONS"
    assert r.book_updates["ETHUSDT"].status == "FLAT"
    assert clock() - start <= 2 * T


def test_spot_that_never_fills_closes_the_perp_after_leg_timeout(clock):
    x = FakeExchange(clock, spot_ask=2500.2)
    x.books["spot"] = ([[2499.8, 100.0]], [[2500.2, 0.0]])    # nothing to buy
    x.script("spot:Buy", *[("partial", 0.0)] * 1000)
    start = clock()
    r = run([enter()], x, clock)
    assert x.short == 0 and r.escalate == "NO_NEW_POSITIONS"
    [close] = x.created("linear:Buy")
    issued = [c for c in x.calls if c[0] == "create" and c[1] == "linear:Buy"]
    assert issued and clock() - start <= 2 * T
    # the close was sent once the leg timed out, not before
    assert len(x.created("spot:Buy")) > 1


def test_timeout_queries_before_retry(clock):
    """§9 / R23: a timeout on send -> lookup by orderLinkId, never a new order."""
    x = FakeExchange(clock).script("linear:Sell", "timeout")
    r = run([enter()], x, clock)
    sells = x.created("linear:Sell")
    assert len(sells) == 1
    link = sells[0]["orderLinkId"]
    i_create = next(i for i, c in enumerate(x.calls) if c[0] == "create" and c[2] == link)
    assert any(c[0] == "find" and c[2] == link for c in x.calls[i_create + 1:])
    assert r.book_updates["ETHUSDT"].perp_qty == pytest.approx(0.03) and hedged(x)


def test_lost_order_is_looked_up_then_resent_with_the_same_link(clock):
    """R23/R24: not found after a timeout -> resend with the SAME orderLinkId
    (Bybit refuses a duplicate), never a new id."""
    x = FakeExchange(clock).script("linear:Sell", "lost")
    r = run([enter()], x, clock)
    sells = x.created("linear:Sell")
    assert len(sells) == 2 and sells[0]["orderLinkId"] == sells[1]["orderLinkId"]
    assert x.short == pytest.approx(0.03) and r.book_updates["ETHUSDT"].status == "OPEN"


def test_unknown_perp_outcome_is_closed_reduce_only(clock):
    """The perp order's fate stays unknown past LEG_TIMEOUT_S: a reduceOnly
    close covers both cases (it cannot open anything)."""
    x = FakeExchange(clock).script("linear:Sell", "timeout").script("find:linear", *["error"] * 400)
    r = run([enter()], x, clock)
    assert x.created("spot:Buy") == []
    assert x.short == 0 and hedged(x)
    assert r.escalate == "NO_NEW_POSITIONS"


def test_unknown_spot_outcome_is_journaled_and_resolved_next_cycle(clock):
    x = FakeExchange(clock).script("spot:Buy", "timeout").script("find:spot", *["hidden"] * 400)
    r = run([enter()], x, clock)
    b = r.book_updates["ETHUSDT"]
    assert len(b.pending_spot) == 1 and b.pending_spot[0].startswith("Buy:")
    assert x.short == 0                                       # the perp was not left naked
    assert any(a.startswith("ORDER_UNKNOWN") for a in r.alerts)
    # next cycle: the order shows up filled -> its coins become the book's
    x.faults.clear()
    upd, alerts = ex.resolve_pending({"ETHUSDT": b}, x)
    nb = upd["ETHUSDT"]
    assert nb.pending_spot == () and nb.spot_qty == pytest.approx(x.spot)


def test_book_unreadable_aborts_the_entry_before_any_order(clock):
    x = FakeExchange(clock).script("book:linear", "error")
    r = run([enter()], x, clock)
    assert x.created() == [] and "ETHUSDT" not in r.book_updates
    assert any(a.startswith("ENTRY_ABORTED") for a in r.alerts)


def test_perp_not_filled_means_nothing_else(clock):
    x = FakeExchange(clock).script("linear:Sell", ("partial", 0.0))
    r = run([enter()], x, clock)
    assert x.created("spot:Buy") == [] and x.short == 0 and r.escalate is None


# --- exits (decision 13.14): no fresh price needed ---------------------------------------- #

def open_exchange(clock, **kw):
    return FakeExchange(clock, short=0.03, spot=0.03, **kw)


def open_snapshot(**kw):
    kw.setdefault("positions", {"ETHUSDT": short(size=0.03)})
    kw.setdefault("acct", account(usdt=16.0, coins={"ETH": 0.03}))
    return snapshot(**kw)


def exit_(legs=("spot", "perp"), spot=0.03, perp=None):
    return Action("EXIT", "ETHUSDT", "test exit", legs=legs, perp_qty=perp, spot_qty=spot)


def test_exit_sells_spot_at_a_book_limit_then_closes_perp_reduce_only_market(clock):
    x = open_exchange(clock)
    r = run([exit_()], x, clock, book={"ETHUSDT": OPEN}, snap=open_snapshot())
    sell, close = x.created()
    assert (sell["category"], sell["side"], sell["orderType"], sell["timeInForce"]) == \
        ("spot", "Sell", "Limit", "IOC")
    assert sell["price"] == "2499.8" and sell["qty"] == "0.03"
    assert (close["category"], close["side"], close["orderType"], close["reduceOnly"]) == \
        ("linear", "Buy", "Market", True)
    assert close["qty"] == "0.03"                           # all of it, from the position
    assert x.short == 0 and x.spot == 0
    b = r.book_updates["ETHUSDT"]
    assert b.status == "FLAT" and b.entry_times == OPEN.entry_times


def test_exit_perp_never_reads_a_price(clock):
    x = open_exchange(clock).script("book:linear", *["error"] * 10)
    run([exit_()], x, clock, book={"ETHUSDT": OPEN}, snap=open_snapshot())
    assert not any(c[0] == "book" and c[1] == "linear" for c in x.calls)
    assert x.short == 0


def test_exit_spot_without_an_orderbook_sells_market_with_an_alert(clock):
    x = open_exchange(clock).script("book:spot", "error")
    r = run([exit_()], x, clock, book={"ETHUSDT": OPEN}, snap=open_snapshot())
    sell = x.created("spot:Sell")[0]
    assert sell["orderType"] == "Market" and sell["marketUnit"] == "baseCoin"
    assert any(a.startswith("SPOT_MARKET_SELL") for a in r.alerts)
    assert x.spot == 0 and x.short == 0


def test_exit_spot_unfilled_limit_falls_back_to_market_with_an_alert(clock):
    x = open_exchange(clock).script("spot:Sell", *[("partial", 0.0)] * 1000)
    r = run([exit_()], x, clock, book={"ETHUSDT": OPEN}, snap=open_snapshot())
    assert x.created("spot:Sell")[-1]["orderType"] == "Market"
    assert any(a.startswith("SPOT_MARKET_SELL") for a in r.alerts)


def test_margin_emergency_exit_closes_perp_first(clock):
    x = open_exchange(clock)
    run([exit_(legs=("perp", "spot"))], x, clock, book={"ETHUSDT": OPEN}, snap=open_snapshot())
    kinds = [c[1] for c in x.calls if c[0] == "create"]
    assert kinds[0] == "linear:Buy" and kinds[-1] == "spot:Sell"


def test_exit_never_sells_more_than_the_book(clock):
    """Foreign coins (decision 13.14) stay put even if an action asked more."""
    x = FakeExchange(clock, short=0.03, spot=0.53)
    r = run([exit_(spot=0.53)], x, clock, book={"ETHUSDT": OPEN},
            snap=open_snapshot(acct=account(usdt=16.0, coins={"ETH": 0.53})))
    assert x.created("spot:Sell")[0]["qty"] == "0.03"
    assert x.spot == pytest.approx(0.5)
    assert any(a.startswith("CRITICAL") for a in r.alerts)


def test_close_all_with_unreadable_positions_uses_the_book(clock):
    x = open_exchange(clock)
    snap = snapshot(acct=account(usdt=16.0, coins={"ETH": 0.03}), positions={},
                    errors={"positions:ETHUSDT": "x"})
    run([exit_()], x, clock, book={"ETHUSDT": OPEN}, snap=snap, state="UNWIND")
    assert x.created("linear:Buy")[0]["qty"] == "0.03" and x.short == 0


def test_exit_when_the_position_is_already_gone_goes_flat(clock):
    """reduceOnly on a zero position (retCode 110017): nothing to close, the
    book goes FLAT instead of waiting for a short that is not there."""
    x = FakeExchange(clock, short=0.0, spot=0.03)
    r = run([exit_()], x, clock, book={"ETHUSDT": OPEN}, snap=open_snapshot())
    assert r.book_updates["ETHUSDT"].status == "FLAT" and r.escalate is None


def test_long_perp_never_ours_is_closed_reduce_only_sell(clock):
    x = FakeExchange(clock)
    x.long = 0.05
    snap = snapshot(positions={"ETHUSDT": short(size=0.0)}, acct=account(usdt=100.0))
    from carry.snapshot import PerpPosition
    snap = snapshot(positions={"ETHUSDT": PerpPosition("ETHUSDT", "Buy", 0.05, 2500.0, None, 1)},
                    acct=account(usdt=100.0))
    run([Action("EXIT", "ETHUSDT", "long perp is never ours", legs=("perp",))], x, clock, snap=snap)
    [o] = x.created()
    assert (o["side"], o["reduceOnly"], o["orderType"]) == ("Sell", True, "Market")
    assert x.long == 0


def test_rebalance_and_trim_touch_only_book_quantities(clock):
    x = FakeExchange(clock, short=0.01, spot=0.53)
    r = run([Action("REBALANCE_TOWARD_NEUTRAL", "ETHUSDT", "adl", legs=("spot",), spot_qty=0.02)],
            x, clock, book={"ETHUSDT": OPEN},
            snap=open_snapshot(positions={"ETHUSDT": short(size=0.01)},
                               acct=account(usdt=16.0, coins={"ETH": 0.53})))
    assert x.created("spot:Sell")[0]["qty"] == "0.02" and x.spot == pytest.approx(0.51)
    assert r.book_updates["ETHUSDT"].spot_qty == pytest.approx(0.01)
    y = open_exchange(clock)
    r = run([Action("TRIM", "ETHUSDT", "adl", legs=("spot", "perp"), perp_qty=0.01, spot_qty=0.01)],
            y, clock, book={"ETHUSDT": OPEN}, snap=open_snapshot())
    assert y.short == pytest.approx(0.02) and y.spot == pytest.approx(0.02)
    b = r.book_updates["ETHUSDT"]
    assert (b.perp_qty, b.spot_qty) == (pytest.approx(0.02), pytest.approx(0.02))


# --- Earn ------------------------------------------------------------------------------- #

def test_redeem_is_placed_once_with_the_plans_link(clock):
    x = FakeExchange(clock)
    a = Action("EARN_REDEEM_FOR_ENTRY", "ETHUSDT", "entry", usdt=90.0, coin="USDT",
               order_link_id="cy-c1-R-ETHUSDT")
    run([a], x, clock)
    [call] = [c for c in x.calls if c[0] == "earn"]
    body = call[3]
    assert (body["orderType"], body["coin"], body["amount"], body["orderLinkId"]) == \
        ("Redeem", "USDT", "90.00", "cy-c1-R-ETHUSDT")
    run([a], x, clock)                                         # rerun of the same cycle
    assert len([c for c in x.calls if c[0] == "earn"]) == 1


def test_earn_return_stakes_only_usdt(clock):
    x = FakeExchange(clock)
    run([Action("EARN_RETURN", None, "idle", usdt=25.0, coin="USDT")], x, clock)
    assert [c[1] for c in x.calls if c[0] == "earn"] == ["Stake"]
    y = FakeExchange(clock)
    r = run([Action("EARN_RETURN", None, "idle", usdt=1.0, coin="ETH")], y, clock)
    assert not [c for c in y.calls if c[0] == "earn"]
    assert any(a.startswith("CRITICAL") for a in r.alerts)


# --- gates -------------------------------------------------------------------------------- #

def test_dry_run_refuses_a_live_exchange_before_any_call(clock):
    x = FakeExchange(clock)
    x.live = True
    with pytest.raises(ex.DryRunRefused):
        run([enter()], x, clock, c=cfg(DRY_RUN=True))
    assert x.calls == []


def test_live_exchange_refuses_writes_in_dry_run():
    class Client:
        def create_order(self, r):
            raise AssertionError("sent")

        def place_order(self, r):
            raise AssertionError("sent")
    live = ex.LiveExchange(Client(), dry_run=True)
    with pytest.raises(ex.DryRunRefused):
        live.create_order({})
    with pytest.raises(ex.DryRunRefused):
        live.place_earn({})


def test_state_gate_drops_entries_outside_normal(clock):
    x = FakeExchange(clock)
    r = run([enter()], x, clock, state="NO_NEW_POSITIONS")
    assert x.calls == [] and any(a.startswith("CRITICAL") for a in r.alerts)


def test_rejected_perp_open_is_reported_and_nothing_else(clock):
    x = FakeExchange(clock).script("linear:Sell", "reject")
    r = run([enter()], x, clock)
    assert x.created("spot:Buy") == [] and x.short == 0
    assert any(a.startswith("ENTRY_ABORTED") for a in r.alerts)


def test_every_order_is_recorded(clock):
    x = FakeExchange(clock)
    r = run([enter()], x, clock)
    assert [o["leg"] for o in r.orders] == ["perp", "spot"]
    assert all(o["outcome"] == "done" and o["filled"] > 0 for o in r.orders)


def test_api_error_class_is_reused():
    assert issubclass(ex.DryRunRefused, RuntimeError)
    assert not issubclass(ex.DryRunRefused, BybitAPIError)
