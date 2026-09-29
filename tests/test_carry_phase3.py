"""CARRY_PLAN Phase 3 — plan_cycle: entry (redeem -> wait -> legs), hold,
exit (§4), rebalancing, risk exits R12-R18, return to Earn. Pure: snapshot +
config + risk state + book -> plan. Written before the implementation."""

import dataclasses

import pytest

from carry import plan as cp
from carry.plan import SymbolBook
from carry_builders import (MIN, NOW, account, cfg, earn, foreign_order, market, redeem_order,
                            short, snapshot)


def kinds(plan, symbol=None):
    return [a.kind for a in plan.actions if symbol is None or a.symbol == symbol]


def only(plan, kind):
    [a] = [a for a in plan.actions if a.kind == kind]
    return a


OPEN = SymbolBook(status="OPEN", entered_ms=NOW - 400 * 3_600_000, perp_qty=0.03)


def open_snap(**kw):
    """A neutral 0.03 ETH position (perp short 0.03, spot 0.03)."""
    kw.setdefault("positions", {"ETHUSDT": short(size=0.03)})
    kw.setdefault("acct", account(usdt=16.0, coins={"ETH": 0.03}))
    return snapshot(**kw)


# --- entry: redeem -> wait -> legs (decision 13.4) --------------------------------- #

def test_enters_directly_when_the_uta_already_holds_enough_usdt():
    plan = cp.plan_cycle(snapshot(acct=account(usdt=100.0)), cfg(), "NORMAL", {}, "c1")
    a = only(plan, "ENTER")
    assert a.symbol == "ETHUSDT" and a.perp_qty == pytest.approx(0.03)
    assert a.spot_qty == pytest.approx(0.03004)                 # fee in base (R22)
    assert a.usdt <= cfg()["MAX_NOTIONAL_PER_SYMBOL_USD"]
    assert kinds(plan) == ["ENTER"]


def test_redeems_first_and_never_buys_spot_in_the_same_cycle():
    plan = cp.plan_cycle(snapshot(acct=account(usdt=10.0)), cfg(), "NORMAL", {}, "c1")
    assert kinds(plan) == ["EARN_REDEEM_FOR_ENTRY"]
    r = only(plan, "EARN_REDEEM_FOR_ENTRY")
    assert r.usdt == pytest.approx(0.03004 * 2500.01 * (1 + cp.ENTRY_PRICE_SLACK) + 15 - 10, abs=0.02)
    assert r.order_link_id.startswith("cy-")
    b = plan.book_updates["ETHUSDT"]
    assert (b.status, b.redeem_link, b.redeem_started_ms) == ("REDEEMING", r.order_link_id, NOW)


def redeeming(link="cy-c1-R-ETHUSDT", started=NOW - 30 * MIN, amount=80.0):
    return {"ETHUSDT": SymbolBook(status="REDEEMING", redeem_link=link, redeem_started_ms=started,
                                  redeem_amount=amount)}


@pytest.mark.parametrize("status", ["Pending", "PartiallyProcessed", None])
def test_waits_while_the_redeem_is_not_complete(status):
    orders = () if status is None else (redeem_order("cy-c1-R-ETHUSDT", status),)
    snap = snapshot(acct=account(usdt=10.0), earn_=earn(orders=orders))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", redeeming(), "c2")
    assert "ENTER" not in kinds(plan) and "EARN_REDEEM_FOR_ENTRY" not in kinds(plan)
    assert "EARN_RETURN" not in kinds(plan)
    assert "ETHUSDT" not in plan.book_updates                     # still REDEEMING


def test_enters_once_the_redeem_succeeded():
    snap = snapshot(acct=account(usdt=95.0), earn_=earn(orders=(redeem_order("cy-c1-R-ETHUSDT", "Success"),)))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", redeeming(), "c2")
    assert kinds(plan) == ["ENTER"]


def test_redeem_timeout_abandons_the_entry_with_an_alert():
    snap = snapshot(acct=account(usdt=10.0), earn_=earn(orders=(redeem_order("cy-c1-R-ETHUSDT"),)))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", redeeming(started=NOW - 121 * MIN), "c2")
    assert "ENTER" not in kinds(plan)
    assert any(a.startswith("EARN_REDEEM_STUCK") for a in plan.alerts)
    assert plan.book_updates["ETHUSDT"].status == "FLAT"


def test_failed_redeem_abandons_the_entry():
    snap = snapshot(acct=account(usdt=10.0), earn_=earn(orders=(redeem_order("cy-c1-R-ETHUSDT", "Fail"),)))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", redeeming(), "c2")
    assert plan.book_updates["ETHUSDT"].status == "FLAT" and "ENTER" not in kinds(plan)


def test_redeem_is_abandoned_when_the_state_leaves_normal():
    snap = snapshot(acct=account(usdt=10.0), earn_=earn(orders=(redeem_order("cy-c1-R-ETHUSDT", "Success"),)))
    plan = cp.plan_cycle(snap, cfg(), "NO_NEW_POSITIONS", redeeming(), "c2")
    assert plan.book_updates["ETHUSDT"].status == "FLAT" and "ENTER" not in kinds(plan)


def test_completed_redeem_waits_out_the_settlement_window():
    snap = snapshot(markets={"ETHUSDT": market(minutes_to_settlement=10)}, acct=account(usdt=95.0),
                    earn_=earn(orders=(redeem_order("cy-c1-R-ETHUSDT", "Success"),)))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", redeeming(), "c2")
    assert kinds(plan) == [] and "ETHUSDT" not in plan.book_updates


def test_abandoned_redeem_money_goes_back_to_earn():
    snap = snapshot(markets={"ETHUSDT": market(rate=0.00001)}, acct=account(usdt=90.0))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {}, "c3")
    r = only(plan, "EARN_RETURN")
    assert r.usdt == pytest.approx(90.0) and r.coin == "USDT"


def test_not_enough_in_earn_means_no_entry():
    plan = cp.plan_cycle(snapshot(acct=account(usdt=10.0), earn_=earn(staked=20.0)), cfg(), "NORMAL", {}, "c1")
    assert "EARN_REDEEM_FOR_ENTRY" not in kinds(plan) and "ENTER" not in kinds(plan)


# --- exit: legs -> USDT back to Earn (decision 13.5) --------------------------------- #

def test_funding_exit_closes_spot_then_perp_and_keeps_usdt_for_next_cycle():
    snap = open_snap(markets={"ETHUSDT": market(settled=[-0.0005] * 12, rate=-0.0005)})
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c4")
    e = only(plan, "EXIT")
    assert e.legs == ("spot", "perp") and e.perp_qty is None     # perp: reduceOnly, all
    assert "EARN_RETURN" not in kinds(plan)                         # after the legs settle


def test_after_exit_everything_idle_returns_to_earn():
    snap = snapshot(markets={"ETHUSDT": market(rate=0.00001)}, acct=account(usdt=95.0))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": dataclasses.replace(OPEN, status="OPEN")}, "c5")
    assert plan.book_updates["ETHUSDT"].status == "FLAT"
    assert only(plan, "EARN_RETURN").usdt == pytest.approx(95.0)


def test_buffer_stays_in_the_uta_while_a_position_is_open():
    snap = open_snap(acct=account(usdt=40.0, coins={"ETH": 0.03}))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c6")
    assert only(plan, "EARN_RETURN").usdt == pytest.approx(40.0 - 15)


def test_spot_leg_never_staked():
    snap = snapshot(markets={"ETHUSDT": market(rate=0.00001)}, acct=account(usdt=0.0, coins={"ETH": 5.0}))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {}, "c7")
    assert all(a.coin in (None, "USDT") for a in plan.actions)
    assert "EARN_RETURN" not in kinds(plan)


# --- §9 tests of this phase ------------------------------------------------------------ #

def test_no_funding_action_inside_settlement_window():
    snap = snapshot(markets={"ETHUSDT": market(minutes_to_settlement=10)}, acct=account(usdt=100.0))
    assert "ENTER" not in kinds(cp.plan_cycle(snap, cfg(), "NORMAL", {}, "c"))
    neg = open_snap(markets={"ETHUSDT": market(settled=[-0.0005] * 12, rate=-0.0005,
                                               minutes_to_settlement=10)})
    assert "EXIT" not in kinds(cp.plan_cycle(neg, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c"))


def test_risk_exit_allowed_inside_settlement_window():
    snap = open_snap(markets={"ETHUSDT": market(minutes_to_settlement=10)},
                     acct=account(usdt=16.0, coins={"ETH": 0.03}, mm_rate=0.65))
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c")
    assert only(plan, "EXIT").legs == ("perp", "spot")               # margin emergency: perp first
    assert any(a.startswith("MARGIN_EMERGENCY") for a in plan.alerts)


def test_unwind_order_spot_then_perp():
    plan = cp.plan_cycle(open_snap(), cfg(), "UNWIND", {"ETHUSDT": OPEN}, "c")
    assert only(plan, "EXIT").legs == ("spot", "perp")


def test_mmr_thresholds():
    warn = cp.plan_cycle(open_snap(acct=account(usdt=16.0, coins={"ETH": 0.03}, mm_rate=0.3)),
                         cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c")
    assert any(a.startswith("MARGIN_WARN") for a in warn.alerts) and "TRIM" not in kinds(warn)
    red = cp.plan_cycle(open_snap(acct=account(usdt=16.0, coins={"ETH": 0.03}, mm_rate=0.45)),
                        cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c")
    t = only(red, "TRIM")
    assert t.perp_qty == pytest.approx(0.01) and t.spot_qty == pytest.approx(0.01)  # half, on steps
    em = cp.plan_cycle(open_snap(acct=account(usdt=16.0, coins={"ETH": 0.03}, mm_rate=0.6)),
                       cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c")
    assert only(em, "EXIT").legs == ("perp", "spot")


def test_isolated_margin_blocks_entry():
    plan = cp.plan_cycle(snapshot(acct=account(usdt=100.0, margin_mode="ISOLATED_MARGIN")),
                         cfg(), "NORMAL", {}, "c")
    assert "ENTER" not in kinds(plan) and any(a.startswith("MARGIN_MODE") for a in plan.alerts)


def test_usdt_borrow_alerts_and_blocks():
    small = cp.plan_cycle(snapshot(acct=account(usdt=100.0, borrow=0.5)), cfg(MAX_USDT_BORROW_USD=1),
                          "NORMAL", {}, "c")
    assert any(a.startswith("USDT_BORROW:") for a in small.alerts) and "ENTER" in kinds(small)
    big = cp.plan_cycle(snapshot(acct=account(usdt=100.0, borrow=5)), cfg(), "NORMAL", {}, "c")
    assert any(a.startswith("USDT_BORROW_LIMIT") for a in big.alerts) and "ENTER" not in kinds(big)


def test_foreign_order_blocks_entries():
    plan = cp.plan_cycle(snapshot(acct=account(usdt=100.0), open_orders=(foreign_order(),)),
                         cfg(), "NORMAL", {}, "c")
    assert "ENTER" not in kinds(plan) and any(a.startswith("FOREIGN_ACTIVITY") for a in plan.alerts)


def test_region_restriction_error_sets_no_new_positions():
    plan = cp.plan_cycle(snapshot(acct=account(usdt=100.0), region=True), cfg(), "NORMAL", {}, "c")
    assert "ENTER" not in kinds(plan) and any(a.startswith("REGION_RESTRICTED") for a in plan.alerts)


def test_no_new_positions_allows_delta_reducing():
    snap = open_snap(acct=account(usdt=16.0, coins={"ETH": 0.036}))       # spot 20 % over the short
    plan = cp.plan_cycle(snap, cfg(), "NO_NEW_POSITIONS", {"ETHUSDT": OPEN}, "c")
    r = only(plan, "REBALANCE_TOWARD_NEUTRAL")
    assert r.legs == ("spot",) and r.spot_qty == pytest.approx(0.006)
    assert "ENTER" not in kinds(plan)


def test_perp_larger_than_spot_reduces_the_perp():
    snap = open_snap(acct=account(usdt=16.0, coins={"ETH": 0.02}), positions={"ETHUSDT": short(size=0.03)})
    r = only(cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c"), "REBALANCE_TOWARD_NEUTRAL")
    assert r.legs == ("perp",) and r.perp_qty == pytest.approx(0.01)


def test_adl_detected_flattens_orphan_spot():
    snap = open_snap(positions={"ETHUSDT": short(size=0.01)})             # short 0.03 -> 0.01, not ours
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c")
    assert any(a.startswith("ADL_DETECTED") for a in plan.alerts)
    r = only(plan, "REBALANCE_TOWARD_NEUTRAL")
    assert r.legs == ("spot",) and r.spot_qty == pytest.approx(0.02)


def test_liquidated_short_leaves_an_orphan_spot_sold_in_the_same_cycle():
    snap = open_snap(positions={"ETHUSDT": short(size=0.0)})
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c")
    e = only(plan, "EXIT")
    assert e.legs == ("spot",) and e.spot_qty == pytest.approx(0.03)
    assert any(a.startswith("ORPHAN_LEG") for a in plan.alerts)


def test_orphan_perp_is_closed_reduce_only():
    snap = open_snap(acct=account(usdt=16.0, coins={}))
    e = only(cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c"), "EXIT")
    assert e.legs == ("perp",) and e.perp_qty is None


def test_adl_rank_high_trims():
    snap = open_snap(positions={"ETHUSDT": short(size=0.03, adl=4)})
    assert "TRIM" in kinds(cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c"))


def test_unreadable_positions_no_entry_exits_reduce_only():
    snap = snapshot(acct=account(usdt=100.0), positions={}, errors={"positions:ETHUSDT": "x"})
    assert "ENTER" not in kinds(cp.plan_cycle(snap, cfg(), "NORMAL", {}, "c"))
    snap = snapshot(acct=account(usdt=16.0, coins={"ETH": 0.03}), positions={},
                    errors={"positions:ETHUSDT": "x"})
    e = only(cp.plan_cycle(snap, cfg(), "UNWIND", {"ETHUSDT": OPEN}, "c"), "EXIT")
    assert e.perp_qty is None                                              # close all, reduceOnly


def test_symbol_not_trading_is_exited():
    snap = open_snap(markets={"ETHUSDT": market(perp_status="Settling")})
    assert only(cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c"), "EXIT")


def test_spot_not_active_collateral_exits_and_blocks_entry():
    snap = open_snap(acct=account(usdt=16.0, coins={"ETH": 0.03}, collateral_active=False))
    assert "EXIT" in kinds(cp.plan_cycle(snap, cfg(), "NORMAL", {"ETHUSDT": OPEN}, "c"))
    flat = snapshot(acct=account(usdt=100.0, collateral_active=False))
    assert "ENTER" not in kinds(cp.plan_cycle(flat, cfg(), "NORMAL", {}, "c"))


def test_collateral_ratio_drop_blocks_entry():
    snap = snapshot(acct=account(usdt=100.0, collateral_ratio=0.8))
    book = {"ETHUSDT": SymbolBook(collateral_ratio=0.95)}
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", book, "c")
    assert "ENTER" not in kinds(plan) and any(a.startswith("CVR_DROP") for a in plan.alerts)


def test_untracked_short_is_adopted_and_reported():
    snap = open_snap()
    plan = cp.plan_cycle(snap, cfg(), "NORMAL", {}, "c")
    assert any(a.startswith("UNTRACKED_POSITION") for a in plan.alerts)
    assert plan.book_updates["ETHUSDT"].status == "OPEN" and "ENTER" not in kinds(plan)


def test_round_trip_limit_blocks_entry():
    trips = (NOW - 3 * 86_400_000, NOW - 2 * 86_400_000)
    plan = cp.plan_cycle(snapshot(acct=account(usdt=100.0)), cfg(), "NORMAL",
                         {"ETHUSDT": SymbolBook(entry_times=trips)}, "c")
    assert "ENTER" not in kinds(plan)


def test_capital_cap_counts_open_positions():
    snaps = {"ETHUSDT": market(), "BTCUSDT": market("BTCUSDT", price=65000.0)}
    c = cfg(SYMBOLS=["ETHUSDT", "BTCUSDT"], MAX_NOTIONAL_PER_SYMBOL_USD=42.5)
    snap = snapshot(markets=snaps, positions={"ETHUSDT": short(size=0.03), "BTCUSDT": short("BTCUSDT")},
                    acct=account(usdt=500.0, coins={"ETH": 0.03}))
    plan = cp.plan_cycle(snap, c, "NORMAL", {"ETHUSDT": OPEN}, "c")
    for a in plan.actions:
        if a.kind == "ENTER":
            assert a.usdt + 0.03 * 2500 <= c["TOTAL_CAPITAL_CAP_USD"] - c["USDT_BUFFER_USD"] + 1e-9


def test_every_action_is_permitted_by_the_state_matrix():
    from carry import state as cs
    for st in cs.STATES:
        plan = cp.plan_cycle(open_snap(), cfg(), st, {"ETHUSDT": OPEN}, "c")
        assert all(cs.is_allowed(st, a.kind) for a in plan.actions)
