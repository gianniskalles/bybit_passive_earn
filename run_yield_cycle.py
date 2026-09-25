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
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

import risk_state
import settings
from bybit_earn_tool import BybitEarnTool
from executor import Executor

ROOT = settings.ROOT

STALE_SNAPSHOT_MS = 4 * 60 * 60 * 1000  # 4 h — Bybit USDT APR history refresh
JSONL_FILE = "{date}.jsonl"  # LOG_DIR / jsonl template
CYCLE_LATENCY_ALERT_S = 300
AGENT_TIMEOUT_S = 280

AGENT_ACTIONS = ("STAKE", "REDEEM", "REDEEM_ALL", "HOLD", "ALERT_ONLY",
                 "NO_NEW_POSITIONS", "REJECTED_CROSS_COIN")
MANDATORY_PARAMS = ["ENTRY_APR", "RESOLVED_MODEL", "ACCOUNT_TYPE"]

# (raw_stdout, session_id) = agent(prompt, cfg, cycle_id)
Agent = Callable[[str, Dict[str, Any], str], Tuple[str, Optional[str]]]


# --------------------------------------------------------------------------- #
# Config loading                                                              #
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"config not found: {path}")
    with path.open() as f:
        return yaml.safe_load(f)


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


def _opt_int(value: Any) -> Optional[int]:
    f = _opt_float(value)
    return int(f) if f is not None and f == int(f) and f >= 0 else None


def collect_inputs(tool: BybitEarnTool, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pulls the data the agent needs, applies COIN_WHITELIST, and records each
    filter drop.  Returns a dict with: scan, positions, balances, filtered,
    snapshot_meta, idle_per_coin, product_status.

    `product_status` maps every whitelisted product id Bybit returned to its
    status, including products dropped from `scan` — the wrapper needs it to
    exit positions in products that stopped being Available.
    """
    filtered: List[Dict[str, str]] = []
    snapshot_ts = int(time.time() * 1000)

    products = tool.get_earn_products() or []
    # Live-scan age anchor: the input to MAX_SCAN_AGE_SECONDS.
    product_fetch_ts_ms = int(time.time() * 1000)
    whitelist = list(cfg["COIN_WHITELIST"])

    scan: List[Dict] = []
    product_status: Dict[str, Any] = {}
    for p in products:
        coin = p.get("coin")
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

        if status != "Available":
            filtered.append({"product_id": product_id,
                             "reason": f"STATUS_NOT_AVAILABLE: {status!r}"})
            continue

        hist = tool.get_earn_apr_history(coin=coin) or []
        if not hist:
            filtered.append({"product_id": product_id, "reason": "NO_APR_HISTORY"})
            continue
        # Two DISTINCT staleness checks: APR-history age here (hours,
        # MAX_APR_HISTORY_GAP_HOURS); live-scan age at decision time
        # (seconds, MAX_SCAN_AGE_SECONDS).
        latest_ts = max(int(h.get("timestamp", 0)) for h in hist)
        apr_history_age_seconds = (snapshot_ts - latest_ts) // 1000
        max_gap_hours = cfg.get("MAX_APR_HISTORY_GAP_HOURS", 4)
        if apr_history_age_seconds > max_gap_hours * 3600:
            filtered.append({
                "product_id": product_id,
                "reason": f"STALE_HISTORY: apr_history_age {apr_history_age_seconds}s > {max_gap_hours}h gap threshold",
            })
            continue

        apr_ma_24h = _apr_ma_24h(hist)
        if apr_ma_24h is None:
            filtered.append({"product_id": product_id, "reason": "NO_APR_MA_24H"})
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
            "has_tiered_apr": bool(p.get("hasTieredApr", False)),
            # Bybit returns -1 for an unlimited pool -> null (no pool limit).
            "remaining_capacity": None if remaining is not None and remaining < 0 else remaining,
            "tier_cap_amount": None,  # Bybit doesn't expose this in /v5/earn/product
            "redemption_eta_hours": 0.0,  # FlexibleSaving is T+0
            "apr_ma_7d": apr_ma_24h,  # we only keep 24h; 7d = 24h best-effort
            "apr_p25_180d": None,  # 180d history not exposed by API
            "apr_p75_180d": None,  # 180d history not exposed by API
            "marginal_apr_for_size": est_apr,  # = estimate_apr (no tier effect at our size)
            "apr_history_age_seconds": apr_history_age_seconds,
        })

    # --- balances + positions (only for whitelisted coins) ---
    balance_data = tool.get_wallet_balance(account_type=cfg["ACCOUNT_TYPE"]) or {}
    balances_summary: List[Dict] = []
    real_idle_per_coin: Dict[str, float] = {}
    if balance_data.get("list"):
        for c in balance_data["list"][0].get("coin", []):
            coin = c.get("coin")
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
    for coin in whitelist:
        for p in tool.get_earn_positions(coin=coin) or []:
            positions.append({
                "product_id": str(p.get("productId")),
                "coin": p.get("coin"),
                "amount": _opt_float(p.get("amount")),
                "status": p.get("status"),
            })

    # DRY_RUN-only simulated balance substitution.
    balance_source = "real"
    sim = cfg.get("SIMULATED_IDLE_BALANCE")
    if cfg.get("DRY_RUN") and sim is not None:
        balances_summary = [{"coin": coin, "wallet_balance": float(sim), "equity": float(sim),
                             "_note": "simulated (DRY_RUN)"} for coin in whitelist]
        real_idle_per_coin = {coin: float(sim) for coin in whitelist}
        balance_source = "simulated"

    snapshot_meta = {
        "ts": snapshot_ts,
        "product_fetch_ts_ms": product_fetch_ts_ms,
        "account_type": cfg["ACCOUNT_TYPE"],
        "balance_source": balance_source,
        "stale_threshold_ms": STALE_SNAPSHOT_MS,
    }
    return {"scan": scan, "positions": positions, "balances": balances_summary,
            "filtered": filtered, "snapshot_meta": snapshot_meta,
            "idle_per_coin": real_idle_per_coin, "product_status": product_status}


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
        partial = e.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        return partial, session_id
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
    kept = [d for d in decisions if d.get("action") != "STAKE"]
    dropped = len(decisions) - len(kept)
    alerts = [f"RISK_GATE_DROPPED_STAKE: {dropped} STAKE dropped under {effective_state}"] if dropped else []
    return kept, alerts


def unwind_decisions(cfg: Dict[str, Any]) -> List[Dict]:
    return [{"action": "REDEEM_ALL", "coin": coin, "origin": "wrapper",
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
               positions: List[Dict], idle_per_coin: Dict[str, float]) -> Tuple[List[Dict], List[Dict]]:
    """Turn decisions into executable orders.  Returns (orders, skipped),
    where `skipped` are execution-shaped records explaining why a decision
    produced no order."""
    scan_by_id = {p["product_id"]: p for p in scan}
    held: Dict[str, Decimal] = {}
    for p in positions:
        held[p["product_id"]] = held.get(p["product_id"], Decimal(0)) + (_dec(p.get("amount")) or Decimal(0))
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
                if d.get("coin") in (None, p.get("coin")):
                    redeem(p["product_id"], p.get("coin"), origin, str(d.get("reason", "")))
        elif action == "STAKE":
            pid = d["product_id"]
            if pid in staked:
                skip(d, "duplicate STAKE for this product in the same cycle")
                continue
            product = scan_by_id[pid]
            coin = product["coin"]
            amount, why = compute_stake_amount(idle.get(coin, Decimal(0)),
                                               held.get(pid, Decimal(0)), product, cfg)
            if amount is None:
                skip(d, why)
                continue
            staked.add(pid)
            idle[coin] = idle.get(coin, Decimal(0)) - amount
            orders.append({"action": "STAKE", "coin": coin, "product_id": pid,
                           "amount": _fmt(amount), "origin": origin,
                           "reason": f"{d.get('reason', '')} | amount: {why}"})
    return orders, skipped


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


def _apr_ma_24h(hist: List[Dict]) -> Optional[float]:
    """Last 24 hourly points → simple mean."""
    recents = []
    for h in hist[-24:]:
        v = h.get("apr", "0")
        try:
            recents.append(_parse_pct(v))
        except ValueError:
            continue
    if not recents:
        return None
    return sum(recents) / len(recents)


# --------------------------------------------------------------------------- #
# The cycle                                                                   #
# --------------------------------------------------------------------------- #

def run_cycle(cfg: Dict[str, Any], tool, agent: Agent = call_agent) -> Tuple[Dict, int]:
    """Run one cycle, write its decision record, return (record, exit_code)."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    cycle_start = time.monotonic()
    log_dir = Path(cfg.get("LOG_DIR") or settings.default_log_dir())
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
    }

    def finish(rc: int) -> Tuple[Dict, int]:
        rec["cycle_duration_seconds"] = round(time.monotonic() - cycle_start, 3)
        if rec["cycle_duration_seconds"] > CYCLE_LATENCY_ALERT_S:
            alerts.append(f"CYCLE_LATENCY_HIGH: cycle took {rec['cycle_duration_seconds']}s "
                          f"(> {CYCLE_LATENCY_ALERT_S}s); will start losing cycles silently")
        write_decision_log(log_dir, rec)
        return rec, rc

    # Required config -> hard fail before anything else.
    for param in MANDATORY_PARAMS:
        if cfg.get(param) is None:
            alerts.extend([f"CONFIG_INCOMPLETE: '{param}' is null", "CRITICAL"])
            rec["decisions"] = [{"action": "ALERT_ONLY",
                                 "reason": f"CRITICAL: Required config parameter '{param}' is null or missing"}]
            return finish(3)

    # Risk state: never terminates the cycle (T1.2).
    effective, risk_meta, risk_alerts = resolve_risk_state()
    rec["risk_state"], rec["risk_state_meta"] = effective, risk_meta
    alerts.extend(risk_alerts)

    try:
        data = collect_inputs(tool, cfg)
    except Exception as e:
        alerts.append("CONFIG_INCOMPLETE: data collection error")
        rec["decisions"] = [{"action": "ALERT_ONLY", "reason": f"data collection failed: {e}"}]
        return finish(2)
    scan, positions = data["scan"], data["positions"]
    rec["filtered_by_wrapper"] = data["filtered"]
    rec["snapshot_meta"] = data["snapshot_meta"]
    rec["balance_source"] = data["snapshot_meta"]["balance_source"]

    if not scan and not positions:
        alerts.append("CONFIG_INCOMPLETE: no products in COIN_WHITELIST survived filtering")
        rec["decisions"] = [{"action": "HOLD", "reason": "no whitelisted products available"}]
        return finish(0)

    if effective == "UNWIND":
        # The kill switch does not depend on a model.
        decisions = unwind_decisions(cfg)
    else:
        path = prompt_path(cfg)
        if not path.is_file():
            alerts.extend([f"CONFIG_INCOMPLETE: prompt file {path.name} not found", "CRITICAL"])
            rec["decisions"] = [{"action": "ALERT_ONLY", "reason": f"prompt file {path.name} missing"}]
            return finish(3)
        template = path.read_bytes()
        rec["prompt_file"] = path.name
        rec["prompt_sha256"] = hashlib.sha256(template).hexdigest()
        prompt = compose_prompt(template.decode("utf-8"), cycle_id, cfg, effective, risk_meta,
                                scan, data["balances"], positions, list(alerts))

        rec["agent_called"] = True
        try:
            raw, rec["session_id"] = agent(prompt, cfg, cycle_id)
            agent_out = extract_json(raw, cycle_id)
        except Exception as e:
            alerts.append(f"AGENT_PARSE_ERROR: {e}")
            agent_out = {"decisions": [{"action": "ALERT_ONLY", "reason": "agent output unparseable"}]}

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
        max_scan_age_sec = cfg.get("MAX_SCAN_AGE_SECONDS", 900)
        scan_age_seconds = (int(time.time() * 1000) - data["snapshot_meta"]["product_fetch_ts_ms"]) // 1000
        if scan_age_seconds > max_scan_age_sec:
            alerts.append(f"STALE_SCAN: live scan age {scan_age_seconds}s > {max_scan_age_sec}s threshold")
            decisions = [{"action": "ALERT_ONLY", "reason": f"STALE_SCAN: live scan age {scan_age_seconds}s"}]

    # Deterministic gates — the last word before execution.
    decisions, gate_alerts = apply_risk_gate(decisions, effective)
    alerts.extend(gate_alerts)
    forced = unavailable_redeems(positions, data["product_status"])
    decisions = forced + decisions
    rec["decisions"] = decisions

    orders, skipped = build_plan(decisions, cfg, scan, positions, data["idle_per_coin"])
    rec["plan"] = orders
    executor = Executor(bybit_tool=tool, dry_run=cfg.get("DRY_RUN", True),
                        allow_new_positions=(effective == "NORMAL"))
    rec["executions"] = executor.execute(orders) + skipped
    return finish(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=settings.config_file())
    parser.add_argument("--dry-run", action="store_true",
                        help="override config DRY_RUN and force dry-run")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.dry_run:
        cfg["DRY_RUN"] = True

    # Safety check: SIMULATED_IDLE_BALANCE + DRY_RUN=false is forbidden.
    if not cfg.get("DRY_RUN", True) and cfg.get("SIMULATED_IDLE_BALANCE") is not None:
        raise SystemExit(
            "CRITICAL: SIMULATED_IDLE_BALANCE is set but DRY_RUN=false. "
            "Clear SIMULATED_IDLE_BALANCE before going live, or the cycle "
            "will pretend to have fake capital while moving real funds."
        )

    rec, rc = run_cycle(cfg, tool=BybitEarnTool(), agent=call_agent)
    print(f"[run_yield_cycle] cycle_id={rec['cycle_id']}")
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    sys.exit(rc)


if __name__ == "__main__":
    main()
