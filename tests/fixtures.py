"""Regression test fixtures for the yield rotation prompt.

Each fixture's "inputs" section describes what the wrapper delivers
to the agent each cycle. The wrapper is responsible for:
  - HMAC-validating the risk_state file
  - Staleness-checking the risk_state ts
  - Verifying snapshot_age <= 900 per product
  - Falling back to NO_NEW_POSITIONS if risk_state fails

The agent receives the VERIFIED, NORMALISED view. It does not see the
raw sig. It does not see products with snapshot_age > 900. If
risk_state fails verification, the agent sees NO_NEW_POSITIONS +
a RISK_STATE_UNVERIFIED alert, and may only REDEEM.

Field semantics reminder:
  - `null` = field absent/unset. Not always disqualifying (see
    prompt v3 rule 1).
  - The agent's job is to read what it is given and apply the rules
    in the prompt. It never invents values.

Validator semantics: the runner checks decisions count, action, coin,
and (where applicable) product_id/from_product_id/to_product_id. It
also checks the risk_state echo, the alerts, and that specific
strings appear in `holds`.
"""

import json
import sys
from pathlib import Path


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# Shared config used by most fixtures (USDT-only, $5 test scale)
TIGHT_CONFIG = {
    "COIN_WHITELIST": ["USDT"],
    "ALLOW_CROSS_COIN": False,
    "ENTRY_APR": 0.018,
    "EXIT_APR": 0.012,
    "MIN_APR_EDGE": 0.005,
    "EDGE_PERSISTENCE_CYCLES": 3,
    "MAX_REDEMPTION_ETA_HOURS": 2,
    "MAX_PER_PRODUCT_USD": 5,
    "MIN_MOVE_USD": 1,
    "RESERVE_USD": 0,
    "COOLDOWN_MINUTES": 120,
    "MAX_ROTATIONS_PER_DAY": 2,
    "BLACKOUT_MINUTES_BEFORE_DISTRIBUTION": 60,
    "DRY_RUN": True,
    "IDLE_BALANCE_USDT": 5,
}


def _usdt_product(pid, apr_ma_24h, marginal, apr_history_age=3,
                  status="Available", remaining=50000, min_stake=1, max_stake=50000,
                  tier_cap=None, eta=0.0):
    return {
        "product_id": pid, "coin": "USDT", "category": "FlexibleSaving",
        "status": status, "estimate_apr": marginal, "apr_ma_24h": apr_ma_24h,
        "apr_ma_7d": apr_ma_24h, "apr_p25_180d": 0.0150, "apr_p75_180d": 0.0230,
        "marginal_apr_for_size": marginal, "tier_cap_amount": tier_cap,
        "min_stake_amount": min_stake, "max_stake_amount": max_stake,
        "remaining_capacity": remaining, "redemption_eta_hours": eta,
        "apr_history_age_seconds": apr_history_age,
    }


# --- Fixture 1: baseline STAKE 5 USDT (smoke test v2 prompt) ---

F1_BASELINE = {
    "name": "01_baseline_stake",
    "description": "Two products, marginal >= entry on 430, below on 431. Expect STAKE 5 to 430, HOLD 431.",
    "config": TIGHT_CONFIG,
    "risk_state": {"state": "NORMAL", "ts_age_seconds": 5, "verified": True},
    "positions": [],
    "scan": [_usdt_product("430", 0.0198, 0.0210),
             _usdt_product("431", 0.0135, 0.0142)],
    "expected": {
        "decisions": [{"action": "STAKE", "coin": "USDT",
                       "product_id": "430", "amount_usd": 5}],
        "must_hold_reason_contains": ["0.0135", "ENTRY_APR"],
        "alerts": [],
    },
}


# --- Fixture 2: apr_ma_24h is null -> product must be HOLD for STAKE ---

F2_NULL_MA24H = {
    "name": "02_null_apr_ma_24h",
    "description": "apr_ma_24h=null (cold start). Product must be HOLD for STAKE (insufficient data).",
    "config": TIGHT_CONFIG,
    "risk_state": {"state": "NORMAL", "ts_age_seconds": 5, "verified": True},
    "positions": [],
    "scan": [_usdt_product("430", None, 0.0210)],  # apr_ma_24h is null
    "expected": {
        "decisions": [],
        "must_hold_reason_contains": ["apr_ma_24h"],  # agent should reference the null
        "alerts": [],
    },
}


# --- Fixture 3: tier_cap_amount null but otherwise valid -> STAKE allowed ---

F3_NULL_TIER_CAP = {
    "name": "03_null_tier_cap_stake_ok",
    "description": "tier_cap_amount=null must NOT disqualify. Expect STAKE 5 to 430.",
    "config": TIGHT_CONFIG,
    "risk_state": {"state": "NORMAL", "ts_age_seconds": 5, "verified": True},
    "positions": [],
    "scan": [_usdt_product("430", 0.0220, 0.0250, tier_cap=None)],
    "expected": {
        "decisions": [{"action": "STAKE", "coin": "USDT",
                       "product_id": "430", "amount_usd": 5}],
        "must_hold_reason_contains": [],
        "alerts": [],
    },
}


# --- Fixture 4: risk state UNWIND with open position -> REDEEM_ALL ---

F4_RISK_UNWIND = {
    "name": "04_risk_state_unwind",
    "description": "Risk state UNWIND must emit REDEEM_ALL regardless of rate.",
    "config": TIGHT_CONFIG,
    "risk_state": {"state": "UNWIND", "ts_age_seconds": 5, "verified": True,
                   "reason": "manual unwind triggered"},
    "positions": [{"product_id": "430", "coin": "USDT", "amount": 5,
                   "claimable_yield": 0.0001}],
    "scan": [_usdt_product("430", 0.0198, 0.0210)],
    "expected": {
        # REDEEM_ALL has no per-product field by design; just check action+state
        "decisions": [{"action": "REDEEM_ALL", "any_coin": True}],
        "risk_state_echo": "UNWIND",
        "alerts": [],
    },
}


# --- Fixture 5: invalid HMAC -> wrapper treats as NO_NEW_POSITIONS, agent must echo ---

F5_INVALID_HMAC = {
    "name": "05_risk_state_invalid_hmac",
    "description": "Wrapper received bad sig. It normalises state to NO_NEW_POSITIONS and "
                   "adds RISK_STATE_UNVERIFIED alert. Agent must echo NO_NEW_POSITIONS, "
                   "must not stake, must raise CRITICAL or RISK_STATE_UNVERIFIED alert.",
    "config": TIGHT_CONFIG,
    "risk_state": {"state": "NO_NEW_POSITIONS", "ts_age_seconds": None,
                   "verified": False,
                   "failure_reason": "HMAC mismatch"},
    "positions": [{"product_id": "430", "coin": "USDT", "amount": 5,
                   "claimable_yield": 0.0}],
    "scan": [_usdt_product("430", 0.0198, 0.0210),
             _usdt_product("431", 0.0230, 0.0250)],
    "expected": {
        "decisions": [{"action": "REDEEM", "coin": "USDT", "from_product_id": "430"}],
        "risk_state_echo": "NO_NEW_POSITIONS",
        "must_alert_contain": ["RISK_STATE_UNVERIFIED"],
    },
}


# --- Fixture 6: apr_history_age > 4h -> wrapper drops the product ---

F6_STALE_SNAPSHOT = {
    "name": "06_stale_history",
    "description": "apr_history_age_seconds=18000 (>4h gap). Wrapper drops the product from scan. "
                   "Agent sees an empty scan -> no decision, must raise STALE_HISTORY alert.",
    "config": TIGHT_CONFIG,
    "risk_state": {"state": "NORMAL", "ts_age_seconds": 5, "verified": True},
    "positions": [],
    "scan": [],  # wrapper dropped the only product
    "wrapper_drops": "STALE_HISTORY",  # tells the runner to add this alert
    "expected": {
        "decisions": [],
        "must_alert_contain": ["STALE_HISTORY"],
    },
}


# --- Fixture 7: ENTRY_APR is null in config -> wrapper adds CONFIG_INCOMPLETE ---

F7_NULL_CONFIG = {
    "name": "07_null_entry_apr_config",
    "description": "ENTRY_APR=null in config. Wrapper adds CONFIG_INCOMPLETE. Agent must HOLD.",
    "config": {**TIGHT_CONFIG, "ENTRY_APR": None},
    "risk_state": {"state": "NORMAL", "ts_age_seconds": 5, "verified": True},
    "positions": [],
    "scan": [_usdt_product("430", 0.0230, 0.0250)],
    "expected": {
        "decisions": [],
        "must_alert_contain": ["CONFIG_INCOMPLETE"],
    },
}


# --- Fixture 8: status NotAvailable with open position -> REDEEM ---

F8_NOT_AVAILABLE = {
    "name": "08_status_not_available",
    "description": "Product status=NotAvailable, open position. Expect REDEEM (risk exit).",
    "config": TIGHT_CONFIG,
    "risk_state": {"state": "NORMAL", "ts_age_seconds": 5, "verified": True},
    "positions": [{"product_id": "430", "coin": "USDT", "amount": 5,
                   "claimable_yield": 0.0}],
    "scan": [_usdt_product("430", 0.0230, 0.0250,
                            status="NotAvailable", remaining=0)],
    "expected": {
        # REDEEM uses from_product_id (semantically: from which product);
        # STAKE uses product_id. Agent may use either; we accept both.
        "decisions": [{"action": "REDEEM", "coin": "USDT",
                       "product_id_any": ["430"]}],
    },
}


F9_VALID_PRODUCT_ID = {
    "name": "09_valid_product_id",
    "description": "Agent returns product_id not in scan -> wrapper rejects with CRITICAL.",
    "config": TIGHT_CONFIG,
    "risk_state": {"state": "NORMAL", "ts_age_seconds": 5, "verified": True},
    "positions": [],
    "scan": [_usdt_product("430", 0.0230, 0.0250)],
    "expected": {
        "decisions": [{"action": "STAKE", "coin": "USDT",
                       "product_id": "999", "amount_usd": 5}],
        "must_hold_reason_contains": [],
        "alerts": [],
    },
}

ALL_FIXTURES = [
    F1_BASELINE, F2_NULL_MA24H, F3_NULL_TIER_CAP, F4_RISK_UNWIND,
    F5_INVALID_HMAC, F6_STALE_SNAPSHOT, F7_NULL_CONFIG, F8_NOT_AVAILABLE,
    F9_VALID_PRODUCT_ID,
]


def get_fixture(name: str) -> dict:
    for f in ALL_FIXTURES:
        if f["name"] == name:
            return f
    raise KeyError(name)


def list_fixture_names() -> list:
    return [f["name"] for f in ALL_FIXTURES]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        for f in ALL_FIXTURES:
            print(f"{f['name']}: {f['description']}")
    else:
        print(f"{len(ALL_FIXTURES)} fixtures defined")
