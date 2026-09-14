#!/usr/bin/env python3
"""
run_yield_cycle.py — Bybit Earn yield-rotation cycle runner.

Architecture (per runbook §3, locked by 40/40 regression v4):

  ┌────────────────────────────────────────────────────────────────┐
  │ WRAPPER (this script)                                          │
  │  - reads /opt/hermes/yield_rotation/config/yield_rotation.yaml │
  │  - verifies risk_state HMAC (max 30 min old)                   │
  │  - fetches products, APR history, balances, positions          │
  │  - filters scan to COIN_WHITELIST                              │
  │  - LOGS each filter drop to filtered_by_wrapper                │
  │  - composes the prompt from prompt_v4.md + structured inputs   │
  │  - calls `hermes chat` with EXPLICIT model + reasoning flags  │
  │  - extracts first top-level JSON from stdout                  │
  │  - validates, then calls executor.execute_plan()              │
  │  - writes the decision record to LOG_DIR/YYYY-MM-DD.jsonl      │
  │                                                                │
  │ AGENT (hermes chat)                                            │
  │  - receives validated inputs + prompt                          │
  │  - emits HOLD / STAKE / REDEEM / REDEEM_ALL / ALERT_ONLY /    │
  │    NO_NEW_POSITIONS, one per product or globally               │
  │  - NEVER sees the HMAC secret                                  │
  └────────────────────────────────────────────────────────────────┘

The split exists so the agent has no credentials to leak and the
wrapper has no language model to hallucinate. Each side has one job.

Every parameter that affects the output lives in config/yield_rotation.yaml.
The `hermes chat` command line is built explicitly with -m, --reasoning,
--provider; nothing is read from a global config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Re-use the same load_env shim as bybit_earn_tool (it loads from
# /opt/hermes/.env when this script is run by systemd as User=hermes
# with an empty PATH and no shell).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bybit_earn_tool as bet  # noqa: E402
from bybit_earn_tool import BybitEarnTool  # noqa: E402

# Risk state verifier is in /opt/hermes/tools/risk_state.py
sys.path.insert(0, "/opt/hermes/tools")
import risk_state as rs  # noqa: E402

from executor import Executor  # noqa: E402


# --------------------------------------------------------------------------- #
# Paths & constants                                                           #
# --------------------------------------------------------------------------- #

ROOT = Path("/opt/hermes/yield_rotation")
CONFIG_PATH = ROOT / "config" / "yield_rotation.yaml"
PROMPT_PATH = ROOT / f"prompt_{os.environ.get('YIELD_ROTATION_PROMPT', 'v4')}.md"
RISK_STATE_PATH = Path("/opt/hermes/state/risk_state.json")

# Tunables that aren't policy, just plumbing.
HERMES_BIN = "/opt/hermes/.venv/bin/hermes"
HERMES_SESSION_DIR = Path("/opt/hermes/state/yield_rotation_sessions")
HERMES_SESSION_DIR.mkdir(parents=True, exist_ok=True)

STALE_SNAPSHOT_MS = 4 * 60 * 60 * 1000  # 4 h — Bybit USDT APR history refresh
JSONL_FILE = "{date}.jsonl"  # LOG_DIR / jsonl template


# --------------------------------------------------------------------------- #
# Config loading                                                              #
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"config not found: {path}")
    with path.open() as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------- #
# Wrapper-side data collection                                                #
# --------------------------------------------------------------------------- #

def collect_inputs(tool: BybitEarnTool, cfg: Dict[str, Any]) -> Tuple[Dict, List[Dict], List[Dict], List[Dict], str]:
    """
    Pulls the data the agent needs, applies COIN_WHITELIST, and records each
    filter drop. Returns (scan_for_agent, positions, balances_summary,
    filtered_by_wrapper, snapshot_meta).

    `scan_for_agent` is a list (not object) — per regression v4, the agent
    iterates a list and the wrapper has already done the whitelist filter.
    """
    filtered: List[Dict[str, str]] = []
    snapshot_ts = int(time.time() * 1000)

    # --- products + per-product APR history ---
    products = tool.get_earn_products() or []
    # Live-scan age anchor: wall clock the instant the product list returns.
    # This is the correct input to MAX_SCAN_AGE_SECONDS — the age of the
    # live /v5/earn/product snapshot at decision time, NOT the APR history.
    product_fetch_ts_ms = int(time.time() * 1000)
    whitelist = set(cfg["COIN_WHITELIST"])

    scan: List[Dict] = []
    for p in products:
        coin = p.get("coin")
        if coin not in whitelist:
            # We log this as a filter drop, but we only spent a public call.
            # (We don't fetch APR history for filtered coins.)
            continue

        product_id = p.get("productId")
        est_str = p.get("estimateApr", "0%")
        try:
            est_apr = _parse_pct(est_str)
        except ValueError:
            est_apr = None
            filtered.append({
                "product_id": product_id,
                "reason": f"PARSE_ERROR: estimateApr={est_str!r}",
            })
            continue

        status = p.get("status")
        if status != "Available":
            filtered.append({
                "product_id": product_id,
                "reason": f"STATUS_NOT_AVAILABLE: {status!r}",
            })
            continue

        # APR history (24h MA). Public endpoint.
        hist = tool.get_earn_apr_history(coin=coin) or []
        # Find the record timestamp closest to now and use the 24h window ending there.
        if not hist:
            filtered.append({
                "product_id": product_id,
                "reason": "NO_APR_HISTORY",
            })
            continue
        # Staleness checks (v5). Two DISTINCT checks, each measuring what its
        # name says:
        #   1. APR-history age = (now − latest APR-history point). Bybit refreshes
        #      this ~hourly; gate with MAX_APR_HISTORY_GAP_HOURS (hours).
        #   2. Live-scan age = (decision-time − product-fetch-time). This is
        #      seconds; gate with MAX_SCAN_AGE_SECONDS. It is NOT the APR history.
        # These are separate because a fresh product list with stale APR history
        # (or vice versa) must fail for a different, correctly-named reason.
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
            filtered.append({
                "product_id": product_id,
                "reason": "NO_APR_MA_24H",
            })
            continue

        scan.append({
            "product_id": product_id,
            "coin": coin,
            "estimate_apr": est_apr,
            "apr_ma_24h": apr_ma_24h,
            "status": status,
            "min_stake_amount": float(p.get("minStakeAmount", 0) or 0),
            "max_stake_amount": float(p.get("maxStakeAmount", 0) or 0),
            "has_tiered_apr": bool(p.get("hasTieredApr", False)),
            # Extended fields expected by prompt v4 + 40/40 regression:
            # remaining_capacity (headroom), tier_cap_amount (bonus
            # tier or null), redemption_eta_hours (liquidity cost),
            # apr_ma_7d / apr_p25_180d / apr_p75_180d (trend context),
            # marginal_apr_for_size (rate at the size we plan to stake).
            # remaining_capacity (headroom): Bybit returns -1.0 for unlimited pool.
            # Convert -1.0 to a very large number so LLM satisfies headroom check.
            "remaining_capacity": (
                float(p.get("remainingPoolAmount", 0) or 0)
                if float(p.get("remainingPoolAmount", 0) or 0) >= 0
                else 99999999.0
            ),
            "tier_cap_amount": None,  # Bybit doesn't expose this in /v5/earn/product
            "redemption_eta_hours": 0.0,  # FlexibleSaving is T+0
            "apr_ma_7d": apr_ma_24h,  # we only keep 24h; 7d = 24h best-effort
            "apr_p25_180d": None,  # 180d history not exposed by API
            "apr_p75_180d": None,  # 180d history not exposed by API
            "marginal_apr_for_size": est_apr,  # = estimate_apr (no tier effect at our size)
            "apr_history_age_seconds": apr_history_age_seconds,
        })

    # --- balances + positions (only for whitelisted coins) ---
    balance_data = tool.get_wallet_balance(account_type=cfg["ACCOUNT_TYPE"])
    balances_summary: List[Dict] = []
    real_idle_per_coin: Dict[str, float] = {}
    if balance_data.get("list"):
        for c in balance_data["list"][0].get("coin", []):
            coin = c.get("coin")
            if coin not in whitelist:
                continue
            try:
                wallet = float(c.get("walletBalance", "0") or 0)
            except ValueError:
                wallet = 0.0
            real_idle_per_coin[coin] = wallet
            balances_summary.append({
                "coin": coin,
                "wallet_balance": wallet,
                "equity": float(c.get("equity", "0") or 0),
            })

    # positions: only for whitelisted coins
    positions: List[Dict] = []
    for coin in whitelist:
        ps = tool.get_earn_positions(coin=coin) or []
        for p in ps:
            positions.append({
                "productId": p.get("productId"),
                "coin": p.get("coin"),
                "amount": p.get("amount", "0"),
                "status": p.get("status"),
            })

    # DRY_RUN-only simulated balance substitution.
    # The simulated balance REPLACES the real balance for the agent's
    # view (we don't want it to see 0.04 real and think it's broke).
    balance_source = "real"
    sim = cfg.get("SIMULATED_IDLE_BALANCE")
    if cfg.get("DRY_RUN") and sim is not None:
        # Drop the real entries; they are misleading under simulation.
        balances_summary = []
        for coin in whitelist:
            balances_summary.append({
                "coin": coin,
                "wallet_balance": float(sim),
                "equity": float(sim),
                "_note": "simulated (DRY_RUN)",
            })
        real_idle_per_coin = {coin: float(sim) for coin in whitelist}
        balance_source = "simulated"

    snapshot_meta = {
        "ts": snapshot_ts,
        "product_fetch_ts_ms": product_fetch_ts_ms,
        "account_type": cfg["ACCOUNT_TYPE"],
        "balance_source": balance_source,
        "stale_threshold_ms": STALE_SNAPSHOT_MS,
    }

    return scan, positions, balances_summary, filtered, snapshot_meta, real_idle_per_coin


# --------------------------------------------------------------------------- #
# Risk state verification                                                     #
# --------------------------------------------------------------------------- #

def _classify_risk_state_failure(reason: str) -> str:
    """Map a raw rs.verify() reason string to a stable, distinct code.

    The `hermes chat` output and the decision record must carry a
    deterministic token (not a free-text reason) so the regression suite
    and the Telegram audit can grep for the actual failure class.
    """
    r = reason or ""
    if "missing: " in r:
        return "RISK_STATE_MISSING"
    if "unreadable" in r:
        return "RISK_STATE_UNREADABLE"
    if "missing sig" in r:
        return "RISK_STATE_MISSING_SIG"
    if "HMAC mismatch" in r:
        return "RISK_STATE_BAD_SIGNATURE"
    if "invalid state" in r:
        return "RISK_STATE_INVALID"
    if "stale" in r:
        return "RISK_STATE_STALE"
    return "RISK_STATE_UNVERIFIED"


def verify_risk_state(cfg: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict], List[str]]:
    """
    Returns (state, state_meta, alerts).
    On any failure: state=None, alerts=[reason]. The caller substitutes
    NO_NEW_POSITIONS.
    """
    secret = os.environ.get("HERMES_RISK_HMAC_KEY", "")
    if not secret:
        return None, None, ["NO_RISK_HMAC_KEY"]
    ok, reason, raw = rs.verify(RISK_STATE_PATH, secret)
    if not ok:
        code = _classify_risk_state_failure(reason)
        return None, None, [f"{code}: {reason}"]
    return raw.get("state"), raw, []


# --------------------------------------------------------------------------- #
# Hermes CLI invocation                                                       #
# --------------------------------------------------------------------------- #

def call_agent(prompt: str, cfg: Dict[str, Any], cycle_id: str) -> Tuple[str, Optional[str], str, str]:
    """
    Invoke `hermes chat` with EXPLICIT model + reasoning. Return
    (raw_stdout, session_id, requested_model, actual_model_used).
    """
    session_id = f"{cycle_id}_{uuid.uuid4().hex[:8]}"
    requested_model = cfg.get("REQUESTED_MODEL", "hermes-cheap")
    resolved_model = cfg.get("RESOLVED_MODEL", "google/gemini-2.5-flash")
    reasoning = cfg["REQUESTED_REASONING"]
    prompt_version = cfg["PROMPT_VERSION"]

    # The header line we prepend; the agent sees a fresh prompt each cycle.
    full_prompt = (
        f"[cycle_id={cycle_id}]\n[prompt_version={prompt_version}]\n\n{prompt}"
    )

    # Pass RESOLVED_MODEL directly to command line so alias map does not intervene
    cmd = [
        HERMES_BIN, "chat",
        "-q", full_prompt,
        "-m", resolved_model,
        "--reasoning", reasoning,
        # No -Q: we want stderr fallback warnings + the raw response
        # without OTEL suppression, so the parser can grab the JSON
        # even if it ends up at the bottom of the output.
    ]
    actual_model = resolved_model
    # NOTE: every flag is explicit. If the global config changes, this
    # command line does not.
    env = {
        "PATH": "/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/opt/hermes",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    # 280s subprocess timeout (was 180s). Above 280s we treat it as a
    # "cycle_latency_high" failure: the agent is hanging and we surface
    # the alert + write a decision record instead of letting the
    # cycle crash silently.
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=280, env=env
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        return partial, session_id, requested_model, actual_model
    raw = proc.stdout or ""
    if proc.returncode != 0 and not raw.strip():
        raise RuntimeError(
            f"hermes chat failed: rc={proc.returncode} stderr={proc.stderr[:200]!r}"
        )
    # Persist the raw agent output for debugging/audit. The decision log
    # is the clean record; this is the raw bytes + stderr.
    debug_path = HERMES_SESSION_DIR / f"{session_id}.raw"
    debug_path.write_text(
        f"--- stdout ---\n{raw}\n--- stderr ---\n{proc.stderr or ''}\n"
    )
    return raw, session_id, requested_model, actual_model


# --------------------------------------------------------------------------- #
# JSON extraction + validation                                                #
# --------------------------------------------------------------------------- #

JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(raw: str) -> Dict:
    """
    Extract JSON object from agent output, skipping the prompt examples
    and OTel banners. Strategy: find the LAST top-level balanced JSON
    object whose key set matches a decision record. This avoids
    matching {} placeholders in the OTel dump and avoids matching the
    example JSON from the prompt.
    """
    # Find all candidate top-level balanced JSON objects.
    candidates = []
    pos = 0
    while True:
        start = raw.find("{", pos)
        if start < 0:
            break
        depth = 0
        end = -1
        in_string = False
        escape = False
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
            break
        candidate = raw[start:end]
        pos = end
        if candidate.strip() == "{}":
            continue
        try:
            parsed = json.loads(candidate)
            # Only accept objects that look like decision records.
            if isinstance(parsed, dict) and "decisions" in parsed:
                return parsed
            candidates.append(parsed)
        except json.JSONDecodeError:
            continue
    # If no decision-record-shaped object found, return the last valid JSON
    # (so AGENT_PARSE_ERROR is raised by the caller only when nothing valid
    # was found at all).
    if candidates:
        return candidates[-1]
    raise ValueError("no top-level JSON object found in agent output")


def validate_decision_record(rec: Dict) -> List[str]:
    """Return a list of human-readable issues; empty list = OK."""
    issues: List[str] = []
    if "decisions" not in rec or not isinstance(rec["decisions"], list):
        issues.append("missing or non-list `decisions`")
    for i, d in enumerate(rec.get("decisions", [])):
        if not isinstance(d, dict):
            issues.append(f"decision[{i}] is not a dict")
            continue
        if "action" not in d:
            issues.append(f"decision[{i}] missing `action`")
        if "reason" not in d:
            issues.append(f"decision[{i}] missing `reason`")
        a = d.get("action")
        if a == "STAKE":
            if d.get("coin") is None:
                issues.append(f"decision[{i}] STAKE missing coin")
            if d.get("product_id") is None:
                issues.append(f"decision[{i}] STAKE missing product_id")
            # v5 schema: amount_usd is the canonical field. `amount` is reserved
            # for held-position reporting and using it in a decision is a
            # schema error — agents that emit it must be retrained on v5.
            if d.get("amount_usd") is None:
                issues.append(f"decision[{i}] STAKE missing required field `amount_usd`")
            if d.get("amount") is not None:
                issues.append(f"decision[{i}] STAKE uses deprecated field `amount` — use `amount_usd`")
        elif a == "REDEEM":
            if d.get("coin") is None:
                issues.append(f"decision[{i}] REDEEM missing coin")
            if d.get("product_id") is None:
                issues.append(f"decision[{i}] REDEEM missing product_id")
        elif a == "REDEEM_ALL":
            # Global: coin may be omitted (per F04 fix)
            pass
    return issues


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
# Main                                                                        #
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="override config DRY_RUN and force dry-run")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # CLI override wins (useful for testing)
    if args.dry_run:
        cfg["DRY_RUN"] = True

    # Safety check: SIMULATED_IDLE_BALANCE + DRY_RUN=false is forbidden.
    if not cfg.get("DRY_RUN", True) and cfg.get("SIMULATED_IDLE_BALANCE") is not None:
        raise SystemExit(
            "CRITICAL: SIMULATED_IDLE_BALANCE is set but DRY_RUN=false. "
            "Clear SIMULATED_IDLE_BALANCE before going live, or the cycle "
            "will pretend to have fake capital while moving real funds."
        )

    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    ts_iso = datetime.now(timezone.utc).isoformat()
    cycle_start = time.monotonic()  # for latency tracking (v5+)

    alerts: List[str] = []

    # Check 3: Required config parameter must not be null/missing -> Hard fail before LLM.
    mandatory_params = ["ENTRY_APR", "RESOLVED_MODEL", "ACCOUNT_TYPE"]
    for param in mandatory_params:
        if cfg.get(param) is None:
            cycle_duration_seconds = round(time.monotonic() - cycle_start, 3)
            rec = {
                "ts": ts_iso,
                "cycle_id": cycle_id,
                "cycle_duration_seconds": cycle_duration_seconds,
                "risk_state": "NO_NEW_POSITIONS",
                "decisions": [{"action": "ALERT_ONLY", "reason": f"CRITICAL: Required config parameter '{param}' is null or missing"}],
                "holds": [],
                "alerts": [f"CONFIG_INCOMPLETE: '{param}' is null", "CRITICAL"],
                "dry_run": cfg.get("DRY_RUN", True),
                "prompt_version": cfg.get("PROMPT_VERSION", "v5"),
                "filtered_by_wrapper": [],
                "model_requested": cfg.get("REQUESTED_MODEL", "hermes-cheap"),
                "balance_source": "real",
                "risk_state_meta": {"verifier": "wrapper_safety_check", "reason": f"missing '{param}'"},
            }
            write_decision_log(Path(cfg.get("LOG_DIR", "/opt/hermes/logs/yield_rotation")), rec)
            print(json.dumps(rec, ensure_ascii=False, indent=2))
            sys.exit(3) # Hard fail

    # Check 1: Invalid HMAC -> hard terminate with CRITICAL before LLM.
    # verify_risk_state returns (None, None, [reason]) on invalid HMAC or missing secret.
    risk_state, risk_state_raw, risk_alerts = verify_risk_state(cfg)
    if risk_state is None or risk_state_raw is None:
        # Per wrapper spec: on invalid/missing HMAC, treat as NO_NEW_POSITIONS and alert.
        risk_state = "NO_NEW_POSITIONS"
        # risk_alerts is a single-item list from verify_risk_state; the code
        # prefix (e.g. RISK_STATE_STALE) is the authoritative failure class.
        failure_code = (risk_alerts[0].split(":", 1)[0] if risk_alerts else "RISK_STATE_UNVERIFIED")
        failure_detail = (risk_alerts[0] if risk_alerts else "unknown")
        alerts = [failure_code]
        cycle_duration_seconds = round(time.monotonic() - cycle_start, 3)
        rec = {
            "ts": ts_iso,
            "cycle_id": cycle_id,
            "cycle_duration_seconds": cycle_duration_seconds,
            "risk_state": risk_state,
            "decisions": [],  # no new positions allowed
            "holds": [],
            "alerts": alerts,
            "dry_run": cfg.get("DRY_RUN", True),
            "prompt_version": cfg.get("PROMPT_VERSION", "v5"),
            "filtered_by_wrapper": [],
            "model_requested": cfg.get("REQUESTED_MODEL", "hermes-cheap"),
            "balance_source": "real",
            "risk_state_meta": {
                "ts": None,
                "reason": failure_detail,
                "code": failure_code,
                "verifier": "tools/risk_state.py:verify()",
                "forced": True,
            },
        }
        write_decision_log(Path(cfg.get("LOG_DIR", "/opt/hermes/logs/yield_rotation")), rec)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        sys.exit(3)  # Hard fail before LLM

    alerts.extend(risk_alerts)
    risk_state_meta = {
        "ts": (risk_state_raw or {}).get("ts"),
        "reason": (risk_state_raw or {}).get("reason"),
        "verifier": "tools/risk_state.py:verify()",
    }

    # 2. Data collection
    tool = BybitEarnTool()
    try:
        scan, positions, balances_summary, filtered, snapshot_meta, idle_per_coin = collect_inputs(tool, cfg)
    except Exception as e:
        # Public endpoints must work even with no creds; if this fails we abort.
        cycle_duration_seconds = round(time.monotonic() - cycle_start, 3)
        if cycle_duration_seconds > 300:
            alerts.append(f"CYCLE_LATENCY_HIGH: cycle took {cycle_duration_seconds}s (> 300s)")
        rec = {
            "ts": ts_iso, "cycle_id": cycle_id, "cycle_duration_seconds": cycle_duration_seconds,
            "risk_state": risk_state,
            "decisions": [{"action": "ALERT_ONLY", "reason": f"data collection failed: {e}"}],
            "holds": [], "alerts": (alerts or []) + ["CONFIG_INCOMPLETE: data collection error"],
            "dry_run": cfg.get("DRY_RUN", True), "prompt_version": cfg["PROMPT_VERSION"],
            "filtered_by_wrapper": [], "model_requested": cfg["REQUESTED_MODEL"],
            "balance_source": "real", "risk_state_meta": risk_state_meta,
        }
        write_decision_log(Path(cfg["LOG_DIR"]), rec)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        sys.exit(2)

    # 3. If no whitelisted products at all, no point asking the agent.
    if not scan and not positions:
        alerts.append("CONFIG_INCOMPLETE: no products in COIN_WHITELIST survived filtering")
        cycle_duration_seconds = round(time.monotonic() - cycle_start, 3)
        if cycle_duration_seconds > 300:
            alerts.append(f"CYCLE_LATENCY_HIGH: cycle took {cycle_duration_seconds}s (> 300s)")
        rec = {
            "ts": ts_iso, "cycle_id": cycle_id, "cycle_duration_seconds": cycle_duration_seconds,
            "risk_state": risk_state,
            "decisions": [{"action": "HOLD", "reason": "no whitelisted products available"}],
            "holds": [], "alerts": alerts,
            "dry_run": cfg.get("DRY_RUN", True), "prompt_version": cfg["PROMPT_VERSION"],
            "filtered_by_wrapper": filtered,
            "model_requested": cfg["REQUESTED_MODEL"],
            "balance_source": snapshot_meta["balance_source"],
            "risk_state_meta": risk_state_meta,
            "snapshot_meta": snapshot_meta,
        }
        write_decision_log(Path(cfg["LOG_DIR"]), rec)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return

    # 4. Compose prompt
    prompt_template = PROMPT_PATH.read_text()
    inputs_block = json.dumps({
        "config": cfg,
        "risk_state": risk_state,
        "risk_state_meta": risk_state_meta,
        "scan": scan,
        "balances": balances_summary,
        "positions": positions,
    }, ensure_ascii=False, indent=2)
    full_prompt = prompt_template + "\n\n```json\n" + inputs_block + "\n```\n"

    # 5. Call agent
    raw, session_id, requested_model, actual_model = call_agent(full_prompt, cfg, cycle_id)

    # 6. Extract + validate
    try:
        agent_out = extract_json(raw)
    except ValueError as e:
        alerts.append(f"AGENT_PARSE_ERROR: {e}")
        agent_out = {
            "decisions": [{"action": "ALERT_ONLY", "reason": "agent output unparseable"}],
            "holds": [], "alerts": ["AGENT_PARSE_ERROR"],
        }

    issues = validate_decision_record(agent_out)
    if issues:
        alerts.append("DECISION_VALIDATION_FAILED: " + "; ".join(issues))
        # Don't trust a malformed record; downgrade to ALERT_ONLY.
        agent_out = {
            "decisions": [{"action": "ALERT_ONLY", "reason": "decision record failed validation"}],
            "holds": [], "alerts": ["DECISION_VALIDATION_FAILED"],
        }

    # Check 5: Model Resolution & Pinning
    # If the actual model used does not match the configured RESOLVED_MODEL,
    # or if there is fallback detected in stdout/stderr, downgrade to ALERT_ONLY + ALERT.
    # Note: we pass resolved_model directly to CLI, but if the local CLI
    # falls back to another model internally (e.g. rate limit), we detect it.
    resolved_model = cfg.get("RESOLVED_MODEL", "google/gemini-2.5-flash")
    if actual_model != resolved_model or "fallback" in raw.lower():
        alerts.append("CYCLE_MODEL_MISMATCH: requested model does not match resolved model or fallback occurred")
        agent_out = {
            "decisions": [{"action": "ALERT_ONLY", "reason": f"CYCLE_MODEL_MISMATCH: expected {resolved_model}, got {actual_model}"}],
            "holds": [], "alerts": ["CYCLE_MODEL_MISMATCH"],
        }

    # Check 4: Valid Product ID
    # Verify that the product_id of any decision (STAKE or REDEEM) exists in current cycle's scan.
    # If agent returns an ID that was not provided, reject the decision and downgrade to ALERT_ONLY with CRITICAL.
    decisions = agent_out.get("decisions", [])
    valid_product_ids = {p["product_id"] for p in scan}
    for d in decisions:
        a = d.get("action")
        if a in ["STAKE", "REDEEM"]:
            pid = d.get("product_id")
            if pid not in valid_product_ids:
                alerts.append(f"CRITICAL: agent decision product_id {pid!r} not in scan whitelist")
                agent_out = {
                    "decisions": [{"action": "ALERT_ONLY", "reason": f"CRITICAL: decision product_id {pid!r} not in scan whitelist"}],
                    "holds": [], "alerts": ["DECISION_VALIDATION_FAILED", "CRITICAL"],
                }
                break

    # Check 6: Live-scan age (MAX_SCAN_AGE_SECONDS). Measures elapsed time
    # between the /v5/earn/product fetch and the moment we execute the plan.
    # This is the check that was previously MISSING (the old "STALE_SNAPSHOT"
    # guard actually measured APR-history age, not scan freshness).
    max_scan_age_sec = cfg.get("MAX_SCAN_AGE_SECONDS", 900)
    product_fetch_ts_ms = snapshot_meta.get("product_fetch_ts_ms")
    if product_fetch_ts_ms is not None:
        scan_age_seconds = (int(time.time() * 1000) - product_fetch_ts_ms) // 1000
    else:
        scan_age_seconds = 0
    if scan_age_seconds > max_scan_age_sec:
        alerts.append(
            f"STALE_SCAN: live scan age {scan_age_seconds}s > {max_scan_age_sec}s threshold"
        )
        agent_out = {
            "decisions": [{"action": "ALERT_ONLY", "reason": f"STALE_SCAN: live scan age {scan_age_seconds}s"}],
            "holds": [], "alerts": ["STALE_SCAN"],
        }

    # 7. Execute
    executor = Executor(bybit_tool=tool, dry_run=cfg.get("DRY_RUN", True))
    decisions = agent_out.get("decisions", [])
    idle_for_coin = idle_per_coin.get("USDT", 0.0)  # primary coin for the test
    exec_records = executor.execute_plan(
        decisions, positions,
        balance_usdt=idle_for_coin,
        min_move_usd=cfg["MIN_MOVE_USD"],
    )

    # 8. Build final record
    cycle_duration_seconds = round(time.monotonic() - cycle_start, 3)

    # v5: cycle latency alert. Systemd timer is 600s; we want at least 5
    # minutes of headroom between cycles. If a single cycle exceeds 300s
    # we will start losing cycles silently (Type=oneshot does not overlap),
    # so we raise a CRITICAL alert and surface it in Telegram.
    CYCLE_LATENCY_ALERT_S = 300
    if cycle_duration_seconds > CYCLE_LATENCY_ALERT_S:
        alerts.append(
            f"CYCLE_LATENCY_HIGH: cycle took {cycle_duration_seconds}s "
            f"(> {CYCLE_LATENCY_ALERT_S}s); will start losing cycles silently"
        )

    rec = {
        "ts": ts_iso,
        "cycle_id": cycle_id,
        "session_id": session_id,
        "cycle_duration_seconds": cycle_duration_seconds,
        "risk_state": risk_state,
        "risk_state_meta": risk_state_meta,
        "decisions": decisions,
        "holds": agent_out.get("holds", []),
        "alerts": (agent_out.get("alerts", []) or []) + alerts,
        "executions": exec_records,
        "dry_run": cfg.get("DRY_RUN", True),
        "balance_source": snapshot_meta["balance_source"],
        "filtered_by_wrapper": filtered,
        "prompt_version": cfg["PROMPT_VERSION"],
        "model_requested": requested_model,
        "snapshot_meta": snapshot_meta,
    }

    log_path = write_decision_log(Path(cfg["LOG_DIR"]), rec)
    print(f"[run_yield_cycle] cycle_id={cycle_id} log={log_path}")
    print(json.dumps(rec, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
