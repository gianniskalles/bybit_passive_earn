"""CARRY_PLAN §7, Phase 3 acceptance: property tests (hypothesis) of the
invariants of plan_cycle over random snapshots, books and risk states.

  1. never an exposure increase outside NORMAL
  2. never above the limits (per symbol, total cap - buffer)
  3. exits always permitted: UNWIND with a position always exits
  4. never a funding-motivated action within 15' of the settlement
  5. never spot without a completed redeem; Earn only ever gets USDT, and
     never the buffer while a position is open
  6. coins beyond the book are never traded: no spot sale above the book's
     spot_qty, no entry while a foreign balance is in the wallet
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from carry import plan as cp
from carry import state as cs
from carry.plan import SymbolBook
from carry_builders import (MIN, NOW, account, cfg, earn, foreign_order, market, redeem_order, short,
                            snapshot)

CFG = cfg()
LINK = "cy-c0-R-ETHUSDT"


@st.composite
def scenario(draw):
    # Half the scenarios open every gate (NORMAL, clean account, strong
    # funding, outside the window) so the entry paths are exercised; the other
    # half draw every gate at random.
    open_gates = draw(st.sampled_from([True, False]))
    pick = (lambda good, *bad: good) if open_gates else (lambda good, *bad: draw(st.sampled_from((good,) + bad)))
    state = pick("NORMAL", "NO_NEW_POSITIONS", "UNWIND", "PANIC")
    level = pick(0.0003, 0.00005, -0.0005)
    rates = draw(st.lists(st.floats(level - 0.0001, level + 0.0001), min_size=10, max_size=12))
    mins = pick(120, 480, 16, 15, 14, 10, 1, 0)
    m = market(settled=rates, rate=rates[-1], minutes_to_settlement=mins,
               basis_bps=pick(0.0, 5.0, 15.0, -15.0), perp_status=pick("Trading", "Settling"))
    short_qty = draw(st.sampled_from([0.0, 0.01, 0.02, 0.03]))
    spot_pick = draw(st.sampled_from(["same", 0.0, 0.01, 0.036]))
    spot_qty = short_qty if spot_pick == "same" else spot_pick
    foreign = draw(st.sampled_from([0.0, 0.0, 0.5]))       # coins the system never bought
    adl = draw(st.sampled_from([1, 4]))
    acct = account(usdt=draw(st.sampled_from([150.0, 10.0, 95.0, 300.0, 40.0, 0.0])),
                   coins={"ETH": spot_qty + foreign},
                   mm_rate=draw(st.sampled_from([0.02, 0.3, 0.45, 0.65])),
                   borrow=pick(0, 5), margin_mode=pick("REGULAR_MARGIN", "ISOLATED_MARGIN"),
                   collateral_active=pick(True, False))
    status = draw(st.sampled_from(["FLAT", "OPEN", "REDEEMING"]))
    red_status = draw(st.sampled_from([None, "Pending", "Success", "Fail"]))
    orders = () if red_status is None else (redeem_order(LINK, red_status),)
    started = NOW - draw(st.integers(0, 200)) * MIN
    sb = SymbolBook(status=status, perp_qty=draw(st.sampled_from([0.0, 0.03])),
                    spot_qty=spot_qty if status == "OPEN" else 0.0,
                    entered_ms=NOW - draw(st.integers(0, 800)) * 3_600_000,
                    redeem_link=LINK if status == "REDEEMING" else None,
                    redeem_started_ms=started if status == "REDEEMING" else None)
    snap = snapshot(markets={"ETHUSDT": m}, positions={"ETHUSDT": short(size=short_qty, adl=adl)},
                    acct=acct, earn_=earn(staked=draw(st.sampled_from([500.0, 100.0, 50.0, 0.0])),
                                          orders=orders),
                    open_orders=(foreign_order(),) if pick(False, True) else (),
                    region=pick(False, True))
    return state, snap, {"ETHUSDT": sb}, mins, short_qty, spot_qty, foreign


def test_scenarios_reach_every_action():
    from collections import Counter
    seen = Counter()

    @settings(max_examples=1500, deadline=None, database=None, derandomize=True)
    @given(scenario())
    def collect(sc):
        seen.update(a.kind for a in run(sc).actions)
    collect()
    assert {"ENTER", "EARN_REDEEM_FOR_ENTRY", "EXIT", "TRIM", "REBALANCE_TOWARD_NEUTRAL",
            "EARN_RETURN"} <= set(seen), seen


def run(sc):
    state, snap, book, *_ = sc
    return cp.plan_cycle(snap, CFG, state, book, "c1")


SETTINGS = settings(max_examples=400, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(scenario())
def test_no_exposure_increase_outside_normal(sc):
    plan = run(sc)
    if sc[0] != "NORMAL":
        assert not [a for a in plan.actions if a.kind in cs.EXPOSURE_INCREASING]
    assert all(cs.is_allowed(sc[0], a.kind) for a in plan.actions)


@SETTINGS
@given(scenario())
def test_never_above_the_limits(sc):
    _, snap, _, _, short_qty, *_ = sc
    open_notional = short_qty * snap.markets["ETHUSDT"].mark_price
    room = CFG["TOTAL_CAPITAL_CAP_USD"] - CFG["USDT_BUFFER_USD"]
    for a in run(sc).actions:
        if a.kind == "ENTER":
            assert a.usdt <= CFG["MAX_NOTIONAL_PER_SYMBOL_USD"] + 1e-9
            assert open_notional + a.usdt <= room + 1e-9
            assert short_qty == 0


@SETTINGS
@given(scenario())
def test_unwind_with_a_position_always_exits(sc):
    state, _, book, _, short_qty, *_ = sc
    if state == "UNWIND" and short_qty > 0:
        assert any(a.kind == "EXIT" for a in run(sc).actions)


@SETTINGS
@given(scenario())
def test_no_funding_action_within_the_window(sc):
    plan = run(sc)
    if sc[3] < CFG["NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN"]:
        assert not [a for a in plan.actions if a.kind in ("ENTER", "EARN_REDEEM_FOR_ENTRY")]
        d = plan.decisions.get("ETHUSDT")
        if d is not None:
            assert not (d.action == "EXIT" and d.kind == "funding")


@SETTINGS
@given(scenario())
def test_redeem_and_earn_rules(sc):
    _, snap, book, _, short_qty, *_ = sc
    plan = run(sc)
    ks = [a.kind for a in plan.actions]
    assert not ("ENTER" in ks and "EARN_REDEEM_FOR_ENTRY" in ks)
    if "ENTER" in ks and book["ETHUSDT"].status == "REDEEMING":
        o = snap.earn.order(LINK)
        assert o is not None and o.status == "Success"
    for a in plan.actions:
        if a.kind in ("EARN_RETURN", "EARN_REDEEM_FOR_ENTRY"):
            assert a.coin == "USDT"
        if a.kind == "EARN_RETURN":
            keep = CFG["USDT_BUFFER_USD"] if short_qty > 0 else 0
            assert a.usdt <= snap.account.balance("USDT").wallet - keep + 1e-9


@SETTINGS
@given(scenario())
def test_foreign_coins_are_never_traded(sc):
    _, snap, book, _, _, _, foreign = sc
    sb = book["ETHUSDT"]
    held = sb.spot_qty if sb.status == "OPEN" else 0.0
    plan = run(sc)
    for a in plan.actions:
        if a.spot_qty is not None and a.kind != "ENTER":
            assert a.spot_qty <= held + 1e-12, (a, held)
    if foreign:
        assert "ENTER" not in [a.kind for a in plan.actions]
        assert any(x.startswith("FOREIGN_BALANCE") for x in plan.alerts)
