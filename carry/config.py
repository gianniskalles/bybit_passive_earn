"""carry/config.py — strict schema for config/carry.yaml (CARRY_PLAN §8, §13).

validate(cfg, testnet) returns every problem as "KEY: message"; [] means the
config can run. The cycle refuses to start on any problem (load() raises).

Three layers:
  1. Types and ranges for every field; a missing or null field is an error
     ("unset" — rule 7: unknown is never guessed).
  2. Relations: hysteresis (entry > exit floor), MMR_WARN < MMR_REDUCE <
     MMR_EMERGENCY, per-symbol notional <= total cap.
  3. Production floors (decision 13.1: no threshold is loosened to make the
     strategy trade). Only a config with TESTNET_ONLY: true may go below
     them, and such a config is refused unless BYBIT_TESTNET is set.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import yaml

from carry.decide import Params

ALLOWED_SYMBOLS = ("BTCUSDT", "ETHUSDT")   # decision Δ2; widening needs a new measurement


class CarryConfigError(ValueError):
    pass


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _between(lo, hi, lo_incl=True, hi_incl=True):
    def check(v):
        return _num(v) and (v >= lo if lo_incl else v > lo) and (v <= hi if hi_incl else v < hi)
    return check


_pos = lambda v: _num(v) and v > 0  # noqa: E731
_nonneg = lambda v: _num(v) and v >= 0  # noqa: E731

# key -> (check, description)
SCHEMA: Dict[str, Tuple[Callable[[Any], bool], str]] = {
    "SYMBOLS": (lambda v: isinstance(v, list) and bool(v) and len(set(v)) == len(v)
                and all(s in ALLOWED_SYMBOLS for s in v),
                f"non-empty list of distinct symbols from {list(ALLOWED_SYMBOLS)}"),
    "CYCLE_MINUTES": (_pos, "number > 0"),
    "DRY_RUN": (lambda v: isinstance(v, bool), "boolean"),
    "LOG_DIR": (lambda v: isinstance(v, str) and bool(v.strip()), "non-empty path"),
    "ACCOUNT_TYPE": (lambda v: v == "UNIFIED", "UNIFIED"),
    "EARN_COIN": (lambda v: v == "USDT", "USDT (layer A: Flexible Easy Earn)"),
    "SPOT_TAKER_FEE": (_between(0, 0.01), "number in [0, 0.01]"),
    "PERP_TAKER_FEE": (_between(0, 0.01), "number in [0, 0.01]"),
    "ENTRY_MIN_EXPECTED_APR": (_num, "number"),
    "ENTRY_MIN_PREDICTED_RATE": (_num, "number"),
    "ENTRY_EV_MULTIPLE": (_nonneg, "number >= 0"),
    "EXIT_PREDICTED_FLOOR": (_num, "number"),
    "EXIT_HORIZON_HOURS": (_pos, "number > 0"),
    "SMOOTHING_SETTLEMENTS": (lambda v: _int(v) and v >= 1, "integer >= 1"),
    "MIN_HOLD_HOURS": (_nonneg, "number >= 0"),
    "MAX_ROUND_TRIPS_PER_30D": (lambda v: _int(v) and v >= 0, "integer >= 0"),
    "NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN": (_nonneg, "number >= 0"),
    "MAX_ENTRY_BASIS_BPS": (_pos, "number > 0"),
    "MAX_SPREAD_BPS": (_pos, "number > 0"),
    "TOTAL_CAPITAL_CAP_USD": (_pos, "number > 0 (decision Δ3)"),
    "MAX_NOTIONAL_PER_SYMBOL_USD": (_pos, "number > 0"),
    "USDT_BUFFER_USD": (_nonneg, "number >= 0 (stays in the UTA while a position is open)"),
    "MAX_USDT_BORROW_USD": (_nonneg, "number >= 0"),
    "MAX_HEDGE_DRIFT_PCT": (_between(0, 100, lo_incl=False), "number in (0, 100]"),
    "LEG_TIMEOUT_S": (_pos, "number > 0"),
    "REDEEM_TIMEOUT_HOURS": (_pos, "number > 0"),
    "MMR_WARN": (_between(0, 1, False, False), "number in (0, 1)"),
    "MMR_REDUCE": (_between(0, 1, False, False), "number in (0, 1)"),
    "MMR_EMERGENCY": (_between(0, 1, False, False), "number in (0, 1)"),
    "ADL_RANK_REDUCE": (lambda v: _int(v) and 1 <= v <= 5, "integer 1..5"),
    "CVR_DROP_ALERT": (_between(0, 1, False), "number in (0, 1]"),
    "UNDERPERFORMANCE_RATIO": (_between(0, 1, False), "number in (0, 1]"),
    "DEADMAN_URL": (lambda v: isinstance(v, str) and re.fullmatch(r"https://\S+", v) is not None,
                    "https:// URL of the external dead-man check (R33)"),
    "CALIBRATION_SOURCE": (lambda v: isinstance(v, str) and bool(v.strip()),
                           "where the ⊙ thresholds come from"),
}
OPTIONAL = {"TESTNET_ONLY": (lambda v: isinstance(v, bool), "boolean")}

# Floors a production (non-TESTNET_ONLY) config may never go below.
PRODUCTION_FLOORS: Dict[str, Tuple[Callable[[Any], bool], str]] = {
    "NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN": (lambda v: v >= 15, ">= 15 (Bybit ±5 s rule, §4)"),
    "ENTRY_EV_MULTIPLE": (lambda v: v >= 1, ">= 1 (MIN_HOLD must repay a round trip)"),
    "ENTRY_MIN_EXPECTED_APR": (lambda v: v >= 0, ">= 0"),
    "ENTRY_MIN_PREDICTED_RATE": (lambda v: v > 0, "> 0"),
    "MIN_HOLD_HOURS": (lambda v: v >= 8, ">= 8 (at least one settlement)"),
}


def validate(cfg: Any, testnet: bool) -> List[str]:
    if not isinstance(cfg, dict):
        return ["CONFIG: not a mapping"]
    errors: List[str] = []
    testnet_only = cfg.get("TESTNET_ONLY") is True
    for key, (check, desc) in SCHEMA.items():
        if key == "DEADMAN_URL" and testnet_only and cfg.get(key) is None:
            continue  # the forced testnet cycle is supervised by hand
        if key not in cfg or cfg[key] is None:
            errors.append(f"{key}: unset — {desc}")
        elif not check(cfg[key]):
            errors.append(f"{key}: {cfg[key]!r} is not {desc}")
    for key, (check, desc) in OPTIONAL.items():
        if key in cfg and not check(cfg[key]):
            errors.append(f"{key}: {cfg[key]!r} is not {desc}")
    bad = {e.split(":", 1)[0] for e in errors}

    def ok(*keys):
        return all(k not in bad and cfg.get(k) is not None for k in keys)

    if ok("ENTRY_MIN_PREDICTED_RATE", "EXIT_PREDICTED_FLOOR") and \
            cfg["ENTRY_MIN_PREDICTED_RATE"] <= cfg["EXIT_PREDICTED_FLOOR"]:
        errors.append("ENTRY_MIN_PREDICTED_RATE: must exceed EXIT_PREDICTED_FLOOR (hysteresis)")
    if ok("MMR_WARN", "MMR_REDUCE", "MMR_EMERGENCY") and not (
            cfg["MMR_WARN"] < cfg["MMR_REDUCE"] < cfg["MMR_EMERGENCY"]):
        errors.append("MMR_WARN: must satisfy MMR_WARN < MMR_REDUCE < MMR_EMERGENCY")
    if ok("MAX_NOTIONAL_PER_SYMBOL_USD", "TOTAL_CAPITAL_CAP_USD") and \
            cfg["MAX_NOTIONAL_PER_SYMBOL_USD"] > cfg["TOTAL_CAPITAL_CAP_USD"]:
        errors.append("MAX_NOTIONAL_PER_SYMBOL_USD: must be <= TOTAL_CAPITAL_CAP_USD")

    if testnet_only and not testnet:
        errors.append("TESTNET_ONLY: this config lowers entry thresholds for a forced testnet "
                      "cycle and is refused unless BYBIT_TESTNET is set")
    if not testnet_only:
        for key, (check, desc) in PRODUCTION_FLOORS.items():
            if ok(key) and not check(cfg[key]):
                errors.append(f"{key}: {cfg[key]!r} below the production floor {desc}")
    return errors


def load(path: Path, testnet: bool) -> Dict:
    try:
        cfg = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise CarryConfigError(f"{path}: unreadable: {e}") from e
    errors = validate(cfg, testnet)
    if errors:
        raise CarryConfigError(f"{path}: " + "; ".join(errors))
    return cfg


def to_params(cfg: Dict) -> Params:
    return Params(
        entry_min_expected_apr=cfg["ENTRY_MIN_EXPECTED_APR"],
        entry_min_predicted_rate=cfg["ENTRY_MIN_PREDICTED_RATE"],
        exit_predicted_floor=cfg["EXIT_PREDICTED_FLOOR"],
        exit_horizon_hours=cfg["EXIT_HORIZON_HOURS"],
        smoothing_settlements=cfg["SMOOTHING_SETTLEMENTS"],
        min_hold_hours=cfg["MIN_HOLD_HOURS"],
        max_round_trips_30d=cfg["MAX_ROUND_TRIPS_PER_30D"],
        no_funding_action_before_settlement_min=cfg["NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN"],
        max_entry_basis_bps=cfg["MAX_ENTRY_BASIS_BPS"],
        max_spread_bps=cfg["MAX_SPREAD_BPS"],
        spot_taker_fee=cfg["SPOT_TAKER_FEE"],
        perp_taker_fee=cfg["PERP_TAKER_FEE"],
        entry_ev_multiple=float(cfg["ENTRY_EV_MULTIPLE"]),
    )
