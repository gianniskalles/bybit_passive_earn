#!/usr/bin/env python3
"""
run_yield_cycle.py — Bybit Earn yield-rotation cycle runner.

  ┌────────────────────────────────────────────────────────────────┐
  │ WRAPPER (this script) — owns every safety decision             │
  │  - reads config/yield_rotation.yaml                            │
  │  - verifies risk_state (risk_state.verify → structured result) │
  │    and derives the EFFECTIVE state: staleness or an invalid    │
  │    record only ever makes it more conservative (T1.2)          │
  │  - fetches products, APR history, balances, positions          │
  │  - UNWIND: builds REDEEM_ALL itself, the LLM is NOT called     │
  │  - otherwise composes prompt_<PROMPT_VERSION>.md + inputs and  │
  │    calls `hermes chat` (no tools, prompt on stdin)             │
  │  - extracts the LAST JSON object carrying this cycle_id        │
  │  - validates, checks product ids (STAKE vs scan, REDEEM vs     │
  │    positions), applies the risk-state gate (drops STAKE unless │
  │    NORMAL), adds mechanical REDEEMs for unavailable products   │
  │  - computes every amount itself (the LLM never does)           │
  │  - executes via executor.py and writes the decision record     │
  │                                                                │
  │ AGENT (hermes chat)                                            │
  │  - says WHAT to do (STAKE/REDEEM/HOLD per product), not how    │
  │    much; never sees a secret; has no tools                     │
  └────────────────────────────────────────────────────────────────┘

Every parameter that affects the output lives in config/yield_rotation.yaml.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

import risk_state
import settings
from bybit_earn_tool import BybitAPIError, BybitEarnTool
from executor import Executor
from notify import Notifier

ROOT = settings.ROOT

APR_WINDOW_MS = 24 * 60 * 60 * 1000
MIN_APR_POINTS = 6  # fewer points in the 24 h window -> apr_ma_24h is null
JSONL_FILE = "{date}.jsonl"  # LOG_DIR / jsonl template
CYCLE_LATENCY_ALERT_S = 300
AGENT_TIMEOUT_S = 280
SESSION_RETENTION_DAYS = 7
# A Stake Bybit reports Success may lag behind /v5/earn/position; count it
# against the per-product cap for this long (double counting only under-stakes).
RECENT_STAKE_WINDOW_MS = 30 * 60 * 1000

AGENT_ACTIONS = ("STAKE", "REDEEM", "REDEEM_ALL", "HOLD", "ALERT_ONLY",
                 "NO_NEW_POSITIONS", "REJECTED_CROSS_COIN")
# Bybit Earn order statuses that are final (compared case-insensitively);
# anything else — including a missing or unknown status — counts as pending.
FINAL_ORDER_STATUSES = ("success", "fail")

class AgentTimeout(RuntimeError):
    """`hermes chat` did not answer within AGENT_TIMEOUT_S."""


# (raw_stdout, session_id) = agent(prompt, cfg, cycle_id)
Agent = Callable[[str, Dict[str, Any], str], Tuple[str, Optional[str]]]


# --------------------------------------------------------------------------- #
# Config loading                                                              #
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> Dict[str, Any]:
    """Parse the YAML config. Raises OSError / yaml.YAMLError / ValueError."""
    with Path(path).open() as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: top level is not a mapping")
    return cfg


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


# field -> (check, description). Every field is required.
CONFIG_SCHEMA: Dict[str, Tuple[Callable[[Any], bool], str]] = {
    "ACCOUNT_TYPE": (_nonempty_str, "non-empty string"),
    "COIN_WHITELIST": (lambda v: isinstance(v, list) and bool(v) and all(map(_nonempty_str, v)),
                       "non-empty list of coin strings"),
    "CYCLE_INTERVAL_MINUTES": (lambda v: _is_num(v) and v > 0, "number > 0"),
    "DRY_RUN": (lambda v: isinstance(v, bool), "boolean"),
    "ENTRY_APR": (lambda v: _is_num(v) and 0 <= v < 1, "number in [0, 1)"),
    "EXIT_APR": (lambda v: _is_num(v) and 0 <= v < 1, "number in [0, 1)"),
    "MIN_APR_EDGE": (lambda v: _is_num(v) and 0 <= v < 1, "number in [0, 1)"),
    "LOG_DIR": (_nonempty_str, "non-empty path"),
    "MAX_PER_PRODUCT_USD": (lambda v: _is_num(v) and v > 0, "number > 0"),
    "MAX_REDEMPTION_ETA_HOURS": (lambda v: _is_num(v) and v >= 0, "number >= 0"),
    "MIN_MOVE_USD": (lambda v: _is_num(v) and v >= 0, "number >= 0"),
    "RESERVE_USD": (lambda v: _is_num(v) and v >= 0, "number >= 0"),
    "PROMPT_VERSION": (lambda v: isinstance(v, str) and re.fullmatch(r"v\d+", v) is not None,
                       "string like v6"),
    "REQUESTED_REASONING": (_nonempty_str, "non-empty string"),
    "RESOLVED_MODEL": (_nonempty_str, "non-empty string"),
    "MAX_SCAN_AGE_SECONDS": (lambda v: _is_num(v) and v > 0, "number > 0"),
    "MAX_APR_HISTORY_GAP_HOURS": (lambda v: _is_num(v) and v > 0, "number > 0"),
}


def validate_config(cfg: Any) -> List[str]:
    """Return a list of problems; [] = the whole config is usable (T3.6)."""
    if not isinstance(cfg, dict):
        return ["config: not a mapping"]
    issues = []
    for field, (check, desc) in CONFIG_SCHEMA.items():
        if field not in cfg or cfg[field] is None:
            issues.append(f"{field}: missing")
        elif not check(cfg[field]):
            issues.append(f"{field}: {cfg[field]!r} is not a {desc}")
    sim = cfg.get("SIMULATED_IDLE_BALANCE")
    if sim is not None and not (_is_num(sim) and sim >= 0):
        issues.append(f"SIMULATED_IDLE_BALANCE: {sim!r} is not null or a number >= 0")
    elif sim is not None and cfg.get("DRY_RUN") is False:
        # Never pretend to have fake capital while moving real funds.
        issues.append("SIMULATED_IDLE_BALANCE: must be null when DRY_RUN is false")
    return issues


def prompt_path(cfg: Dict[str, Any]) -> Path:
    return ROOT / f"prompt_{cfg.get('PROMPT_VERSION')}.md"


# --------------------------------------------------------------------------- #
# Wrapper-side data collection                                                #
# --------------------------------------------------------------------------- #

def _opt_float(value: Any) -> Optional[float]:
    """Parse a Bybit numeric string; unknown/unparseable -> None (rule 7)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_coin(value: Any) -> Optional[str]:
    """'usdt ' -> 'USDT'; anything that is not a non-empty string -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper()


def _opt_int(value: Any) -> Optional[int]:
    f = _opt_float(value)
    return int(f) if f is not None and f == int(f) and f >= 0 else None


def collect_inputs(tool: BybitEarnTool, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pulls the data the agent needs, applies COIN_WHITELIST, and records each
    filter drop.  Returns a dict with: scan, positions, balances, filtered,
    snapshot_meta, idle_per_coin, product_status, orders, pending_coins,
    pending_stakes, pending_unmatched, data_errors.

    Coins are normalised to upper case everywhere.  A pending order that
    cannot be attributed to a whitelisted coin (no coin, another coin), or a
    pending Stake without a readable productId/amount, lands in
    `pending_unmatched` and blocks every new order (fail closed).

    Each Bybit source is read independently.  A failed read (BybitAPIError)
    is recorded in `data_errors[source]`; it is NEVER replaced by an empty
    list, so "no positions" and "positions unreadable" stay distinguishable.

    `product_status` maps every whitelisted product id Bybit returned to its
    status, including products dropped from `scan` — the wrapper needs it to
    exit positions in products that stopped being Available.
    """
    filtered: List[Dict[str, str]] = []
    data_errors: Dict[str, str] = {}
    snapshot_ts = int(time.time() * 1000)

    try:
        products = tool.get_earn_products()
    except BybitAPIError as e:
        data_errors["products"] = str(e)
        products = []
    # Live-scan age anchor: the input to MAX_SCAN_AGE_SECONDS.
    product_fetch_ts_ms = int(time.time() * 1000)
    whitelist = [_norm_coin(c) for c in cfg["COIN_WHITELIST"]]

    scan: List[Dict] = []
    product_status: Dict[str, Any] = {}
    product_info: Dict[str, Dict[str, Any]] = {}
    for p in products:
        coin = _norm_coin(p.get("coin"))
        if coin not in whitelist:
            continue

        product_id = str(p.get("productId"))
        status = p.get("status")
        product_status[product_id] = status
        est_str = p.get("estimateApr", "0%")
        try:
            est_apr = _parse_pct(est_str)
        except (ValueError, AttributeError):
            filtered.append({"product_id": product_id,
                             "reason": f"PARSE_ERROR: estimateApr={est_str!r}"})
            continue

        minutes = _opt_float(p.get("redeemProcessingMinute"))
        eta_hours = minutes / 60 if minutes is not None and minutes >= 0 else None
        product_info[product_id] = {"status": status, "redemption_eta_hours": eta_hours}

        if status != "Available":
            filtered.append({"product_id": product_id,
                             "reason": f"STATUS_NOT_AVAILABLE: {status!r}"})
            continue
        if bool(p.get("hasTieredApr", False)):
            # The rate at our size is not what estimateApr says; unknown.
            filtered.append({"product_id": product_id, "reason": "TIERED_APR_UNCERTAIN"})
            continue
        if eta_hours is None:
            filtered.append({"product_id": product_id,
                             "reason": "REDEMPTION_ETA_UNKNOWN: redeemProcessingMinute missing"})
            continue
        if eta_hours > cfg["MAX_REDEMPTION_ETA_HOURS"]:
            filtered.append({"product_id": product_id,
                             "reason": f"ILLIQUID: redemption_eta_hours {eta_hours} > "
                                       f"MAX_REDEMPTION_ETA_HOURS {cfg['MAX_REDEMPTION_ETA_HOURS']}"})
            continue

        try:
            hist = tool.get_earn_apr_history(product_id=product_id)
        except BybitAPIError as e:
            filtered.append({"product_id": product_id, "reason": f"APR_HISTORY_UNAVAILABLE: {e}"})
            continue
        points = _apr_points(hist)
        if not points:
            filtered.append({"product_id": product_id, "reason": "NO_APR_HISTORY"})
            continue
        # Two DISTINCT staleness checks: APR-history age here (hours,
        # MAX_APR_HISTORY_GAP_HOURS); live-scan age at decision time
        # (seconds, MAX_SCAN_AGE_SECONDS).
        latest_ts = points[-1][0]
        apr_history_age_seconds = (snapshot_ts - latest_ts) // 1000
        max_gap_hours = cfg["MAX_APR_HISTORY_GAP_HOURS"]
        if apr_history_age_seconds > max_gap_hours * 3600:
            filtered.append({
                "product_id": product_id,
                "reason": f"STALE_HISTORY: apr_history_age {apr_history_age_seconds}s > {max_gap_hours}h gap threshold",
            })
            continue

        apr_ma_24h = _apr_ma_24h(points, snapshot_ts)
        if apr_ma_24h is None:
            filtered.append({"product_id": product_id,
                             "reason": f"NO_APR_MA_24H: fewer than {MIN_APR_POINTS} points in 24 h"})
            continue

        remaining = _opt_float(p.get("remainingPoolAmount"))
        scan.append({
            "product_id": product_id,
            "coin": coin,
            "estimate_apr": est_apr,
            "apr_ma_24h": apr_ma_24h,
            "status": status,
            "min_stake_amount": _opt_float(p.get("minStakeAmount")),
            "max_stake_amount": _opt_float(p.get("maxStakeAmount")),
            "precision": _opt_int(p.get("precision")),
            # Bybit returns -1 for an unlimited pool -> null (no pool limit).
            "remaining_capacity": None if remaining is not None and remaining < 0 else remaining,
            "redemption_eta_hours": eta_hours,
            "apr_history_age_seconds": apr_history_age_seconds,
        })

    # --- balances + positions (only for whitelisted coins) ---
    balances_summary: List[Dict] = []
    real_idle_per_coin: Dict[str, float] = {}
    try:
        balance_data = tool.get_wallet_balance(account_type=cfg["ACCOUNT_TYPE"])
    except BybitAPIError as e:
        data_errors["balance"] = str(e)
        balance_data = {}
    if balance_data.get("list"):
        for c in balance_data["list"][0].get("coin", []):
            coin = _norm_coin(c.get("coin"))
            if coin not in whitelist:
                continue
            wallet = _opt_float(c.get("walletBalance")) or 0.0
            real_idle_per_coin[coin] = wallet
            balances_summary.append({
                "coin": coin,
                "wallet_balance": wallet,
                "equity": _opt_float(c.get("equity")) or 0.0,
            })

    positions: List[Dict] = []
    try:
        for coin in whitelist:
            for p in tool.get_earn_positions(coin=coin):
                pid, amount = _norm_id(p.get("productId")), _opt_float(p.get("amount"))
                if pid is None or amount is None or amount < 0:
                    # Counting it as 0 would let the per-product cap be exceeded.
                    raise BybitAPIError(f"unreadable position {p!r}")
                info = product_info.get(pid, {})
                positions.append({
                    "product_id": pid,
                    "coin": _norm_coin(p.get("coin")) or coin,
                    "amount": amount,
                    "status": p.get("status"),
                    "product_status": info.get("status"),
                    "redemption_eta_hours": info.get("redemption_eta_hours"),
                })
    except BybitAPIError as e:
        data_errors["positions"] = str(e)
        positions = []

    orders: List[Dict] = []
    try:
        for o in tool.get_earn_orders():
            orders.append({k: o.get(k) for k in ("orderId", "orderLinkId", "orderType", "coin",
                                                  "productId", "orderValue", "status",
                                                  "createdAt", "updatedAt")})
    except BybitAPIError as e:
        data_errors["orders"] = str(e)
    pending_coins, pending_unmatched = set(), []
    # Stake amounts committed but maybe not yet in positions: pending, plus
    # recently successful (RECENT_STAKE_WINDOW_MS).
    pending_stakes: Dict[str, Decimal] = {}
    for o in orders:
        status = str(o.get("status") or "").strip().lower()
        if status == "success" and str(o.get("orderType") or "").strip().lower() == "stake":
            created, pid, value = (_opt_float(o.get("createdAt")), _norm_id(o.get("productId")),
                                   _dec(o.get("orderValue")))
            if (created is not None and snapshot_ts - created < RECENT_STAKE_WINDOW_MS
                    and pid is not None and value is not None and value > 0):
                pending_stakes[pid] = pending_stakes.get(pid, Decimal(0)) + value
            continue
        if status in FINAL_ORDER_STATUSES:
            continue
        coin = _norm_coin(o.get("coin"))
        if coin not in whitelist:
            pending_unmatched.append(o)
            continue
        pending_coins.add(coin)
        if str(o.get("orderType") or "").strip().lower() != "redeem":
            # Stake (or an unknown type, treated as one): count it against the cap.
            pid, value = _norm_id(o.get("productId")), _dec(o.get("orderValue"))
            if pid is None or value is None or value < 0:
                pending_unmatched.append(o)
                continue
            pending_stakes[pid] = pending_stakes.get(pid, Decimal(0)) + value

    # DRY_RUN-only simulated balance substitution.
    balance_source = "real"
    sim = cfg.get("SIMULATED_IDLE_BALANCE")
    if cfg.get("DRY_RUN") and sim is not None:
        balances_summary = [{"coin": coin, "wallet_balance": float(sim), "equity": float(sim),
                             "_note": "simulated (DRY_RUN)"} for coin in whitelist]
        real_idle_per_coin = {coin: float(sim) for coin in whitelist}
        balance_source = "simulated"
        data_errors.pop("balance", None)  # the real balance is not used

    snapshot_meta = {
        "ts": snapshot_ts,
        "product_fetch_ts_ms": product_fetch_ts_ms,
        "account_type": cfg["ACCOUNT_TYPE"],
        "balance_source": balance_source,
        "apr_history_max_gap_hours": cfg["MAX_APR_HISTORY_GAP_HOURS"],
    }
    return {"scan": scan, "positions": positions, "balances": balances_summary,
            "filtered": filtered, "snapshot_meta": snapshot_meta,
            "idle_per_coin": real_idle_per_coin, "product_status": product_status,
            "product_info": product_info,
            "orders": orders, "pending_coins": sorted(pending_coins),
            "pending_stakes": pending_stakes, "pending_unmatched": pending_unmatched,
            "data_errors": data_errors}


# --------------------------------------------------------------------------- #
# Risk state                                                                  #
# --------------------------------------------------------------------------- #

def resolve_risk_state(env: Optional[Dict[str, str]] = None) -> Tuple[str, Dict, List[str]]:
    """Return (effective_state, meta, alerts).

    Valid signature + fresh     -> the signed state.
    Valid signature, not fresh  -> NORMAL->NO_NEW_POSITIONS, NO_NEW_POSITIONS->same,
                                   UNWIND->UNWIND  (staleness never relaxes).
    Anything else               -> NO_NEW_POSITIONS (redemptions still allowed).
    The cycle continues in every case.
    """
    env = settings.load_env() if env is None else env
    v = risk_state.verify(settings.risk_state_file(), env.get("HERMES_RISK_HMAC_KEY", ""))
    if v.signature_valid:
        effective = v.state if v.fresh else (
            "UNWIND" if v.state == "UNWIND" else "NO_NEW_POSITIONS")
    else:
        effective = "NO_NEW_POSITIONS"
    meta = {
        "code": v.code,
        "signature_valid": v.signature_valid,
        "fresh": v.fresh,
        "verified_state": v.state,
        "effective_state": effective,
        "source": v.source,
        "ts": v.ts,
        "reason": v.reason,
        "age_ms": v.age_ms,
        "detail": v.detail,
    }
    return effective, meta, ([] if v.ok else [v.code])


# --------------------------------------------------------------------------- #
# Hermes CLI invocation                                                       #
# --------------------------------------------------------------------------- #

def build_agent_command(cfg: Dict[str, Any]) -> List[str]:
    """The exact `hermes chat` command line — shared with the regression.

    No tools (--toolsets=), prompt on stdin (never in argv, where `ps` shows
    it), pinned model passed verbatim (not an alias), quiet output.
    """
    return [
        str(settings.hermes_bin()), "chat",
        "--query-file", "/dev/stdin", "-Q", "--toolsets=",
        "-m", str(cfg["RESOLVED_MODEL"]),
        "--reasoning", str(cfg["REQUESTED_REASONING"]),
    ]


def call_agent(prompt: str, cfg: Dict[str, Any], cycle_id: str) -> Tuple[str, Optional[str]]:
    """Invoke `hermes chat`. Return (raw_stdout, session_id)."""
    session_id = f"{cycle_id}_{uuid.uuid4().hex[:8]}"
    cmd = build_agent_command(cfg)
    env = {
        "PATH": f"{settings.hermes_bin().parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(settings.hermes_home()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=AGENT_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired as e:
        raise AgentTimeout(f"no answer within {AGENT_TIMEOUT_S}s") from e
    raw = proc.stdout or ""
    if proc.returncode != 0 and not raw.strip():
        raise RuntimeError(
            f"hermes chat failed: rc={proc.returncode} stderr={(proc.stderr or '')[:200]!r}"
        )
    # Raw agent output for debugging/audit.
    session_dir = settings.session_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"{session_id}.raw").write_text(
        f"--- stdout ---\n{raw}\n--- stderr ---\n{proc.stderr or ''}\n"
    )
    return raw, session_id


def compose_prompt(template: str, cycle_id: str, cfg: Dict[str, Any], risk: str,
                   risk_meta: Dict, scan: List[Dict], balances: List[Dict],
                   positions: List[Dict], wrapper_alerts: List[str]) -> str:
    inputs_block = json.dumps({
        "cycle_id": cycle_id,
        "wrapper_alerts": wrapper_alerts,
        "config": cfg,
        "risk_state": risk,
        "risk_state_meta": risk_meta,
        "scan": scan,
        "balances": balances,
        "positions": positions,
    }, ensure_ascii=False, indent=2)
    return (f"[cycle_id={cycle_id}]\n[prompt_version={cfg.get('PROMPT_VERSION')}]\n\n"
            f"{template}\n\n```json\n{inputs_block}\n```\n")


# --------------------------------------------------------------------------- #
# JSON extraction + validation                                                #
# --------------------------------------------------------------------------- #

def _balanced_objects(raw: str):
    """Yield every top-level balanced {...} substring, in order."""
    pos = 0
    while True:
        start = raw.find("{", pos)
        if start < 0:
            return
        depth, end, in_string, escape = 0, -1, False, False
        for i in range(start, len(raw)):
            c = raw[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            return
        yield raw[start:end]
        pos = end


def extract_json(raw: str, cycle_id: str) -> Dict:
    """Return the LAST decision object in `raw` whose cycle_id is this cycle's.

    Anything else — the prompt's example (cycle_id "EXAMPLE"), an echo of a
    previous cycle, OTel banners — is ignored.  Raises ValueError if there is
    no such object.
    """
    found = None
    for candidate in _balanced_objects(raw):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (isinstance(parsed, dict) and "decisions" in parsed
                and parsed.get("cycle_id") == cycle_id):
            found = parsed
    if found is None:
        raise ValueError(f"no decision object with cycle_id={cycle_id!r} in agent output")
    return found


def _norm_id(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)) and str(value).strip():
        return str(value).strip()
    return None


def validate_decision_record(rec: Dict) -> List[str]:
    """Return a list of issues; empty list = OK.  Normalises product_id to str.

    Amount fields from the LLM are ignored, never validated: the wrapper
    computes every amount (T1.5), so no LLM type slip can crash the cycle.
    """
    issues: List[str] = []
    decisions = rec.get("decisions")
    if not isinstance(decisions, list):
        return ["missing or non-list `decisions`"]
    for i, d in enumerate(decisions):
        if not isinstance(d, dict):
            issues.append(f"decision[{i}] is not an object")
            continue
        a = d.get("action")
        if a not in AGENT_ACTIONS:
            issues.append(f"decision[{i}] unknown action {a!r}")
            continue
        if "reason" not in d:
            issues.append(f"decision[{i}] missing `reason`")
        if a in ("STAKE", "REDEEM"):
            if not isinstance(d.get("coin"), str):
                issues.append(f"decision[{i}] {a} missing coin")
            pid = _norm_id(d.get("product_id"))
            if pid is None:
                issues.append(f"decision[{i}] {a} missing product_id")
            else:
                d["product_id"] = pid
    return issues


# --------------------------------------------------------------------------- #
# Gates, mechanical exits and the plan (all deterministic)                    #
# --------------------------------------------------------------------------- #

def apply_risk_gate(decisions: List[Dict], effective_state: str) -> Tuple[List[Dict], List[str]]:
    """Drop every STAKE unless the effective state is NORMAL."""
    if effective_state == "NORMAL":
        return decisions, []
    return _drop_stakes(decisions, "RISK_GATE_DROPPED_STAKE", f"under {effective_state}")


def apply_data_gate(decisions: List[Dict], data_errors: Dict[str, str]) -> Tuple[List[Dict], List[str]]:
    """Drop every STAKE when positions, balance or orders could not be read.

    The per-product cap subtracts the held position and pending orders
    block duplicates; without those inputs a STAKE could exceed
    MAX_PER_PRODUCT_USD or repeat an order. Fail closed."""
    missing = sorted(k for k in ("positions", "balance", "orders") if k in data_errors)
    if not missing:
        return decisions, []
    return _drop_stakes(decisions, "DATA_GATE_DROPPED_STAKE", f"({', '.join(missing)} unavailable)")


def _drop_stakes(decisions: List[Dict], code: str, why: str) -> Tuple[List[Dict], List[str]]:
    kept = [d for d in decisions if d.get("action") != "STAKE"]
    dropped = len(decisions) - len(kept)
    return kept, ([f"{code}: {dropped} STAKE dropped {why}"] if dropped else [])


def unwind_decisions(cfg: Dict[str, Any]) -> List[Dict]:
    return [{"action": "REDEEM_ALL", "coin": _norm_coin(coin), "origin": "wrapper",
             "reason": "risk_state UNWIND: wrapper redeems every position (no LLM)"}
            for coin in cfg["COIN_WHITELIST"]]


def unavailable_redeems(positions: List[Dict], product_status: Dict[str, Any]) -> List[Dict]:
    """REDEEM for every position whose product Bybit lists as not Available."""
    out, seen = [], set()
    for p in positions:
        pid = p["product_id"]
        status = product_status.get(pid)
        if pid in product_status and status != "Available" and pid not in seen:
            seen.add(pid)
            out.append({"action": "REDEEM", "coin": p.get("coin"), "product_id": pid,
                        "origin": "wrapper",
                        "reason": f"wrapper: product status {status!r} != 'Available'"})
    return out


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


def compute_stake_amount(idle: Decimal, held: Decimal, product: Dict,
                         cfg: Dict[str, Any]) -> Tuple[Optional[Decimal], str]:
    """min(idle − RESERVE_USD, MAX_PER_PRODUCT_USD − held, remaining_capacity,
    max_stake_amount), rounded DOWN to the product precision; None if below
    max(MIN_MOVE_USD, min_stake_amount) or if a needed value is unknown."""
    reserve = _dec(cfg.get("RESERVE_USD")) or Decimal(0)
    cap = _dec(cfg.get("MAX_PER_PRODUCT_USD"))
    min_move = _dec(cfg.get("MIN_MOVE_USD")) or Decimal(0)
    if cap is None:
        return None, "MAX_PER_PRODUCT_USD not set"
    precision = product.get("precision")
    if not isinstance(precision, int):
        return None, "product precision unknown"
    max_stake = _dec(product.get("max_stake_amount"))
    if max_stake is None:
        return None, "product max_stake_amount unknown"
    min_stake = _dec(product.get("min_stake_amount"))
    if min_stake is None:
        return None, "product min_stake_amount unknown"

    limits = {"idle - RESERVE_USD": idle - reserve,
              "MAX_PER_PRODUCT_USD - held": cap - held,
              "max_stake_amount": max_stake}
    remaining = product.get("remaining_capacity")
    if remaining is not None:  # null = Bybit reports no pool limit
        limits["remaining_capacity"] = _dec(remaining) or Decimal(0)
    binding = min(limits, key=lambda k: limits[k])
    amount = limits[binding].quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
    floor = max(min_move, min_stake)
    if amount <= 0 or amount < floor:
        return None, (f"amount {_fmt(max(amount, Decimal(0)))} below floor {_fmt(floor)} "
                      f"(max(MIN_MOVE_USD, min_stake_amount)); binding limit {binding}")
    return amount, f"binding limit {binding}"


def build_plan(decisions: List[Dict], cfg: Dict[str, Any], scan: List[Dict],
               positions: List[Dict], idle_per_coin: Dict[str, float],
               pending_coins: Optional[List[str]] = None,
               pending_stakes: Optional[Dict[str, Decimal]] = None,
               block_all: bool = False) -> Tuple[List[Dict], List[Dict]]:
    """Turn decisions into executable orders.  Returns (orders, skipped),
    where `skipped` are execution-shaped records explaining why a decision
    produced no order.

    No order is planned in a coin that has a pending Bybit order (T2.2) — a
    redemption can take up to 48 h — and none at all when `block_all`.
    Pending Stake amounts count against MAX_PER_PRODUCT_USD like held
    positions do."""
    pending = {_norm_coin(c) for c in (pending_coins or [])}

    def is_pending(coin: Optional[str]) -> bool:
        return block_all or _norm_coin(coin) in pending

    scan_by_id = {p["product_id"]: p for p in scan}
    held: Dict[str, Decimal] = {}
    for p in positions:
        held[p["product_id"]] = held.get(p["product_id"], Decimal(0)) + (_dec(p.get("amount")) or Decimal(0))
    # Committed to a product = held + pending stakes; only held can be redeemed.
    committed = dict(held)
    for pid, amount in (pending_stakes or {}).items():
        committed[pid] = committed.get(pid, Decimal(0)) + amount
    idle = {c: _dec(v) or Decimal(0) for c, v in idle_per_coin.items()}

    orders: List[Dict] = []
    skipped: List[Dict] = []
    staked, redeemed = set(), set()

    def skip(d: Dict, reason: str) -> None:
        skipped.append({"ts": _now(), "action": d.get("action"), "coin": d.get("coin"),
                        "product_id": d.get("product_id"), "amount": None,
                        "origin": d.get("origin", "agent"), "would_call": None,
                        "executed": False, "reason": reason})

    def redeem(pid: str, coin: Optional[str], origin: str, reason: str) -> None:
        if pid in redeemed:
            return
        if is_pending(coin):
            redeemed.add(pid)
            skip({"action": "REDEEM", "coin": coin, "product_id": pid, "origin": origin},
                 _pending_reason(coin, block_all))
            return
        amount = held.get(pid)
        if amount is None or amount <= 0:
            skip({"action": "REDEEM", "coin": coin, "product_id": pid, "origin": origin},
                 "no position amount to redeem")
            return
        redeemed.add(pid)
        orders.append({"action": "REDEEM", "coin": coin, "product_id": pid,
                       "amount": _fmt(amount), "origin": origin, "reason": reason})

    for d in decisions:
        action = d.get("action")
        origin = d.get("origin", "agent")
        if action == "REDEEM":
            redeem(d["product_id"], d.get("coin"), origin, str(d.get("reason", "")))
        elif action == "REDEEM_ALL":
            for p in positions:
                if d.get("coin") is None or _norm_coin(d.get("coin")) == _norm_coin(p.get("coin")):
                    redeem(p["product_id"], p.get("coin"), origin, str(d.get("reason", "")))
        elif action == "STAKE":
            pid = d["product_id"]
            if pid in staked:
                skip(d, "duplicate STAKE for this product in the same cycle")
                continue
            product = scan_by_id[pid]
            coin = product["coin"]
            if is_pending(coin):
                staked.add(pid)
                skip(d, _pending_reason(coin, block_all))
                continue
            amount, why = compute_stake_amount(idle.get(coin, Decimal(0)),
                                               committed.get(pid, Decimal(0)), product, cfg)
            if amount is None:
                skip(d, why)
                continue
            staked.add(pid)
            idle[coin] = idle.get(coin, Decimal(0)) - amount
            orders.append({"action": "STAKE", "coin": coin, "product_id": pid,
                           "amount": _fmt(amount), "origin": origin,
                           "reason": f"{d.get('reason', '')} | amount: {why}"})
    return orders, skipped


def _pending_reason(coin: Optional[str], block_all: bool) -> str:
    if block_all:
        return "pending Bybit order not attributable to a whitelisted coin; no new orders at all"
    return f"pending Bybit order in {coin}; no new order until it completes"


# --------------------------------------------------------------------------- #
# Decision log                                                                #
# --------------------------------------------------------------------------- #

def write_decision_log(log_dir: Path, rec: Dict) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = log_dir / JSONL_FILE.format(date=today)
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_pct(s: str) -> float:
    """Bybit returns APR as '0.8%' or '0.008' depending on endpoint."""
    s = s.strip()
    if s.endswith("%"):
        return float(s.rstrip("%")) / 100.0
    return float(s)


def _apr_points(hist: List[Dict]) -> List[Tuple[int, float]]:
    """(timestamp_ms, apr) for every parseable point, sorted by time.
    Bybit's ordering is never assumed."""
    points = []
    for h in hist:
        if not isinstance(h, dict):
            continue
        ts, apr = _opt_float(h.get("timestamp")), h.get("apr")
        try:
            value = _parse_pct(apr) if isinstance(apr, str) else None
        except ValueError:
            value = None
        if ts is not None and value is not None:
            points.append((int(ts), value))
    return sorted(points)


def _apr_ma_24h(points: List[Tuple[int, float]], now_ms: int) -> Optional[float]:
    """Mean APR over the 24 h time window ending now; null with fewer than
    MIN_APR_POINTS points in it."""
    window = [v for ts, v in points if now_ms - APR_WINDOW_MS < ts <= now_ms]
    if len(window) < MIN_APR_POINTS:
        return None
    return sum(window) / len(window)


# --------------------------------------------------------------------------- #
# The cycle                                                                   #
# --------------------------------------------------------------------------- #

def run_cycle(cfg: Dict[str, Any], tool, agent: Agent = call_agent,
              config_error: Optional[str] = None) -> Tuple[Dict, int]:
    """Run one cycle, write its decision record, return (record, exit_code).

    Exit codes: 0 ok, 3 config problem (CONFIG_INCOMPLETE), 4 unexpected
    exception (CYCLE_CRASH). A record is written in every case (T3.7).
    """
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    cycle_start = time.monotonic()
    cfg = cfg if isinstance(cfg, dict) else {}
    log_dir = Path(cfg["LOG_DIR"]) if _nonempty_str(cfg.get("LOG_DIR")) else settings.default_log_dir()
    alerts: List[str] = []
    rec: Dict[str, Any] = {
        "ts": _now(),
        "cycle_id": cycle_id,
        "session_id": None,
        "risk_state": "NO_NEW_POSITIONS",
        "risk_state_meta": None,
        "agent_called": False,
        "decisions": [],
        "holds": [],
        "alerts": alerts,
        "plan": [],
        "executions": [],
        "dry_run": cfg.get("DRY_RUN", True),
        "balance_source": "real",
        "filtered_by_wrapper": [],
        "prompt_version": cfg.get("PROMPT_VERSION"),
        "prompt_file": None,
        "prompt_sha256": None,
        "model_requested_on_cli": cfg.get("RESOLVED_MODEL"),
        "snapshot_meta": None,
        "orders": [],
        "data_errors": {},
    }

    try:
        rc = _cycle(cfg, tool, agent, cycle_id, rec, alerts, config_error)
    except Exception as e:
        # Never die silently: the record carries the crash and blocks the heartbeat.
        alerts.append(f"CYCLE_CRASH: {type(e).__name__}: {e}")
        rec["crash"] = traceback.format_exc(limit=8)
        rec["decisions"] = rec.get("decisions") or [{"action": "ALERT_ONLY", "reason": "cycle crashed"}]
        rc = 4

    rec["cycle_duration_seconds"] = round(time.monotonic() - cycle_start, 3)
    if rec["cycle_duration_seconds"] > CYCLE_LATENCY_ALERT_S:
        alerts.append(f"CYCLE_LATENCY_HIGH: cycle took {rec['cycle_duration_seconds']}s "
                      f"(> {CYCLE_LATENCY_ALERT_S}s); will start losing cycles silently")
    write_decision_log(log_dir, rec)
    return rec, rc


def _cycle(cfg: Dict[str, Any], tool, agent: Agent, cycle_id: str, rec: Dict[str, Any],
           alerts: List[str], config_error: Optional[str]) -> int:
    """The cycle body; fills `rec`/`alerts` in place and returns the exit code."""
    # Whole config validated before anything else (T3.6).
    issues = ([f"config unreadable: {config_error}"] if config_error else []) + validate_config(cfg)
    if issues:
        alerts.extend(f"CONFIG_INCOMPLETE: {i}" for i in issues)
        alerts.append("CRITICAL")
        rec["decisions"] = [{"action": "ALERT_ONLY", "reason": "CRITICAL: invalid config; nothing runs"}]
        return 3

    # Risk state: never terminates the cycle (T1.2).
    effective, risk_meta, risk_alerts = resolve_risk_state()
    rec["risk_state"], rec["risk_state_meta"] = effective, risk_meta
    alerts.extend(risk_alerts)

    data = collect_inputs(tool, cfg)
    scan, positions = data["scan"], data["positions"]
    data_errors = data["data_errors"]
    rec["filtered_by_wrapper"] = data["filtered"]
    rec["snapshot_meta"] = data["snapshot_meta"]
    rec["balance_source"] = data["snapshot_meta"]["balance_source"]
    rec["orders"] = data["orders"][-20:]
    rec["data_errors"] = data_errors
    alerts.extend(f"DATA_UNAVAILABLE: {src}: {err}" for src, err in sorted(data_errors.items()))
    if data["pending_coins"]:
        alerts.append(f"PENDING_ORDERS: {', '.join(map(str, data['pending_coins']))}")
    if data["pending_unmatched"]:
        ids = ", ".join(str(o.get("orderId") or o.get("orderLinkId")) for o in data["pending_unmatched"])
        alerts.append(f"PENDING_ORDER_UNMATCHED: {len(data['pending_unmatched'])} pending order(s) "
                      f"not attributable to a whitelisted coin ({ids}); all new orders blocked")

    if "positions" in data_errors:
        # Without positions nothing can be sized or redeemed safely: no LLM,
        # no orders this cycle (non-blocking; the next cycle retries).
        rec["decisions"] = [{"action": "ALERT_ONLY",
                             "reason": "positions unavailable; no orders this cycle"}]
        return 0

    if not scan and not positions and not data_errors:
        # Transient (e.g. stale APR history) — visible, but non-blocking.
        alerts.append("NO_ELIGIBLE_PRODUCTS: no whitelisted product survived filtering")
        rec["decisions"] = [{"action": "HOLD", "reason": "no whitelisted product survived filtering"}]
        return 0

    if effective == "UNWIND":
        # The kill switch does not depend on a model.
        decisions = unwind_decisions(cfg)
    else:
        path = prompt_path(cfg)
        if not path.is_file():
            alerts.extend([f"CONFIG_INCOMPLETE: prompt file {path.name} not found", "CRITICAL"])
            rec["decisions"] = [{"action": "ALERT_ONLY", "reason": f"prompt file {path.name} missing"}]
            return 3
        template = path.read_bytes()
        rec["prompt_file"] = path.name
        rec["prompt_sha256"] = hashlib.sha256(template).hexdigest()
        prompt = compose_prompt(template.decode("utf-8"), cycle_id, cfg, effective, rec["risk_state_meta"],
                                scan, data["balances"], positions, list(alerts))

        rec["agent_called"] = True
        fallback = {"decisions": [{"action": "ALERT_ONLY", "reason": "no usable agent output"}]}
        try:
            raw, rec["session_id"] = agent(prompt, cfg, cycle_id)
            agent_out = extract_json(raw, cycle_id)
        except AgentTimeout as e:
            alerts.append(f"AGENT_TIMEOUT: {e}")
            agent_out = fallback
        except (ValueError, RuntimeError, OSError) as e:
            alerts.append(f"AGENT_PARSE_ERROR: {e}")
            agent_out = fallback

        issues = validate_decision_record(agent_out)
        if issues:
            alerts.append("DECISION_VALIDATION_FAILED: " + "; ".join(issues))
            agent_out = {"decisions": [{"action": "ALERT_ONLY", "reason": "decision record failed validation"}]}
        decisions = agent_out["decisions"]
        holds = agent_out.get("holds", [])
        rec["holds"] = holds if isinstance(holds, list) else []
        agent_alerts = agent_out.get("alerts", [])
        if isinstance(agent_alerts, list):
            rec["agent_alerts"] = [a for a in agent_alerts if isinstance(a, str)]

        # Product ids: STAKE must target the scan, REDEEM a held position.
        scan_ids = {p["product_id"] for p in scan}
        held_ids = {p["product_id"] for p in positions}
        for d in decisions:
            a, pid = d.get("action"), d.get("product_id")
            if (a == "STAKE" and pid not in scan_ids) or (a == "REDEEM" and pid not in held_ids):
                where = "scan" if a == "STAKE" else "positions"
                alerts.append(f"CRITICAL: agent {a} product_id {pid!r} not in {where}")
                decisions = [{"action": "ALERT_ONLY",
                              "reason": f"CRITICAL: {a} product_id {pid!r} not in {where}"}]
                break

        # Live-scan age (MAX_SCAN_AGE_SECONDS) at decision time.
        max_scan_age_sec = cfg["MAX_SCAN_AGE_SECONDS"]
        scan_age_seconds = (int(time.time() * 1000) - data["snapshot_meta"]["product_fetch_ts_ms"]) // 1000
        if scan_age_seconds > max_scan_age_sec:
            alerts.append(f"STALE_SCAN: live scan age {scan_age_seconds}s > {max_scan_age_sec}s threshold")
            decisions = [{"action": "ALERT_ONLY", "reason": f"STALE_SCAN: live scan age {scan_age_seconds}s"}]

    # Deterministic gates — the last word before execution.
    decisions, gate_alerts = apply_risk_gate(decisions, effective)
    alerts.extend(gate_alerts)
    decisions, gate_alerts = apply_data_gate(decisions, data_errors)
    alerts.extend(gate_alerts)
    allow_new_positions = effective == "NORMAL" and not any(
        k in data_errors for k in ("positions", "balance", "orders"))
    forced = unavailable_redeems(positions, data["product_status"])
    decisions = forced + decisions
    rec["decisions"] = decisions

    orders, skipped = build_plan(decisions, cfg, scan, positions, data["idle_per_coin"],
                                 data["pending_coins"], data["pending_stakes"],
                                 block_all=bool(data["pending_unmatched"]))
    rec["plan"] = orders
    executor = Executor(bybit_tool=tool, dry_run=cfg["DRY_RUN"],
                        allow_new_positions=allow_new_positions,
                        account_type=cfg["ACCOUNT_TYPE"], cycle_id=cycle_id)
    rec["executions"] = executor.execute(orders) + skipped
    return 0


# --------------------------------------------------------------------------- #
# After the cycle: notifications and housekeeping                             #
# --------------------------------------------------------------------------- #

def notify_cycle(rec: Dict[str, Any], notifier: Notifier) -> None:
    """Telegram, deduplicated per channel (T5.2): blocking codes, the
    effective risk state, unreadable data, unattributable pending orders.
    Every live order is an event (always sent)."""
    from heartbeat import _is_blocking_code

    cid = rec.get("cycle_id")
    blocking = sorted({c for c in map(_is_blocking_code, rec.get("alerts", [])) if c})
    notifier.observe("cycle", ",".join(blocking) or None,
                     f"🚨 CYCLE {cid}: {', '.join(blocking)} — heartbeat will not renew. "
                     f"Alerts: {'; '.join(rec.get('alerts', []))[:1500]}",
                     resolved_text="✅ CYCLE: no blocking codes any more")
    state = rec.get("risk_state")
    meta = rec.get("risk_state_meta") or {}
    notifier.observe("risk_state", None if state == "NORMAL" else f"{state}:{meta.get('code')}",
                     f"⚠️ RISK STATE {state} ({meta.get('code')}, source={meta.get('source')}): "
                     f"no new positions" + ("; redeeming everything" if state == "UNWIND" else ""),
                     resolved_text="✅ RISK STATE back to NORMAL")
    errors = rec.get("data_errors") or {}
    notifier.observe("data", ",".join(sorted(errors)) or None,
                     f"⚠️ Bybit data unavailable: {', '.join(sorted(errors))} — no STAKE while it lasts",
                     resolved_text="✅ Bybit data readable again")
    unmatched = [a for a in rec.get("alerts", []) if a.startswith("PENDING_ORDER_UNMATCHED")]
    notifier.observe("pending_unmatched", "unmatched" if unmatched else None,
                     f"⚠️ {unmatched[0] if unmatched else ''}",
                     resolved_text="✅ no unattributable pending orders")
    for e in rec.get("executions", []):
        if e.get("executed"):
            notifier.event(f"✅ ORDER {e.get('action')} {e.get('amount')} {e.get('coin')} "
                           f"product {e.get('product_id')} orderId "
                           f"{(e.get('response') or {}).get('orderId')} (cycle {cid})")
        elif e.get("error"):
            notifier.event(f"🚨 ORDER FAILED {e.get('action')} {e.get('amount')} {e.get('coin')} "
                           f"product {e.get('product_id')}: {e['error'][:300]}")


def cleanup_sessions(session_dir: Path, max_age_days: int = SESSION_RETENTION_DAYS,
                     now: Optional[float] = None) -> int:
    """Delete raw agent session files older than max_age_days (T5.4)."""
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    removed = 0
    for f in Path(session_dir).glob("*.raw"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=settings.config_file())
    parser.add_argument("--dry-run", action="store_true",
                        help="override config DRY_RUN and force dry-run")
    args = parser.parse_args()

    config_error = None
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as e:
        cfg, config_error = {}, f"{type(e).__name__}: {e}"
    if args.dry_run:
        cfg["DRY_RUN"] = True

    rec, rc = run_cycle(cfg, tool=BybitEarnTool(), agent=call_agent, config_error=config_error)
    try:
        notify_cycle(rec, Notifier(settings.load_env(), cfg))
        cleanup_sessions(settings.session_dir())
    except Exception as e:  # housekeeping must never change the cycle outcome
        print(f"[run_yield_cycle] post-cycle housekeeping failed: {e}", file=sys.stderr)
    print(f"[run_yield_cycle] cycle_id={rec['cycle_id']}")
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    sys.exit(rc)


if __name__ == "__main__":
    main()
