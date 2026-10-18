"""The fixes of the audit of 18/10 (calibration/mainnet_roundtrip_20261017/
AUDIT.md), as Giannis approved them:

1. The seq -1 placeholder row is always ignored (test_carry_mainnet_check,
   test_carry_mainnet_roundtrip_20261017). A position that cannot be read
   while the book is OPEN: CRITICAL to Telegram at once (its own channel, no
   2-cycle filter); after 6 cycles in a row, a controlled exit on the book
   (spot sell, perp reduceOnly).
2. The ledger: the coin fee counted once, the coin kept valued, not lost.
3. Tiered Earn APR: layer A is the tiers' rate on the amount staked; the
   entry threshold and the daily summary use it.
4. The fee remainder in the coin is the book's dust_qty: the next entry buys
   that much less, its exit sells it with the rest; FOREIGN_BALANCE only
   beyond book + dust.
5. Paper and live: separate ledger and book.
"""

import json
from dataclasses import replace
from types import MappingProxyType

import pytest

import heartbeat
import run_carry_cycle as rcc
import settings
from carry import book as book_store
from carry import exposure as cx
from carry import execute as ex
from carry import ledger
from carry import snapshot as sn
from carry.paper import PaperExchange, PaperState
from carry.plan import UNREADABLE_EXIT_CYCLES, SymbolBook, plan_cycle
from carry_builders import NOW, account, cfg, earn, market, short, snapshot
from carry_regimes import FakeClient, Regime
from fake_exchange import FakeClock, FakeExchange
from helpers import KEY
from test_carry_phase5 import BULL, carry_env, cycle  # noqa: F401  (fixture)

CFG = cfg(SYMBOLS=["ETHUSDT"])
OPEN = SymbolBook(status="OPEN", entered_ms=NOW - 400 * 3_600_000, perp_qty=0.03,
                  spot_qty=0.03, entry_times=(NOW - 400 * 3_600_000,))
TIERS = [{"min": "0", "max": "100", "estimateApr": "6.94%"},
         {"min": "100", "max": "-1", "estimateApr": "1.94%"}]


@pytest.fixture
def clock():
    return FakeClock()


# --- 1. a position that cannot be read while the book is OPEN ------------------------- #

def blind(eth=0.03):
    return snapshot(acct=account(usdt=16.0, coins={"ETH": eth}), positions={},
                    errors={"positions:ETHUSDT": "Unreadable: position ETHUSDT: x"})


def test_unreadable_with_the_book_open_is_critical_every_cycle_then_exits():
    book = {"ETHUSDT": OPEN}
    for n in range(1, UNREADABLE_EXIT_CYCLES + 1):
        plan = plan_cycle(blind(), CFG, "NORMAL", book, f"c{n}")
        book = {"ETHUSDT": plan.book_updates["ETHUSDT"]}
        assert book["ETHUSDT"].unreadable_cycles == n
        [crit] = [a for a in plan.alerts if a.startswith("CRITICAL: POSITION_UNREADABLE:")]
        assert f"ETHUSDT book OPEN (short 0.03, spot 0.03)" in crit and f"{n}/6 cycles" in crit
        assert heartbeat._is_blocking_code(crit, heartbeat.CARRY_BLOCKING_CODES) == "CRITICAL"
        assert not any(a.kind == "ENTER" for a in plan.actions)
        exits = [a for a in plan.actions if a.kind == "EXIT"]
        if n < UNREADABLE_EXIT_CYCLES:
            assert exits == [], n
        else:
            [e] = exits
            assert "controlled exit" in crit
            assert (e.legs, e.perp_qty, e.spot_qty) == (("spot", "perp"), 0.03, 0.03)
    assert UNREADABLE_EXIT_CYCLES == 6


@pytest.mark.parametrize("state", ["NO_NEW_POSITIONS", "UNWIND"])
def test_the_controlled_exit_is_allowed_in_every_state(state):
    sb = replace(OPEN, unreadable_cycles=UNREADABLE_EXIT_CYCLES - 1)
    plan = plan_cycle(blind(), CFG, state, {"ETHUSDT": sb}, "c")
    assert [a.kind for a in plan.actions] == ["EXIT"]


def test_the_counter_restarts_once_the_position_reads_again():
    sb = replace(OPEN, unreadable_cycles=4)
    snap = snapshot(positions={"ETHUSDT": short(size=0.03)},
                    acct=account(usdt=16.0, coins={"ETH": 0.03}))
    plan = plan_cycle(snap, CFG, "NORMAL", {"ETHUSDT": sb}, "c")
    assert plan.book_updates["ETHUSDT"].unreadable_cycles == 0
    assert not any("POSITION_UNREADABLE" in a for a in plan.alerts)
    assert not any(a.kind == "EXIT" for a in plan.actions)


def test_unreadable_with_the_book_flat_is_not_this_alert():
    plan = plan_cycle(blind(eth=0.0), CFG, "NORMAL", {}, "c")
    assert not any("POSITION_UNREADABLE" in a for a in plan.alerts)
    assert "ETHUSDT" not in plan.book_updates or \
        plan.book_updates["ETHUSDT"].unreadable_cycles == 0


def test_the_controlled_exit_sells_the_spot_and_closes_the_book_short_reduce_only(clock):
    sb = replace(OPEN, unreadable_cycles=UNREADABLE_EXIT_CYCLES - 1)
    snap = blind()
    plan = plan_cycle(snap, CFG, "NORMAL", {"ETHUSDT": sb}, "c")
    x = FakeExchange(clock, short=0.03, spot=0.03)
    book = dict({"ETHUSDT": sb}, **plan.book_updates)
    r = ex.execute_plan(plan, snap, CFG, "NORMAL", book, "c", x, clock=clock, sleep=clock.sleep)
    sell, close = x.created()
    assert (sell["category"], sell["side"], sell["qty"]) == ("spot", "Sell", "0.03")
    assert (close["category"], close["side"], close["orderType"], close["reduceOnly"],
            close["qty"]) == ("linear", "Buy", "Market", True, "0.03")
    assert x.short == 0 and x.spot == 0
    b = r.book_updates["ETHUSDT"]
    assert b.status == "FLAT" and b.unreadable_cycles == 0


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, token, chat, text):
        self.sent.append(text)
        return True


def test_the_critical_goes_to_telegram_at_once_on_its_own_channel(tmp_path):
    from notify import Notifier
    send = Recorder()
    n = Notifier({"TELEGRAM_BOT_TOKEN": "t", "ALERT_TELEGRAM_CHAT_ID": "1"}, sender=send,
                 state_file=tmp_path / "notify.json")
    crit = ("CRITICAL: POSITION_UNREADABLE: ETHUSDT book OPEN (short 0.03, spot 0.03), "
            "position unreadable 1/6 cycles: Unreadable: x")
    other = "CRITICAL: something else already announced"
    rcc.notify_cycle({"cycle_id": "c0", "alerts": [other], "risk_state": "NORMAL",
                      "dry_run": False}, n)
    assert len(send.sent) == 1
    rcc.notify_cycle({"cycle_id": "c1", "alerts": [other, crit], "risk_state": "NORMAL",
                      "dry_run": False}, n)
    assert len(send.sent) == 2 and "POSITION_UNREADABLE" in send.sent[-1]   # first cycle
    rcc.notify_cycle({"cycle_id": "c2", "alerts": [other, crit.replace("1/6", "2/6")],
                      "risk_state": "NORMAL", "dry_run": False}, n)
    assert len(send.sent) == 2                                  # the same condition
    exit_ = crit.replace("1/6 cycles", "6/6 cycles; controlled exit on the book's quantities")
    rcc.notify_cycle({"cycle_id": "c6", "alerts": [other, exit_], "risk_state": "NORMAL",
                      "dry_run": False}, n)
    assert len(send.sent) == 3 and "controlled exit" in send.sent[-1]
    rcc.notify_cycle({"cycle_id": "c7", "alerts": [other], "risk_state": "NORMAL",
                      "dry_run": False}, n)
    assert send.sent[-1].startswith("✅ CARRY: the position is readable again")


# --- 2. the ledger ------------------------------------------------------------------------ #

def legs(buy_qty=0.01002, coin_fee=0.00001002, sold=0.01):
    return [
        {"type": "order", "symbol": "ETHUSDT", "action": "ENTER", "leg": "perp",
         "category": "linear", "side": "Sell", "filled": 0.01, "avg_price": 2415.77,
         "fee": 0.01328674, "fee_detail": {"USDT": "0.01328674"}},
        {"type": "order", "symbol": "ETHUSDT", "action": "ENTER", "leg": "spot",
         "category": "spot", "side": "Buy", "filled": buy_qty, "avg_price": 2416.96,
         "fee": coin_fee, "fee_detail": {"ETH": str(coin_fee)}},
        {"type": "order", "symbol": "ETHUSDT", "action": "SELL", "leg": "spot",
         "category": "spot", "side": "Sell", "filled": sold, "avg_price": 2398.69,
         "fee": 0.0239869, "fee_detail": {"USDT": "0.0239869"}},
        {"type": "order", "symbol": "ETHUSDT", "action": "CLOSE", "leg": "perp",
         "category": "linear", "side": "Buy", "filled": 0.01, "avg_price": 2397.41,
         "fee": 0.01318576, "fee_detail": {"USDT": "0.01318576"}}]


def test_the_round_trip_of_17_18_10_is_the_wallet_change():
    rt = ledger.round_trip("ETHUSDT", legs(), 1)
    assert rt["kept_qty"] == pytest.approx(0.00000998, abs=1e-14)
    assert rt["kept_value"] == pytest.approx(0.00000998 * 2398.69)
    s = ledger.summary(legs() + [rt, {"type": "funding", "amount": 0.00047121}])
    assert round(s["fees"], 8) == 0.07467734                      # the coin fee once, here
    assert round(s["net"], 8) == -0.07348846                      # = the wallet (AUDIT §3)


def test_a_coin_fee_is_never_counted_twice():
    """The same trip with the fee in USDT instead: the same net, to the fee."""
    in_coin = ledger.round_trip("ETHUSDT", legs(), 1)
    usdt = legs(buy_qty=0.01000998, coin_fee=0.0)
    usdt[1]["fee"], usdt[1]["fee_detail"] = 0.00001002 * 2416.96, {"USDT": "x"}
    in_usdt = ledger.round_trip("ETHUSDT", usdt, 1)
    net = lambda rows, rt: ledger.summary(rows + [rt])["net"]       # noqa: E731
    assert net(legs(), in_coin) == pytest.approx(net(usdt, in_usdt), abs=1e-9)


def test_dust_of_an_earlier_trip_sold_in_this_one_comes_out():
    """The entry used 0.00000998 ETH of book dust (bought less), the exit
    sold it: kept_qty < 0 takes its value back out."""
    rows = legs(buy_qty=0.01001, coin_fee=0.00001001, sold=0.01)
    rt = ledger.round_trip("ETHUSDT", rows, 1)
    assert rt["kept_qty"] == pytest.approx(0.01001 - 0.00001001 - 0.01, abs=1e-14)
    assert rt["kept_qty"] < 0 and rt["kept_value"] < 0


# --- 3. tiered Earn APR --------------------------------------------------------------------- #

@pytest.mark.parametrize("amount,apr", [(57.1714, 0.0694), (100.0, 0.0694), (0.0, 0.0694),
                                        (200.0, (100 * 0.0694 + 100 * 0.0194) / 200)])
def test_tier_apr(amount, apr):
    tiers = sn.tiers_of({"hasTieredApr": True, "tierAprDetails": TIERS})
    assert sn.tier_apr(tiers, amount) == pytest.approx(apr)


@pytest.mark.parametrize("details", [None, [], [{"min": "0", "max": "100", "estimateApr": "6.94%"}],
                                     [{"min": "0", "max": "100", "estimateApr": "6.94%"},
                                      {"min": "150", "max": "-1", "estimateApr": "1.94%"}],
                                     [{"min": "0", "max": "100", "estimateApr": "x"},
                                      {"min": "100", "max": "-1", "estimateApr": "1.94%"}]])
def test_tiers_that_cannot_be_read_make_the_product_unreadable(details):
    with pytest.raises(sn.Unreadable):
        sn.tiers_of({"hasTieredApr": True, "tierAprDetails": details})


def test_a_flat_rate_product_has_no_tiers():
    assert sn.tiers_of({"hasTieredApr": False, "tierAprDetails": TIERS}) is None
    assert sn.tiers_of({}) is None


def test_the_entry_threshold_is_on_the_tier_rate():
    """Layer A 6.94 % (57 USDT staked) instead of the headline 1.94 %: the
    same funding that entered on 1.94 % stays out."""
    m = market(rate=0.0001, settled=[0.0001] * 12)              # ~11 % APR
    low = earn(staked=57.17, apr=0.0194)
    tiered = replace(low, apr=sn.tier_apr(sn.tiers_of({"hasTieredApr": True,
                                                       "tierAprDetails": TIERS}), 57.17))
    for e, enters in ((low, True), (tiered, False)):
        snap = snapshot(markets={"ETHUSDT": m}, earn_=e, acct=account(usdt=100.0))
        d = plan_cycle(snap, CFG, "NORMAL", {}, "c").decisions["ETHUSDT"]
        assert (d.action == "ENTER") is enters, d.reason


def test_the_paper_account_earns_the_tier_rate():
    p = PaperExchange(PaperState(earn_staked=100.07), cfg())
    tiers = sn.tiers_of({"hasTieredApr": True, "tierAprDetails": TIERS})
    assert p.rate(0.0194, tiers) == pytest.approx((100 * 0.0694 + 0.07 * 0.0194) / 100.07)
    assert p.rate(0.0194, None) == 0.0194 and p.rate(None, tiers) is None


def test_the_daily_summary_shows_the_real_earn_apr(tmp_path):
    import summary
    line = summary.earn_line({"apr": 0.0694, "headline_apr": 0.0194, "tiered": True,
                              "staked": 57.1714})
    assert line == "Earn: 6,9% στα 57.17 USDT (κλιμακωτό APR· το βασικό 1,9%)"
    assert summary.earn_line({"apr": 0.0173, "tiered": False, "staked": 10}) == \
        "Earn: 1,7% στα 10.00 USDT"
    from datetime import datetime, timezone
    now = 1_792_300_000_000
    rec = {"ts": datetime.fromtimestamp(now / 1000, timezone.utc).isoformat(), "dry_run": False,
           "risk_state": "NORMAL", "alerts": [],
           "earn": {"apr": 0.0694, "headline_apr": 0.0194, "tiered": True, "staked": 57.1714}}
    (tmp_path / "2026-10-18.jsonl").write_text(json.dumps(rec) + "\n")
    text = summary.build_carry_summary(tmp_path, tmp_path / "rs.json", tmp_path / "hold.json",
                                       KEY, now_ms=now + 1000)
    assert "Earn: 6,9% στα 57.17 USDT (κλιμακωτό APR· το βασικό 1,9%)" in text


class TieredClient(FakeClient):
    def get_earn_products(self, coin=None):
        return [{"coin": "USDT", "productId": "1", "estimateApr": "1.94%", "hasTieredApr": True,
                 "tierAprDetails": TIERS}]


def test_a_dry_cycle_records_the_tier_rate(carry_env):  # noqa: F811
    regime = Regime(BULL)
    t = regime.cycle_times()[0]
    rcc_cycle_client = TieredClient(apr="1.94%")
    from test_carry_phase5 import Clock, Pinger, renew
    renew(t)
    clk = Clock()
    rec, rc = rcc.run_cycle(carry_env["cfg"], carry_env["env"], rcc_cycle_client, now_ms=t,
                            snapshot_fn=regime.snapshot_fn(), clock=clk, sleep=clk.sleep,
                            paper_start=(0.0, 100.0), opener=Pinger())
    assert rc == 0, rec["alerts"]
    assert rec["earn"] == {"apr": pytest.approx(0.0694), "headline_apr": 0.0194, "tiered": True,
                           "staked": 100.0}
    assert rec["funding"]["ETHUSDT"]["layer_a_apr"] == pytest.approx(0.0694)


# --- 4. the fee remainder is ours ----------------------------------------------------------- #

def test_book_dust_is_not_a_foreign_balance():
    """Builders' ETH spot minimum is 0.00062."""
    for dust, wallet, foreign in ((0.0005, 0.0011, False), (0.0, 0.0011, True),
                                  (0.0005, 0.0012, True)):
        snap = snapshot(acct=account(usdt=100.0, coins={"ETH": wallet}))
        plan = plan_cycle(snap, CFG, "NORMAL", {"ETHUSDT": SymbolBook(dust_qty=dust)}, "c")
        assert any(a.startswith("FOREIGN_BALANCE") for a in plan.alerts) is foreign, \
            (dust, wallet, plan.alerts)


def test_the_entry_size_buys_the_dust_less():
    snap = snapshot(acct=account(usdt=100.0, coins={"ETH": 0.0005}),
                    markets={"ETHUSDT": market(rate=0.0003)})
    with_dust = plan_cycle(snap, CFG, "NORMAL", {"ETHUSDT": SymbolBook(dust_qty=0.0005)}, "c")
    without = plan_cycle(snapshot(acct=account(usdt=100.0),
                                  markets={"ETHUSDT": market(rate=0.0003)}),
                         CFG, "NORMAL", {}, "c")
    [a] = [x for x in with_dust.actions if x.kind == "ENTER"]
    [b] = [x for x in without.actions if x.kind == "ENTER"]
    assert a.perp_qty == b.perp_qty
    assert a.spot_qty == pytest.approx(b.spot_qty - 0.0005, abs=0.00002)


def test_the_entry_uses_the_dust_and_the_exit_keeps_the_remainder(clock):
    """17-18/10 with the dust in the book: buy 0.03003 instead of 0.03004;
    spot 0.00000998 + 0.02999997; the exit sells 0.03 and 0.00000995 stays
    the book's dust. Three round trips: the wallet never holds more than
    one remainder, always equal to the book's dust."""
    d = 0.00000998
    x = FakeExchange(clock, spot=d)
    book = {"ETHUSDT": SymbolBook(dust_qty=d)}
    for trip in range(3):
        snap = snapshot(acct=account(usdt=200.0, coins={"ETH": x.spot}))
        entry = ex.execute_plan(
            plan_of_enter(), snap, CFG, "NORMAL", book, f"e{trip}", x, clock=clock,
            sleep=clock.sleep)
        book.update(entry.book_updates)
        sb = book["ETHUSDT"]
        assert sb.status == "OPEN" and sb.dust_qty == 0 and sb.perp_qty == 0.03
        assert sb.spot_qty == pytest.approx(x.spot, abs=1e-12) and sb.spot_qty >= 0.03
        if trip == 0:
            assert x.created("spot:Buy")[0]["qty"] == "0.03003"
        snap = snapshot(positions={"ETHUSDT": short(size=0.03)},
                        acct=account(usdt=200.0, coins={"ETH": x.spot}))
        plan = plan_cycle(snap, CFG, "UNWIND", book, f"x{trip}")
        out = ex.execute_plan(plan, snap, CFG, "UNWIND", book, f"x{trip}", x, clock=clock,
                              sleep=clock.sleep)
        book.update(out.book_updates)
        sb = book["ETHUSDT"]
        assert sb.status == "FLAT" and x.short == 0
        assert sb.dust_qty == pytest.approx(x.spot, abs=1e-12) and sb.dust_qty < 0.00001


def plan_of_enter():
    from carry.plan import Action, Plan
    return Plan((Action("ENTER", "ETHUSDT", "t", legs=("perp", "spot"), perp_qty=0.03,
                        spot_qty=0.03004, usdt=75.0),), (), MappingProxyType({}),
                MappingProxyType({}))


def test_the_exposure_check_counts_the_book_dust_as_ours(tmp_path):
    path = tmp_path / "book.json"
    snap = snapshot(acct=account(usdt=1.0, coins={"ETH": 0.0011}))
    take = lambda c, cfg_, now_ms=None, private=True: snap          # noqa: E731
    book_store.save(path, KEY, {"ETHUSDT": SymbolBook(dust_qty=0.0005)}, NOW)
    assert cx.check(object(), CFG, path, KEY, take=take).verdict == cx.FLAT
    book_store.save(path, KEY, {"ETHUSDT": SymbolBook()}, NOW)
    e = cx.check(object(), CFG, path, KEY, take=take)
    assert e.verdict == cx.OPEN and "exchange ETH: wallet 0.0011 (> dust 0.00062)" in e.lines


# --- 5. paper and live apart ---------------------------------------------------------------- #

def test_a_dry_cycle_writes_the_paper_book_and_ledger_only(carry_env):  # noqa: F811
    regime = Regime(BULL)
    for t in regime.cycle_times()[:3]:
        rec, rc = cycle(carry_env, regime, t)
        assert rc == 0, rec["alerts"]
    assert settings.carry_book_file(paper=True).exists()
    assert not settings.carry_book_file().exists()
    assert settings.carry_book_file(paper=True) != settings.carry_book_file()
    log = carry_env["log_dir"]
    assert list((log / "paper" / "ledger").glob("carry_ledger_*.jsonl"))
    assert not (log / "ledger").exists()
    assert ledger.read(ledger.home(log, True)) and ledger.read(log) == []


def test_the_legacy_paper_rows_leave_the_live_ledger(tmp_path):
    rows = [{"type": "earn_interest", "ts_ms": NOW, "amount": 1e-5, "apr": 0.0196},
            {"type": "order", "ts_ms": NOW + 1, "symbol": "ETHUSDT", "filled": 0.01},
            {"type": "funding", "ts_ms": NOW + 2, "amount": 0.0005}]
    ledger.write(tmp_path, rows, NOW, "c")
    assert ledger.split_legacy_paper(tmp_path) == 1
    assert [r["type"] for r in ledger.read(tmp_path)] == ["order", "funding"]
    assert [r["type"] for r in ledger.read(ledger.home(tmp_path, True))] == ["earn_interest"]
    assert ledger.split_legacy_paper(tmp_path) == 0                   # idempotent


def test_a_cycle_splits_the_legacy_rows_once_with_an_alert(carry_env):  # noqa: F811
    log = carry_env["log_dir"]
    ledger.write(log, [{"type": "earn_interest", "ts_ms": NOW, "amount": 1e-5}], NOW, "old")
    regime = Regime(BULL)
    t1, t2 = regime.cycle_times()[:2]
    rec, _ = cycle(carry_env, regime, t1)
    assert any(a.startswith("LEDGER_SPLIT: 1 DRY_RUN row(s) moved") for a in rec["alerts"])
    rec, _ = cycle(carry_env, regime, t2)
    assert not any(a.startswith("LEDGER_SPLIT") for a in rec["alerts"])
    assert ledger.read(log) == []
