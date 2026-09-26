"""CARRY_PLAN Phase 0B — carry/decide.py, the pure funding decision (§4).

The calibration and (from Phase 3) the production cycle call the same
decide_symbol(); these tests pin the §4 rules on it. No IO anywhere.
"""

import dataclasses

import pytest

from carry.decide import Decision, FundingView, Params, PositionView, decide_symbol

H = 3600 * 1000
MIN = 60 * 1000
NOW = 1_790_000_000_000


def params(**over):
    base = dict(
        entry_min_expected_apr=0.03,
        entry_min_predicted_rate=0.0001,
        exit_predicted_floor=-0.00005,
        exit_horizon_hours=72,
        smoothing_settlements=9,
        min_hold_hours=168,
        max_round_trips_30d=4,
        no_funding_action_before_settlement_min=15,
        max_entry_basis_bps=None,
        max_spread_bps=None,
        spot_taker_fee=0.001,
        perp_taker_fee=0.00055,
    )
    base.update(over)
    return Params(**base)


def view(minutes_to_settlement=120, predicted=0.0001, settled=(0.0001,) * 9, interval=480,
         previous_interval=480, layer_a=0.02, **over):
    return FundingView(symbol="ETHUSDT", now_ms=NOW,
                       next_funding_time_ms=NOW + minutes_to_settlement * MIN,
                       funding_interval_min=interval, previous_interval_min=previous_interval,
                       predicted_rate=predicted, settled_rates=tuple(settled),
                       layer_a_apr=layer_a, **over)


OUT = PositionView(in_position=False, entered_ms=None, round_trips_30d=0)


def held(hours=200):
    return PositionView(in_position=True, entered_ms=NOW - hours * H, round_trips_30d=1)


# --- entry ----------------------------------------------------------------- #

def test_enters_when_carry_clearly_beats_layer_a():
    d = decide_symbol(view(predicted=0.0002, settled=(0.0002,) * 9), OUT, params())
    assert d.action == "ENTER" and d.kind == "funding"


def test_no_entry_when_excess_over_layer_a_too_small():
    # 0.0001 * 1095 = 10.95% APR, layer A 10% -> excess ~0.95% < 3%
    d = decide_symbol(view(layer_a=0.10), OUT, params())
    assert d.action == "STAY_OUT"


def test_no_entry_when_round_trip_is_not_paid_back_within_min_hold():
    # very short min hold: 0.31% round trip amortised over 8 h is huge
    d = decide_symbol(view(predicted=0.0002, settled=(0.0002,) * 9), OUT,
                      params(min_hold_hours=8))
    assert d.action == "STAY_OUT" and "round trip" in d.reason


@pytest.mark.parametrize("risk_state", ["NO_NEW_POSITIONS", "UNWIND"])
def test_no_entry_outside_normal(risk_state):
    d = decide_symbol(view(predicted=0.0003, settled=(0.0003,) * 9), OUT, params(),
                      risk_state=risk_state)
    assert d.action == "STAY_OUT"


def test_round_trip_limit_blocks_entry():
    pos = PositionView(in_position=False, entered_ms=None, round_trips_30d=4)
    d = decide_symbol(view(predicted=0.0003, settled=(0.0003,) * 9), pos, params())
    assert d.action == "STAY_OUT" and "round trips" in d.reason


@pytest.mark.parametrize("field", ["predicted_rate", "funding_interval_min",
                                   "next_funding_time_ms", "layer_a_apr"])
def test_unknown_input_means_no_entry(field):
    v = dataclasses.replace(view(predicted=0.0003, settled=(0.0003,) * 9), **{field: None})
    assert decide_symbol(v, OUT, params()).action == "STAY_OUT"


def test_not_enough_settlements_to_smooth_means_no_entry():
    d = decide_symbol(view(predicted=0.0003, settled=(0.0003,) * 3), OUT, params())
    assert d.action == "STAY_OUT"


def test_entry_basis_and_spread_limits():
    p = params(max_entry_basis_bps=10, max_spread_bps=5)
    good = dict(predicted=0.0003, settled=(0.0003,) * 9)
    assert decide_symbol(view(basis_bps=3, spread_bps=1, **good), OUT, p).action == "ENTER"
    assert decide_symbol(view(basis_bps=-12, spread_bps=1, **good), OUT, p).action == "STAY_OUT"
    assert decide_symbol(view(basis_bps=3, spread_bps=9, **good), OUT, p).action == "STAY_OUT"
    assert decide_symbol(view(basis_bps=None, spread_bps=1, **good), OUT, p).action == "STAY_OUT"


# --- §9 tests --------------------------------------------------------------- #

def test_single_negative_funding_does_not_exit():
    settled = (0.0001,) * 8 + (-0.0002,)
    d = decide_symbol(view(predicted=-0.0001, settled=settled), held(), params())
    assert d.action == "HOLD"


def test_sustained_negative_funding_exits_before_settlement():
    settled = (-0.0001,) * 9
    d = decide_symbol(view(minutes_to_settlement=20, predicted=-0.0002, settled=settled),
                      held(), params())
    assert d.action == "EXIT" and d.kind == "funding"
    # the same situation inside the window waits for the next one
    d = decide_symbol(view(minutes_to_settlement=14, predicted=-0.0002, settled=settled),
                      held(), params())
    assert d.action == "HOLD" and "window" in d.reason


def test_no_funding_action_inside_settlement_window():
    good = dict(predicted=0.0003, settled=(0.0003,) * 9)
    assert decide_symbol(view(minutes_to_settlement=10, **good), OUT, params()).action == "STAY_OUT"
    assert decide_symbol(view(minutes_to_settlement=16, **good), OUT, params()).action == "ENTER"
    bad = dict(predicted=-0.0005, settled=(-0.0005,) * 9)
    assert decide_symbol(view(minutes_to_settlement=10, **bad), held(), params()).action == "HOLD"


def test_risk_exit_allowed_inside_settlement_window():
    d = decide_symbol(view(minutes_to_settlement=10), held(hours=1), params(),
                      risk_exit_reason="MMR_EMERGENCY")
    assert d.action == "EXIT" and d.kind == "risk"


def test_unwind_exits_regardless_of_window_and_hold():
    d = decide_symbol(view(minutes_to_settlement=5), held(hours=1), params(), risk_state="UNWIND")
    assert d.action == "EXIT" and d.kind == "risk"


def test_funding_interval_read_not_assumed():
    p = params()
    v8 = view(predicted=0.0001, settled=(0.0001,) * 9, interval=480, previous_interval=480)
    v1 = view(predicted=0.0001, settled=(0.0001,) * 9, interval=60, previous_interval=60)
    # same per-settlement rate, 8x the settlements per year
    assert decide_symbol(v1, OUT, p).expected_apr == pytest.approx(
        8 * decide_symbol(v8, OUT, p).expected_apr)
    changed = view(predicted=0.0003, settled=(0.0003,) * 9, interval=60, previous_interval=480)
    d = decide_symbol(changed, OUT, p)
    assert d.action == "STAY_OUT" and "interval changed" in d.reason


# --- exit -------------------------------------------------------------------- #

def test_min_hold_blocks_funding_exit_only():
    settled = (-0.0003,) * 9
    v = view(predicted=-0.0003, settled=settled)
    assert decide_symbol(v, held(hours=10), params()).action == "HOLD"
    assert decide_symbol(v, held(hours=10), params(), risk_exit_reason="ADL").action == "EXIT"


def test_expected_negative_income_beyond_round_trip_exits():
    # smoothed -0.0002 over 72 h (9 settlements) = -0.18%... below 0.31% -> hold
    v = view(predicted=0.0, settled=(-0.0002,) * 9)
    assert decide_symbol(v, held(), params(exit_predicted_floor=-0.01)).action == "HOLD"
    # over 168 h (21 settlements) = -0.42% > 0.31% -> exit
    assert decide_symbol(v, held(), params(exit_predicted_floor=-0.01,
                                           exit_horizon_hours=168)).action == "EXIT"


def test_hysteresis_is_enforced():
    with pytest.raises(ValueError):
        params(entry_min_predicted_rate=0.0, exit_predicted_floor=0.0001)


def test_params_reject_nonsense():
    for bad in (dict(smoothing_settlements=0), dict(min_hold_hours=-1),
                dict(spot_taker_fee=-0.1), dict(no_funding_action_before_settlement_min=-5)):
        with pytest.raises(ValueError):
            params(**bad)


def test_decision_is_pure():
    v, p = view(predicted=0.0003, settled=(0.0003,) * 9), params()
    assert decide_symbol(v, OUT, p) == decide_symbol(v, OUT, p)
    assert isinstance(decide_symbol(v, OUT, p), Decision)
