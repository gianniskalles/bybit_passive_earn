"""carry/decide.py — the funding decision (CARRY_PLAN §4). PURE: no IO, no
clock, no randomness. The calibration (tools/carry_calibrate.py) and, from
Phase 3, the production cycle call exactly this function.

Per symbol and per cycle it answers one of:
  ENTER     open the neutral position (long spot + short perp)
  HOLD      keep it
  EXIT      close it (kind "funding" or "risk")
  STAY_OUT  capital stays in layer A

Rules, in order:
  1. Risk first: a risk exit (or UNWIND) closes a held position regardless of
     the settlement window or MIN_HOLD. Exits are always allowed.
  2. No funding-motivated action within NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN
     of the next settlement (Bybit does not guarantee inclusion/exclusion
     around it); an unknown settlement time counts as inside the window.
  3. Unknown = no new exposure: any input needed for an entry that is None
     means STAY_OUT.
  4. The funding interval is read, never assumed; a change of interval means
     no entry for that symbol in that cycle.
  5. Entry (NORMAL only):
       predicted >= ENTRY_MIN_PREDICTED_RATE
       smoothed * periods_per_year - layer_A_apr >= ENTRY_MIN_EXPECTED_APR
       (smoothed - layer_A per period) * periods in MIN_HOLD >= round trip cost
       |basis| <= MAX_ENTRY_BASIS_BPS, spread <= MAX_SPREAD_BPS (when set)
       round trips in the last 30 days < MAX_ROUND_TRIPS_PER_30D
  6. Funding exit (after MIN_HOLD, outside the window) only if
       expected income over EXIT_HORIZON_HOURS < -(round trip cost), or
       smoothed < 0 and predicted < EXIT_PREDICTED_FLOOR.
     A single negative settlement never triggers it.
Hysteresis: ENTRY_MIN_PREDICTED_RATE must exceed EXIT_PREDICTED_FLOOR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

MINUTES_PER_YEAR = 365 * 24 * 60
RISK_STATES = ("NORMAL", "NO_NEW_POSITIONS", "UNWIND")


@dataclass(frozen=True)
class Params:
    entry_min_expected_apr: float
    entry_min_predicted_rate: float
    exit_predicted_floor: float
    exit_horizon_hours: float
    smoothing_settlements: int
    min_hold_hours: float
    max_round_trips_30d: int
    no_funding_action_before_settlement_min: float
    max_entry_basis_bps: Optional[float]
    max_spread_bps: Optional[float]
    spot_taker_fee: float
    perp_taker_fee: float

    def __post_init__(self):
        if self.entry_min_predicted_rate <= self.exit_predicted_floor:
            raise ValueError("hysteresis: ENTRY_MIN_PREDICTED_RATE must exceed EXIT_PREDICTED_FLOOR")
        if not isinstance(self.smoothing_settlements, int) or self.smoothing_settlements < 1:
            raise ValueError("SMOOTHING_SETTLEMENTS must be an integer >= 1")
        if not isinstance(self.max_round_trips_30d, int) or self.max_round_trips_30d < 0:
            raise ValueError("MAX_ROUND_TRIPS_PER_30D must be an integer >= 0")
        for name in ("exit_horizon_hours", "min_hold_hours", "no_funding_action_before_settlement_min",
                     "spot_taker_fee", "perp_taker_fee"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")

    @property
    def leg_pair_cost(self) -> float:
        """One direction (open or close): spot + perp taker fee."""
        return self.spot_taker_fee + self.perp_taker_fee

    @property
    def round_trip_cost(self) -> float:
        return 2 * self.leg_pair_cost


@dataclass(frozen=True)
class FundingView:
    symbol: str
    now_ms: int
    next_funding_time_ms: Optional[int]
    funding_interval_min: Optional[float]
    previous_interval_min: Optional[float]   # as seen by the previous cycle; None = unknown
    predicted_rate: Optional[float]          # rate of the upcoming settlement
    settled_rates: Tuple[float, ...]         # oldest first, most recent last
    layer_a_apr: Optional[float]
    basis_bps: Optional[float] = None
    spread_bps: Optional[float] = None


@dataclass(frozen=True)
class PositionView:
    in_position: bool
    entered_ms: Optional[int]
    round_trips_30d: int


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: str                   # ENTER | HOLD | EXIT | STAY_OUT
    kind: Optional[str]           # "funding" | "risk" | None
    reason: str
    expected_apr: Optional[float] = field(default=None, compare=True)
    smoothed_rate: Optional[float] = None


def _smoothed(rates: Tuple[float, ...], n: int) -> Optional[float]:
    if len(rates) < n:
        return None
    window = rates[-n:]
    return sum(window) / n


def decide_symbol(view: FundingView, pos: PositionView, p: Params,
                  risk_state: str = "NORMAL",
                  risk_exit_reason: Optional[str] = None) -> Decision:
    sym = view.symbol
    if risk_state not in RISK_STATES:
        risk_state = "NO_NEW_POSITIONS"   # unknown state: more conservative, never less

    interval = view.funding_interval_min
    ppy = MINUTES_PER_YEAR / interval if interval else None
    smoothed = _smoothed(view.settled_rates, p.smoothing_settlements)
    expected_apr = smoothed * ppy if smoothed is not None and ppy else None

    def out(action, kind, reason):
        return Decision(sym, action, kind, reason, expected_apr, smoothed)

    # 1. Risk first: exits are always allowed.
    if pos.in_position and (risk_exit_reason or risk_state == "UNWIND"):
        return out("EXIT", "risk", f"risk exit: {risk_exit_reason or 'risk_state UNWIND'}")
    if not pos.in_position and risk_state != "NORMAL":
        return out("STAY_OUT", None, f"risk_state {risk_state}: no new exposure")

    # 2. Settlement window (unknown time counts as inside).
    if view.next_funding_time_ms is None:
        return out("HOLD" if pos.in_position else "STAY_OUT", None,
                   "next settlement time unknown: no funding action")
    minutes_left = (view.next_funding_time_ms - view.now_ms) / 60000
    if minutes_left < p.no_funding_action_before_settlement_min:
        return out("HOLD" if pos.in_position else "STAY_OUT", None,
                   f"inside the settlement window ({minutes_left:.1f} min left): no funding action")

    if pos.in_position:
        return _exit_rule(view, pos, p, smoothed, out)
    return _entry_rule(view, pos, p, smoothed, ppy, expected_apr, out)


def _exit_rule(view, pos, p, smoothed, out) -> Decision:
    if pos.entered_ms is None:
        return out("HOLD", None, "entry time unknown: MIN_HOLD cannot be proven over")
    held_h = (view.now_ms - pos.entered_ms) / 3_600_000
    if held_h < p.min_hold_hours:
        return out("HOLD", None, f"held {held_h:.1f} h < MIN_HOLD_HOURS {p.min_hold_hours}")
    interval = view.funding_interval_min
    if smoothed is None or not interval:
        return out("HOLD", None, "smoothed funding or interval unknown: no funding exit")
    horizon_periods = p.exit_horizon_hours * 60 / interval
    expected_income = smoothed * horizon_periods
    if -expected_income > p.round_trip_cost:
        return out("EXIT", "funding",
                   f"expected income over {p.exit_horizon_hours} h = {expected_income:.5f} "
                   f"< -round trip {p.round_trip_cost:.5f}")
    if (smoothed < 0 and view.predicted_rate is not None
            and view.predicted_rate < p.exit_predicted_floor):
        return out("EXIT", "funding",
                   f"smoothed {smoothed:.6f} < 0 and predicted {view.predicted_rate:.6f} "
                   f"< EXIT_PREDICTED_FLOOR {p.exit_predicted_floor}")
    return out("HOLD", None, f"smoothed {smoothed:.6f}, predicted {view.predicted_rate}")


def _entry_rule(view, pos, p, smoothed, ppy, expected_apr, out) -> Decision:
    interval = view.funding_interval_min
    if not interval:
        return out("STAY_OUT", None, "funding interval unknown")
    if view.previous_interval_min is not None and view.previous_interval_min != interval:
        return out("STAY_OUT", None, f"funding interval changed "
                                     f"({view.previous_interval_min} -> {interval} min)")
    if pos.round_trips_30d >= p.max_round_trips_30d:
        return out("STAY_OUT", None, f"{pos.round_trips_30d} round trips in 30 days "
                                     f">= MAX_ROUND_TRIPS_PER_30D {p.max_round_trips_30d}")
    if view.predicted_rate is None or smoothed is None or view.layer_a_apr is None:
        return out("STAY_OUT", None, "predicted rate, smoothed funding or layer A APR unknown")
    if view.predicted_rate < p.entry_min_predicted_rate:
        return out("STAY_OUT", None, f"predicted {view.predicted_rate:.6f} "
                                     f"< ENTRY_MIN_PREDICTED_RATE {p.entry_min_predicted_rate}")
    excess = expected_apr - view.layer_a_apr
    if excess < p.entry_min_expected_apr:
        return out("STAY_OUT", None, f"expected APR {expected_apr:.4f} - layer A "
                                     f"{view.layer_a_apr:.4f} = {excess:.4f} "
                                     f"< ENTRY_MIN_EXPECTED_APR {p.entry_min_expected_apr}")
    hold_periods = p.min_hold_hours * 60 / interval
    net_per_period = smoothed - view.layer_a_apr / ppy
    if net_per_period * hold_periods < p.round_trip_cost:
        return out("STAY_OUT", None, f"MIN_HOLD income {net_per_period * hold_periods:.5f} "
                                     f"does not pay back the round trip {p.round_trip_cost:.5f}")
    if p.max_entry_basis_bps is not None:
        if view.basis_bps is None or abs(view.basis_bps) > p.max_entry_basis_bps:
            return out("STAY_OUT", None, f"basis {view.basis_bps} bps outside "
                                         f"±{p.max_entry_basis_bps}")
    if p.max_spread_bps is not None:
        if view.spread_bps is None or view.spread_bps > p.max_spread_bps:
            return out("STAY_OUT", None, f"spread {view.spread_bps} bps > {p.max_spread_bps}")
    return out("ENTER", "funding", f"expected APR {expected_apr:.4f} beats layer A "
                                   f"{view.layer_a_apr:.4f} by {excess:.4f}; MIN_HOLD pays back "
                                   f"the round trip")
