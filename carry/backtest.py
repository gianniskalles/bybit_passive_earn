"""carry/backtest.py — replay settled funding through the PRODUCTION decision
(carry.decide.decide_symbol) to measure whether the carry is worth it
(CARRY_PLAN Phase 0B).

Model, per unit of notional:
  - A decision is taken once per settlement, just outside the no-action
    window (NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN + 1 min before it) — the
    last moment the production cycle may act for funding reasons.
  - Entering or exiting costs spot + perp taker fee each (× cost_multiplier).
  - In position, each settlement pays the settled rate to the short.
  - Out of position, capital earns layer A (Easy Earn APR, step function
    over the recorded APR history) for the interval.
  - predictor "oracle": the predicted rate equals the rate that settled (the
    predicted rate 15' before a settlement is close to final);
    "lagged": the previous settled rate (conservative).
Not modelled: basis at entry/exit, slippage, spread (fees only; the ±50%
cost sensitivity is the stand-in), intra-interval exits.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from carry import decide as _decide
from carry.decide import MINUTES_PER_YEAR, FundingView, Params, PositionView

DAY_MS = 24 * 3600 * 1000
MIN_MS = 60 * 1000
PREDICTORS = ("oracle", "lagged")


def layer_a_at(points: Sequence[Tuple[int, float]], ts: int) -> Optional[float]:
    """APR in force at ts (step function). Before the first point: the first
    value (the report states how much of the period that covers)."""
    if not points:
        return None
    i = bisect.bisect_right([p[0] for p in points], ts) - 1
    return points[max(i, 0)][1]


@dataclass
class Result:
    predictor: str
    settlements: int = 0
    entries: int = 0
    exits: int = 0
    costs: float = 0.0
    total_return: float = 0.0
    layer_a_return: float = 0.0
    minutes: float = 0.0
    minutes_in_position: float = 0.0
    negative_count: int = 0
    longest_negative_minutes: float = 0.0
    daily: Dict[int, float] = field(default_factory=dict)
    reasons_seen: set = field(default_factory=set)
    predicted_used: List[Optional[float]] = field(default_factory=list)

    @property
    def days(self) -> float:
        return self.minutes / 1440

    @property
    def net_apr(self) -> float:
        return self.total_return * 365 / self.days if self.days else 0.0

    @property
    def layer_a_apr(self) -> float:
        return self.layer_a_return * 365 / self.days if self.days else 0.0

    @property
    def excess_apr(self) -> float:
        return self.net_apr - self.layer_a_apr

    @property
    def time_in_position(self) -> float:
        return self.minutes_in_position / self.minutes if self.minutes else 0.0

    @property
    def negative_share(self) -> float:
        return self.negative_count / self.settlements if self.settlements else 0.0

    @property
    def longest_negative_days(self) -> float:
        return self.longest_negative_minutes / 1440

    @property
    def worst_30d_return(self) -> float:
        return worst_window(self.daily, 30)

    def summary(self) -> Dict:
        return {"predictor": self.predictor, "days": round(self.days, 2),
                "net_apr": self.net_apr, "layer_a_apr": self.layer_a_apr,
                "excess_apr": self.excess_apr, "entries": self.entries, "exits": self.exits,
                "round_trips": min(self.entries, self.exits), "costs": self.costs,
                "time_in_position": self.time_in_position,
                "worst_30d_return": self.worst_30d_return,
                "negative_share": self.negative_share,
                "longest_negative_days": self.longest_negative_days}


def worst_window(daily: Dict[int, float], days: int) -> float:
    """Minimum sum over any `days` consecutive days (the whole period if shorter)."""
    if not daily:
        return 0.0
    first, last = min(daily), max(daily)
    values = [daily.get(d, 0.0) for d in range(first, last + DAY_MS, DAY_MS)]
    if len(values) <= days:
        return sum(values)
    window = sum(values[:days])
    worst = window
    for i in range(days, len(values)):
        window += values[i] - values[i - days]
        worst = min(worst, window)
    return worst


def simulate(funding: Sequence[Tuple[int, float]], layer_a: Sequence[Tuple[int, float]],
             params: Params, predictor: str = "lagged", cost_multiplier: float = 1.0,
             symbol: str = "SYM", eval_from_ms: Optional[int] = None,
             eval_to_ms: Optional[int] = None) -> Result:
    """Replay `funding` [(settlement_ms, rate)] (oldest first).

    Decisions use all history; PnL is counted only for settlements in
    [eval_from_ms, eval_to_ms), and the position starts flat at eval_from_ms
    (a cold start for out-of-sample checks)."""
    if predictor not in PREDICTORS:
        raise ValueError(f"predictor must be one of {PREDICTORS}")
    res = Result(predictor=predictor)
    series = list(funding)
    n = params.smoothing_settlements
    default_gap = ((series[1][0] - series[0][0]) / MIN_MS) if len(series) > 1 else 480.0
    in_pos, entered, entry_times = False, None, []
    neg_run = 0.0
    started = eval_from_ms is None

    def book(ts: int, amount: float) -> None:
        day = ts - ts % DAY_MS
        res.daily[day] = res.daily.get(day, 0.0) + amount
        res.total_return += amount

    for i, (ts, rate) in enumerate(series):
        if eval_to_ms is not None and ts >= eval_to_ms:
            break
        if not started and ts >= eval_from_ms:
            started, in_pos, entered, entry_times = True, False, None, []

        interval = (ts - series[i - 1][0]) / MIN_MS if i >= 1 else None
        previous = (series[i - 1][0] - series[i - 2][0]) / MIN_MS if i >= 2 else None
        predicted = rate if predictor == "oracle" else (series[i - 1][1] if i >= 1 else None)
        decision_ms = ts - int((params.no_funding_action_before_settlement_min + 1) * MIN_MS)
        a = layer_a_at(layer_a, ts)
        view = FundingView(symbol=symbol, now_ms=decision_ms, next_funding_time_ms=ts,
                           funding_interval_min=interval, previous_interval_min=previous,
                           predicted_rate=predicted,
                           settled_rates=tuple(r for _, r in series[max(0, i - n):i]),
                           layer_a_apr=a)
        recent = sum(1 for t in entry_times if decision_ms - 30 * DAY_MS < t <= decision_ms)
        d = _decide.decide_symbol(view, PositionView(in_pos, entered, recent), params)

        if not started:
            # warm-up before the evaluation window: follow the rules, count nothing
            if d.action == "ENTER":
                in_pos, entered = True, decision_ms
            elif d.action == "EXIT":
                in_pos, entered = False, None
            continue

        res.settlements += 1
        res.predicted_used.append(predicted)
        res.reasons_seen.add(d.reason)
        if d.action in ("ENTER", "EXIT"):
            cost = params.leg_pair_cost * cost_multiplier
            res.costs += cost
            book(decision_ms, -cost)
            if d.action == "ENTER":
                in_pos, entered = True, decision_ms
                entry_times.append(decision_ms)
                res.entries += 1
            else:
                in_pos, entered = False, None
                res.exits += 1

        period = interval if interval else default_gap
        a_accrual = (a or 0.0) * period / MINUTES_PER_YEAR
        res.layer_a_return += a_accrual
        res.minutes += period
        if in_pos:
            book(ts, rate)
            res.minutes_in_position += period
        else:
            book(ts, a_accrual)
        if rate < 0:
            res.negative_count += 1
            neg_run += period
            res.longest_negative_minutes = max(res.longest_negative_minutes, neg_run)
        else:
            neg_run = 0.0
    return res


def combine(results: Sequence[Result]) -> Result:
    """Equal-weight portfolio of per-symbol results (capital split evenly)."""
    k = len(results)
    out = Result(predictor=results[0].predictor)
    for r in results:
        for day, v in r.daily.items():
            out.daily[day] = out.daily.get(day, 0.0) + v / k
        out.total_return += r.total_return / k
        out.layer_a_return += r.layer_a_return / k
        out.costs += r.costs / k
        out.entries += r.entries
        out.exits += r.exits
        out.settlements += r.settlements
        out.negative_count += r.negative_count
        out.minutes_in_position += r.minutes_in_position / k
        out.longest_negative_minutes = max(out.longest_negative_minutes, r.longest_negative_minutes)
    out.minutes = sum(r.minutes for r in results) / k
    return out


def go_check(base: Result, costs_up: Result, min_excess: float = 0.03,
             worst_30d_floor: float = -0.005) -> Dict:
    """CARRY_PLAN §2 GO criteria."""
    excess_ok = base.excess_apr >= min_excess
    worst_ok = base.worst_30d_return >= worst_30d_floor
    robust = costs_up.excess_apr >= min_excess and costs_up.worst_30d_return >= worst_30d_floor
    return {"excess_apr": base.excess_apr, "excess_ok": excess_ok,
            "worst_30d_return": base.worst_30d_return, "worst_30d_ok": worst_ok,
            "excess_apr_costs_x1_5": costs_up.excess_apr,
            "worst_30d_costs_x1_5": costs_up.worst_30d_return,
            "robust_to_costs": robust, "go": bool(excess_ok and worst_ok and robust)}


def param_grid(base: Dict) -> Iterable[Params]:
    """The candidate thresholds (⊙ in CARRY_PLAN §8)."""
    for entry in (0.00005, 0.0001, 0.00015):
        for floor in (-0.0001, -0.00005, 0.0):
            if floor >= entry:
                continue
            for n in (3, 9, 21):
                for hold in (72, 168, 336):
                    for horizon in (72, 168):
                        for trips in (2, 4):
                            yield Params(**{**base, "entry_min_predicted_rate": entry,
                                            "exit_predicted_floor": floor,
                                            "smoothing_settlements": n,
                                            "min_hold_hours": hold,
                                            "exit_horizon_hours": horizon,
                                            "max_round_trips_30d": trips})
