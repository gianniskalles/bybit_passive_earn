"""carry/state.py — the carry strategy's own risk state and what each state
permits (CARRY_PLAN §6, decision 13).

The record lives in its own file (settings.carry_risk_state_file()) with its
own profile ("hermes-carry"), signed by signing.py through risk_state.py and
kept alive by `heartbeat.py --system carry`. The yield rotation's file can
never be read as the carry's, or vice versa.

Effective state, exactly as for the yield rotation:
  valid + fresh            -> as signed
  valid, stale             -> NORMAL->NO_NEW_POSITIONS, NNP->NNP, UNWIND->UNWIND
  missing/invalid/no key   -> NO_NEW_POSITIONS
Staleness only ever makes the system more conservative.

Actions (per cycle, per symbol):
  ENTER                        open a neutral position
  EARN_REDEEM_FOR_ENTRY        step 1 of an entry: redeem USDT from Easy Earn
  REBALANCE_AWAY_FROM_NEUTRAL  any leg change that increases |delta| or size
  EXIT                         close both legs
  TRIM                         reduce both legs proportionally
  REBALANCE_TOWARD_NEUTRAL     reduce the larger leg towards the smaller
  EARN_RETURN                  put idle USDT back into Easy Earn (never the
                               margin buffer while a position is open — that
                               is a sizing rule, not a state rule)
Anything that reduces risk is allowed in every state, including unknown ones.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import risk_state
import settings

PROFILE = risk_state.CARRY_PROFILE
STATES = ("NORMAL", "NO_NEW_POSITIONS", "UNWIND")

RISK_REDUCING = frozenset({"EXIT", "TRIM", "REBALANCE_TOWARD_NEUTRAL", "EARN_RETURN"})
EXPOSURE_INCREASING = frozenset({"ENTER", "EARN_REDEEM_FOR_ENTRY",
                                 "REBALANCE_AWAY_FROM_NEUTRAL"})
_PERMITTED = {
    "NORMAL": RISK_REDUCING | EXPOSURE_INCREASING,
    "NO_NEW_POSITIONS": RISK_REDUCING,
    "UNWIND": RISK_REDUCING,
}


def is_allowed(state: str, action: str) -> bool:
    """Unknown state -> only risk-reducing actions; unknown action -> denied."""
    if action not in RISK_REDUCING | EXPOSURE_INCREASING:
        return False
    return action in _PERMITTED.get(state, RISK_REDUCING)


def resolve(env: Optional[Dict[str, str]] = None) -> Tuple[str, Dict, List[str]]:
    """(effective_state, meta, alerts) for the carry strategy."""
    env = settings.load_env() if env is None else env
    v = risk_state.verify(settings.carry_risk_state_file(),
                          env.get("HERMES_RISK_HMAC_KEY", ""), profile=PROFILE)
    if v.signature_valid:
        effective = v.state if v.fresh else ("UNWIND" if v.state == "UNWIND" else "NO_NEW_POSITIONS")
    else:
        effective = "NO_NEW_POSITIONS"
    meta = {"code": v.code, "signature_valid": v.signature_valid, "fresh": v.fresh,
            "verified_state": v.state, "effective_state": effective, "source": v.source,
            "ts": v.ts, "reason": v.reason, "age_ms": v.age_ms, "detail": v.detail}
    return effective, meta, ([] if v.ok else [v.code])
