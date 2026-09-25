#!/usr/bin/env python3
"""External heartbeat for risk_state.json — runs as a SEPARATE process/timer.

Purpose: keep a healthy system in NORMAL without giving the LLM, or the
cycle itself, any way to open the door.  The heartbeat is the only
automatic writer of risk_state.json; it never overrides an operator.

Decision table (FINISH_PLAN A1-B, T1.3, T1.4):

  Bybit API unreachable                         -> ABSTAIN
  last cycle carries a blocking code            -> ABSTAIN + alert
  risk_state file absent                        -> write NO_NEW_POSITIONS
                                                   (source=heartbeat_bootstrap)
  present but unverifiable (unreadable,
    malformed, bad HMAC, no key)                -> ABSTAIN + alert, never overwrite
  NO_NEW_POSITIONS with source=heartbeat_bootstrap
    and a verified clean cycle AFTER its ts     -> write NORMAL (heartbeat_renew)
  NORMAL + fresh                                -> nothing
  NORMAL + stale, scanner alive                 -> write NORMAL (heartbeat_renew)
  NORMAL + stale, scanner not alive             -> ABSTAIN + alert
  anything else (operator states, UNWIND,
    bootstrap without a clean cycle yet)        -> ABSTAIN (+ alert if stale)

ABSTAIN never writes.  "Verified clean cycle" = the newest cycle record has
no blocking code, verified THIS exact risk_state record (signature valid and
the same ts), and ran after it.

check_last_cycle_ok() blocks ONLY on BLOCKING_CODES.  It must NOT block on
RISK_STATE_*, RISK_GATE_*, STALE_SCAN or CYCLE_LATENCY_HIGH — those are
non-fatal, and blocking on them creates a deadlock.

LOG_DIR is read from the SAME config file as the wrapper.  A missing LOG_DIR
directory is a misconfig (exit 3 + alert), never treated as bootstrap.

Environment (settings.load_env: process env > /opt/hermes/.env; the shared
/opt/data/.env contributes TELEGRAM_BOT_TOKEN only):
  HERMES_RISK_HMAC_KEY  (required to sign/verify)
  BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET
  ALERT_TELEGRAM_CHAT_ID, TELEGRAM_BOT_TOKEN

Test-only switches (never set in production):
  YIELD_LOG_DIR          override dir containing *.jsonl
  YIELD_SKIP_API_CHECK=1 skip the Bybit reachability probe
  YIELD_SKIP_CONFIG_DCHECK=1  skip the "LOG_DIR must exist" error
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

import risk_state
import settings

MAX_AGE_MS = risk_state.MAX_AGE_MS

# The ONLY cycle-log codes that block the heartbeat. See module docstring.
BLOCKING_CODES = (
    "CONFIG_INCOMPLETE",
    "CRITICAL",
    "AGENT_PARSE_ERROR",
    "DECISION_VALIDATION_FAILED",
    "CYCLE_CRASH",
)


def load_config(path: Path) -> dict:
    """Load the SAME config/yield_rotation.yaml the wrapper uses."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_log_dir(cfg: dict) -> Path:
    """LOG_DIR from config (wrapper source of truth), with a test-only env
    override. Raises on a non-existent directory unless the test escape
    hatch is set."""
    cfg_dir = cfg.get("LOG_DIR") or str(settings.default_log_dir())
    override = os.environ.get("YIELD_LOG_DIR", "")
    chosen = Path(override) if override else Path(cfg_dir)
    missing = not chosen.is_dir()
    if missing and os.environ.get("YIELD_SKIP_CONFIG_DCHECK", "") != "1":
        raise FileNotFoundError(
            f"LOG_DIR from config {settings.config_file().name!r}: {chosen} does not exist. "
            f"This is a MISCONFIG — refusing to treat it as bootstrap."
        )
    return chosen


def check_bybit_api() -> bool:
    """Verify the Bybit public API is reachable."""
    try:
        from bybit_earn_tool import BybitEarnTool
        products = BybitEarnTool().get_earn_products()
        return bool(products and isinstance(products, list))
    except Exception:
        return False


def _is_blocking_code(alert: Any) -> Optional[str]:
    """Return the blocking code if this alert is a hard-fail, else None."""
    if not isinstance(alert, str):
        return None
    a = alert.strip()
    for code in BLOCKING_CODES:
        if a == code or a.startswith(code + ":"):
            return code
    return None


def last_cycle_record(log_dir: Path) -> Optional[Dict[str, Any]]:
    """The newest cycle record, or None if there is none / it is unreadable."""
    logs = sorted(log_dir.glob("*.jsonl"))
    if not logs:
        return None
    try:
        lines = logs[-1].read_text().strip().splitlines()
        rec = json.loads(lines[-1]) if lines else None
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def check_last_cycle_ok(log_dir: Path) -> Tuple[bool, Optional[str]]:
    """(ok, blocking_code). No readable cycle -> ok (nothing to block on)."""
    rec = last_cycle_record(log_dir)
    if rec is None:
        return True, None
    alerts = rec.get("alerts")
    if isinstance(alerts, str):
        alerts = [alerts]
    if not isinstance(alerts, list):
        return True, None
    for alert in alerts:
        code = _is_blocking_code(alert)
        if code is not None:
            return False, code
    return True, None


def _cycle_ts_ms(rec: Dict[str, Any]) -> Optional[int]:
    ts = rec.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def latest_cycle_age_ms(log_dir: Path) -> Optional[int]:
    """Age (ms) of the newest cycle record, or None if there is none."""
    rec = last_cycle_record(log_dir)
    ts = _cycle_ts_ms(rec) if rec else None
    return None if ts is None else int(time.time() * 1000) - ts


def scanner_alive(log_dir: Path, alive_window_ms: int) -> bool:
    """True iff the scanner produced a decision within the liveness window.
    The heartbeat renews only with EXTERNAL evidence the scanner is alive,
    never merely because the heartbeat itself runs."""
    age = latest_cycle_age_ms(log_dir)
    return age is not None and 0 <= age < alive_window_ms


def clean_cycle_verified(log_dir: Path, state_ts: int) -> bool:
    """True iff the newest cycle is non-blocking, verified exactly this
    risk_state record, and ran after it."""
    rec = last_cycle_record(log_dir)
    if rec is None or not check_last_cycle_ok(log_dir)[0]:
        return False
    meta = rec.get("risk_state_meta")
    if not isinstance(meta, dict):
        return False
    cycle_ts = _cycle_ts_ms(rec)
    return (meta.get("signature_valid") is True
            and meta.get("ts") == state_ts
            and cycle_ts is not None and cycle_ts > state_ts)


def send_telegram_alert(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a simple Telegram message via Bot API."""
    try:
        import urllib.parse
        import urllib.request
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    env = settings.load_env()
    state_file = settings.risk_state_file()
    config_file = settings.config_file()

    def alert(text: str) -> None:
        bot_token = env.get("TELEGRAM_BOT_TOKEN")
        chat_id = env.get("ALERT_TELEGRAM_CHAT_ID")
        if bot_token and chat_id:
            send_telegram_alert(bot_token, chat_id, text)

    hmac_key = env.get("HERMES_RISK_HMAC_KEY")
    if not hmac_key:
        print("[heartbeat] ERROR: HERMES_RISK_HMAC_KEY not set", file=sys.stderr)
        return 1

    try:
        cfg = load_config(config_file)
        log_dir = resolve_log_dir(cfg)
    except FileNotFoundError as e:
        print(f"[heartbeat] ERROR: {e}", file=sys.stderr)
        alert(f"🚨 HEARTBEAT ERROR: {e}")
        return 3  # distinct: misconfig, NOT bootstrap
    except Exception as e:
        print(f"[heartbeat] ERROR: cannot read config {config_file}: {e}", file=sys.stderr)
        return 3

    if os.environ.get("YIELD_SKIP_API_CHECK", "") != "1" and not check_bybit_api():
        print("[heartbeat] ABSTAIN: Bybit API unreachable")
        return 0

    cycle_ok, block_code = check_last_cycle_ok(log_dir)
    if not cycle_ok:
        print(f"[heartbeat] ABSTAIN: last cycle blocked by {block_code}")
        alert(f"⚠️ HEARTBEAT ABSTAIN: last cycle hard-failed ({block_code}). "
              f"Risk state NOT renewed.")
        return 0

    interval_min = cfg.get("CYCLE_INTERVAL_MINUTES", 10)
    if not isinstance(interval_min, (int, float)) or interval_min <= 0:
        interval_min = 10
    scanner_window_ms = max(MAX_AGE_MS, int(3 * interval_min * 60 * 1000))

    v = risk_state.verify(state_file, hmac_key)

    if v.code == risk_state.CODE_MISSING:
        # Fail-closed bootstrap: promoted to NORMAL only after a verified
        # clean cycle has seen this exact record.
        risk_state.write(state_file, hmac_key, "NO_NEW_POSITIONS",
                         "heartbeat bootstrap: no verified supervision cycle yet",
                         risk_state.SOURCE_BOOTSTRAP)
        print("[heartbeat] WROTE: NO_NEW_POSITIONS (bootstrap)")
        return 0

    if not v.signature_valid:
        print(f"[heartbeat] ABSTAIN: risk_state unverifiable ({v.code}: {v.detail})")
        alert(f"⚠️ HEARTBEAT ABSTAIN: risk_state unverifiable ({v.code}: {v.detail}). "
              f"NOT overwritten.")
        return 0

    age_min = max(0, (v.age_ms or 0) // 60000)

    if (v.state == "NO_NEW_POSITIONS" and v.source == risk_state.SOURCE_BOOTSTRAP
            and scanner_alive(log_dir, scanner_window_ms)
            and clean_cycle_verified(log_dir, v.ts)):
        risk_state.write(state_file, hmac_key, "NORMAL",
                         "heartbeat: bootstrap promoted after verified clean cycle",
                         risk_state.SOURCE_RENEW)
        print("[heartbeat] WROTE: NORMAL (bootstrap promoted after verified clean cycle)")
        return 0

    if v.state != "NORMAL":
        print(f"[heartbeat] ABSTAIN: {v.state} (source={v.source}, {v.code})")
        if not v.fresh:
            alert(f"⚠️ HEARTBEAT ALERT: risk state is {v.state} (source={v.source}) "
                  f"and STALE ({age_min} min old). Heartbeat did NOT renew.")
        return 0

    if v.fresh:
        print("[heartbeat] OK: state is NORMAL and fresh")
        return 0

    if not scanner_alive(log_dir, scanner_window_ms):
        print(f"[heartbeat] ABSTAIN: NORMAL but stale ({age_min} min) AND scanner "
              f"not alive (no recent decision). NOT renewing.")
        alert(f"⚠️ HEARTBEAT ABSTAIN: risk state is NORMAL but STALE ({age_min} min) "
              f"AND scanner has not produced a recent decision. Risk state NOT renewed.")
        return 0

    risk_state.write(state_file, hmac_key, "NORMAL",
                     f"heartbeat (was stale {age_min} min, scanner alive)",
                     risk_state.SOURCE_RENEW)
    print(f"[heartbeat] WROTE: NORMAL (was stale {age_min} min, scanner alive)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
