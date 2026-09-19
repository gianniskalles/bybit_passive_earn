#!/usr/bin/env python3
"""External heartbeat for risk_state.json — runs as a SEPARATE process/timer (A2).

Purpose: renew an EXPIRED risk_state so the yield cycle does not stall in
NO_NEW_POSITIONS (which is what happens when risk_state goes stale — the
wrapper treats stale/missing as NO_NEW_POSITIONS and, being the agent, the
cycle can never re-enter NORMAL on its own).

This is the v5.3-approved decision table (per handoff §Heartbeat):

  risk_state file absent                    -> write NORMAL
  NORMAL but stale                          -> write NORMAL
  present, unverifiable (bad HMAC/unread.)  -> ABSTAIN + alert, never overwrite
  non-NORMAL (UNWIND/NO_NEW_POSITIONS),
    even stale                              -> ABSTAIN + alert, never overwrite
  Bybit API unreachable                     -> ABSTAIN

check_last_cycle_ok() blocks ONLY on the 5 hard-fail codes:
  CONFIG_INCOMPLETE, CRITICAL, AGENT_PARSE_ERROR, DECISION_VALIDATION_FAILED,
  CYCLE_MODEL_MISMATCH.
It must NOT block on RISK_STATE_*, STALE_SCAN or CYCLE_LATENCY_HIGH — those
are non-fatal, and blocking on them creates a deadlock (the heartbeat exists
precisely to recover stale risk states / tolerate transient scan staleness).

LOG_DIR is NOT guessed: it is read from the SAME config file the wrapper
(run_yield_cycle.py) loads — config/yield_rotation.yaml.  If the configured
LOG_DIR directory does not exist, that is a hard error (exit 3 + alert); it is
NOT the same as "no cycle has run yet" (bootstrap).  The two are distinguished
explicitly:
  - LOG_DIR dir missing        -> error + alert (misconfig), do NOT write NORMAL
  - LOG_DIR exists, no *.jsonl -> bootstrap, write NORMAL

Environment (each falls back to /opt/hermes/.env and /opt/data/.env):
  HERMES_RISK_HMAC_KEY  (required to sign/verify)
  BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET
  ALERT_TELEGRAM_CHAT_ID, TELEGRAM_BOT_TOKEN

Test-only overrides (never set in production):
  YIELD_STATE_FILE       absolute path to a risk_state.json (default prod path)
  YIELD_CONFIG_FILE      absolute path to config/yield_rotation.yaml
  YIELD_LOG_DIR          override dir containing *.jsonl (test only)
  YIELD_SKIP_API_CHECK=1 skip the Bybit reachability probe
  YIELD_SKIP_CONFIG_DCHECK=1  skip the "LOG_DIR must exist" error (test only)
"""

import json
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime

import yaml

# Shared signing is the single source of truth. Add the repo dir so this
# script (which lives in the repo) imports the SAME module as risk_state.py.
_REPO_DIR = Path("/opt/hermes/yield_rotation")
if str(_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_DIR))
from signing import sign, verify  # noqa: E402  (single source of truth)

# ---- Paths ----
HERMES_DIR = Path("/opt/hermes")
YIELD_DIR = HERMES_DIR / "yield_rotation"
STATE_DIR = HERMES_DIR / "state"
DEFAULT_STATE_FILE = STATE_DIR / "risk_state.json"
DEFAULT_CONFIG_FILE = YIELD_DIR / "config" / "yield_rotation.yaml"

RISK_STATE_FILE = Path(os.environ.get("YIELD_STATE_FILE", DEFAULT_STATE_FILE))
CONFIG_FILE = Path(os.environ.get("YIELD_CONFIG_FILE", DEFAULT_CONFIG_FILE))
SKIP_API_CHECK = os.environ.get("YIELD_SKIP_API_CHECK", "") == "1"
# Test-only escape hatch (defaults: prod enforces LOG_DIR existence).
SKIP_CONFIG_DCHECK = os.environ.get("YIELD_SKIP_CONFIG_DCHECK", "") == "1"

# ---- Constants ----
MAX_AGE_MS = 30 * 60 * 1000  # 30 minutes — same as risk_state.py verify()

# The ONLY cycle-log codes that block the heartbeat. See module docstring.
BLOCKING_CODES = (
    "CONFIG_INCOMPLETE",
    "CRITICAL",
    "AGENT_PARSE_ERROR",
    "DECISION_VALIDATION_FAILED",
    "CYCLE_MODEL_MISMATCH",
)


def load_config(path: Path) -> dict:
    """Load the SAME config/yield_rotation.yaml the wrapper uses."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_log_dir(cfg: dict, env) -> Path:
    """Return the LOG_DIR: from config (wrapper source of truth), with a
    test-only env override. Raises on non-existent directory unless the test
    escape hatch or an override forces a path."""
    cfg_dir = cfg.get("LOG_DIR") or str(DEFAULT_LOG_DIR_FALLBACK())
    # Test-only: allow an override dir for the *_dir_missing_* tests.
    override = os.environ.get("YIELD_LOG_DIR", "")
    chosen = Path(override) if override else Path(cfg_dir)

    missing = not chosen.exists() or not chosen.is_dir()
    if missing and not SKIP_CONFIG_DCHECK:
        raise FileNotFoundError(
            f"LOG_DIR from config {CONFIG_FILE.name!r}: {chosen} does not exist. "
            f"This is a MISCONFIG — refusing to treat it as bootstrap."
        )
    return chosen


def DEFAULT_LOG_DIR_FALLBACK() -> Path:
    return Path("/opt/hermes/logs/yield_rotation")


def load_env(path: Path | str) -> dict:
    """Load KEY=VAL from .env file."""
    p = Path(path) if not isinstance(path, Path) else path
    try:
        content = p.read_text()
    except PermissionError:
        if os.geteuid() == 0:
            try:
                result = subprocess.run(
                    ["sudo", "-u", "hermes", "cat", str(p)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    content = result.stdout
                else:
                    return {}
            except Exception:
                return {}
        else:
            return {}
    env = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def write_risk_state(hmac_key: str, state: str, reason: str) -> dict:
    """Atomically write risk_state.json with signature. Returns the written dict."""
    ts = int(time.time() * 1000)
    obj = {
        "profile": "hermes-yield-rotation",
        "state": state,
        "ts": ts,
        "reason": reason,
    }
    obj["sig"] = sign(hmac_key, obj)
    tmp = RISK_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(RISK_STATE_FILE)
    return obj


def read_risk_state() -> dict | None:
    """Return parsed risk_state dict, or None if absent/unreadable."""
    if not RISK_STATE_FILE.exists():
        return None
    try:
        return json.loads(RISK_STATE_FILE.read_text())
    except Exception:
        return None


def check_bybit_api() -> bool:
    """Verify Bybit API is reachable (products + positions)."""
    try:
        sys.path.insert(0, str(YIELD_DIR))
        from bybit_earn_tool import BybitEarnTool
        tool = BybitEarnTool()
        products = tool.get_earn_products()
        return bool(products and isinstance(products, list))
    except Exception:
        return False


def _is_blocking_code(alert: str) -> str | None:
    """Return the blocking code if this alert is a hard-fail, else None."""
    a = (alert or "").strip()
    for code in BLOCKING_CODES:
        if a == code or a.startswith(code + ":"):
            return code
    return None


def check_last_cycle_ok(log_dir: Path) -> tuple[bool, str | None]:
    """
    Check the last yield cycle for a hard-fail code.

    Returns (ok: bool, blocking_code_or_None).
    - LOG_DIR directory exists but contains no *.jsonl -> bootstrap -> OK.
    - LOG_DIR *directory missing* is handled by resolve_log_dir() BEFORE this
      (raises -> misconfig error). We never get here with a missing dir.
    - Non-blocking alerts (RISK_STATE_*, STALE_SCAN, CYCLE_LATENCY_HIGH) never
      cause a block — deadlock guard.
    """
    logs = sorted(log_dir.glob("*.jsonl"))
    if not logs:
        return True, None  # no cycle has run yet -> bootstrap
    latest_log = logs[-1]
    try:
        lines = latest_log.read_text().strip().splitlines()
        if not lines:
            return True, None  # empty file -> nothing to block on
        last = json.loads(lines[-1])
    except Exception:
        # Unreadable last log. Do not assume success, but also not one of the
        # explicit hard-fail codes — treat as bootstrap-ish (no block).
        return True, None
    for alert in last.get("alerts", []):
        code = _is_blocking_code(alert)
        if code is not None:
            return False, code
    return True, None


def get_current_risk_state() -> str | None:
    """Read current risk state (from the file), or None if absent/unreadable."""
    rs = read_risk_state()
    if not rs:
        return None
    return rs.get("state")


def is_risk_state_fresh(rs: dict) -> bool:
    """Check if risk state timestamp is within MAX_AGE_MS."""
    if not rs or "ts" not in rs:
        return False
    age = int(time.time() * 1000) - rs["ts"]
    return age < MAX_AGE_MS


def send_telegram_alert(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a simple Telegram message via Bot API."""
    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    # Load .env files, but let the process environment win (test overrides).
    env1 = load_env(HERMES_DIR / ".env")
    env2 = load_env(Path("/opt/data/.env"))
    merged = {**env1, **env2}
    env = {k: v for k, v in merged.items() if k not in os.environ}
    env.update({k: v for k, v in os.environ.items() if k in merged or k in
                ("HERMES_RISK_HMAC_KEY", "YIELD_STATE_FILE", "YIELD_CONFIG_FILE",
                 "YIELD_LOG_DIR", "YIELD_SKIP_API_CHECK", "YIELD_SKIP_CONFIG_DCHECK")})

    hmac_key = env.get("HERMES_RISK_HMAC_KEY")
    if not hmac_key:
        print("[heartbeat] ERROR: HERMES_RISK_HMAC_KEY not set", file=sys.stderr)
        return 1

    # LOG_DIR comes from the SAME config as the wrapper. Missing dir = hard error.
    try:
        cfg = load_config(CONFIG_FILE)
        log_dir = resolve_log_dir(cfg, env)
    except FileNotFoundError as e:
        print(f"[heartbeat] ERROR: {e}", file=sys.stderr)
        bot_token = env.get("TELEGRAM_BOT_TOKEN")
        chat_id = env.get("ALERT_TELEGRAM_CHAT_ID")
        if bot_token and chat_id:
            send_telegram_alert(
                bot_token, chat_id,
                f"🚨 HEARTBEAT ERROR: {e}",
            )
        return 3  # distinct: misconfig, NOT bootstrap
    except Exception as e:
        print(f"[heartbeat] ERROR: cannot read config {CONFIG_FILE}: {e}", file=sys.stderr)
        return 3

    # Condition 1: Bybit API reachable (skippable for tests).
    if not SKIP_API_CHECK and not check_bybit_api():
        print("[heartbeat] ABSTAIN: Bybit API unreachable")
        return 0  # Not an error — just don't write

    # Condition 2: last cycle must not have a hard-fail code.
    cycle_ok, block_code = check_last_cycle_ok(log_dir)
    if not cycle_ok:
        print(f"[heartbeat] ABSTAIN: last cycle blocked by {block_code}")
        bot_token = env.get("TELEGRAM_BOT_TOKEN")
        chat_id = env.get("ALERT_TELEGRAM_CHAT_ID")
        if bot_token and chat_id:
            send_telegram_alert(
                bot_token, chat_id,
                f"⚠️ HEARTBEAT ABSTAIN: last cycle hard-failed ({block_code}). "
                f"Risk state NOT renewed."
            )
        return 0

    # Condition 3: risk state matrix.
    rs = read_risk_state()

    # File absent -> write NORMAL (bootstrap / clean slate).
    if rs is None:
        write_risk_state(hmac_key, "NORMAL", "heartbeat (was missing)")
        print("[heartbeat] WROTE: NORMAL (was missing)")
        return 0

    # Present but unverifiable -> ABSTAIN + alert, never overwrite.
    payload = {k: v for k, v in rs.items() if k != "sig"}
    if not verify(hmac_key, payload, rs.get("sig", "")):
        print("[heartbeat] ABSTAIN: present but unverifiable (bad HMAC/unreadable)")
        bot_token = env.get("TELEGRAM_BOT_TOKEN")
        chat_id = env.get("ALERT_TELEGRAM_CHAT_ID")
        if bot_token and chat_id:
            send_telegram_alert(
                bot_token, chat_id,
                "⚠️ HEARTBEAT ABSTAIN: risk_state present but unverifiable "
                "(bad HMAC/unreadable). NOT overwritten."
            )
        return 0

    current_state = rs.get("state")

    # Non-NORMAL (even stale) -> ABSTAIN + alert, never overwrite.
    if current_state and current_state != "NORMAL":
        reason_info = "STALE" if not is_risk_state_fresh(rs) else "fresh"
        print(f"[heartbeat] ABSTAIN: active non-NORMAL state "
              f"({current_state}, {reason_info})")
        if not is_risk_state_fresh(rs):
            age_min = max(0, (int(time.time() * 1000) - int(rs["ts"])) // 60000)
            bot_token = env.get("TELEGRAM_BOT_TOKEN")
            chat_id = env.get("ALERT_TELEGRAM_CHAT_ID")
            if bot_token and chat_id:
                send_telegram_alert(
                    bot_token, chat_id,
                    f"⚠️ HEARTBEAT ALERT: risk state is {current_state} "
                    f"and STALE ({age_min} min old). Heartbeat did NOT renew."
                )
        return 0

    # NORMAL and fresh -> nothing to do.
    if is_risk_state_fresh(rs):
        print("[heartbeat] OK: state is NORMAL and fresh")
        return 0

    # NORMAL but stale -> write fresh NORMAL.
    age_min = max(0, (int(time.time() * 1000) - int(rs["ts"])) // 60000)
    write_risk_state(hmac_key, "NORMAL", f"heartbeat (was stale {age_min} min)")
    print(f"[heartbeat] WROTE: NORMAL (was stale {age_min} min)")
    return 0


if __name__ == "__main__":
    sys.exit(main())