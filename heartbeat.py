#!/usr/bin/env python3
"""
External heartbeat for risk_state.json — runs as a SEPARATE process/timer (A2).

This is NOT part of the yield cycle. It is an external guard that renews the
risk state ONLY when ALL safety conditions hold:
  1. Bybit API reachable (earn products + positions)
  2. Last cycle did NOT end with CRITICAL (checked via decision log)
  3. No active non-NORMAL risk state (UNWIND / NO_NEW_POSITIONS / CRITICAL)
     — and NOT a stale non-NORMAL (older than 30 min is still non-NORMAL)

If ANY condition fails, heartbeat ABSTAINS (does not write). The yield cycle
will then naturally fall into NO_NEW_POSITIONS due to stale risk state — this
is the correct fail-safe behavior.

Environment:
  HERMES_RISK_HMAC_KEY  (from /opt/hermes/.env)
  BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET  (from /opt/hermes/.env)
  ALERT_TELEGRAM_CHAT_ID, TELEGRAM_BOT_TOKEN  (from /opt/data/.env)
"""

import os
import sys
import json
import time
import hmac
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

# ---- Paths ----
HERMES_DIR = Path("/opt/hermes")
YIELD_DIR = HERMES_DIR / "yield_rotation"
STATE_DIR = HERMES_DIR / "state"
RISK_STATE_FILE = STATE_DIR / "risk_state.json"
LOG_DIR = YIELD_DIR / "logs"

# ---- Constants ----
MAX_AGE_MS = 30 * 60 * 1000  # 30 minutes — same as risk_state.py verify()
MAX_CYCLE_AGE_S = 15 * 60    # last cycle must be < 15 min old


def load_env(path: Path | str) -> dict:
    """Load KEY=VAL from .env file."""
    p = Path(path) if not isinstance(path, Path) else path
    try:
        content = p.read_text()
    except PermissionError:
        if os.geteuid() == 0:
            try:
                import subprocess
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


def sign_state(hmac_key: str, state: str, ts: int, reason: str) -> str:
    """Compute HMAC-SHA256 over canonical JSON (same as risk_state.py)."""
    canonical = json.dumps(
        {"profile": "hermes-yield-rotation", "state": state, "ts": ts, "reason": reason},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hmac.new(
        hmac_key.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()


def write_risk_state(hmac_key: str, state: str, reason: str) -> bool:
    """Atomically write risk_state.json with signature."""
    ts = int(time.time() * 1000)
    sig = sign_state(hmac_key, state, ts, reason)
    payload = {
        "profile": "hermes-yield-rotation",
        "state": state,
        "ts": ts,
        "reason": reason,
        "sig": sig,
    }
    tmp = RISK_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    tmp.replace(RISK_STATE_FILE)
    return True


def read_risk_state() -> dict | None:
    if not RISK_STATE_FILE.exists():
        return None
    try:
        return json.loads(RISK_STATE_FILE.read_text())
    except Exception:
        return None


def check_bybit_api(env: dict) -> bool:
    """Verify Bybit API is reachable (products + positions)."""
    try:
        # Use the existing tool module - it reads env directly
        import os
        os.environ["BYBIT_API_KEY"] = env.get("BYBIT_API_KEY", "")
        os.environ["BYBIT_API_SECRET"] = env.get("BYBIT_API_SECRET", "")
        sys.path.insert(0, str(YIELD_DIR))
        from bybit_earn_tool import BybitEarnTool
        tool = BybitEarnTool()
        products = tool.get_earn_products()
        positions = tool.get_earn_positions()
        return bool(products and isinstance(products, list))
    except Exception:
        return False


def check_last_cycle_ok() -> bool:
    """
    Check if the last yield cycle completed without CRITICAL.
    Reads the latest JSONL log file.
    """
    if not LOG_DIR.exists():
        return False
    logs = sorted(LOG_DIR.glob("*.jsonl"))
    if not logs:
        return False
    latest_log = logs[-1]
    try:
        lines = latest_log.read_text().strip().splitlines()
        if not lines:
            return False
        last = json.loads(lines[-1])
        # Cycle age
        ts = last.get("ts", 0)  # ISO or ms epoch — see write_cycle_log convention
        if isinstance(ts, (int, float)):
            # ms epoch
            if int(ts) < 10**12:  # seconds
                ts *= 1000
        else:
            # ISO string → parse
            from datetime import datetime
            try:
                ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                return False
        if time.time() * 1000 - ts > MAX_CYCLE_AGE_S * 1000:
            return False
        # No CRITICAL in alerts
        alerts = last.get("alerts", [])
        if "CRITICAL" in alerts:
            return False
        return True
    except Exception:
        return False


def get_current_risk_state() -> str | None:
    """Read current risk state (NORMAL, UNWIND, NO_NEW_POSITIONS, CRITICAL)."""
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
    # Load env from both .env files
    env1 = load_env(HERMES_DIR / ".env")
    env2 = load_env(Path("/opt/data/.env"))
    env = {**env1, **env2}

    hmac_key = env.get("HERMES_RISK_HMAC_KEY")
    if not hmac_key:
        print("[heartbeat] ERROR: HERMES_RISK_HMAC_KEY not set", file=sys.stderr)
        return 1

    # Condition 1: Bybit API reachable
    if not check_bybit_api(env):
        print("[heartbeat] ABSTAIN: Bybit API unreachable")
        return 0  # Not an error — just don't write

    # Condition 2: Last cycle OK (not CRITICAL, recent)
    if not check_last_cycle_ok():
        print("[heartbeat] ABSTAIN: Last cycle missing/old/CRITICAL")
        return 0

    # Condition 3: Current risk state
    current_state = get_current_risk_state()
    rs = read_risk_state()

    # If current state is NORMAL and fresh — nothing to do
    if current_state == "NORMAL" and is_risk_state_fresh(rs):
        print("[heartbeat] OK: state is NORMAL and fresh")
        return 0

    # If current state is non-NORMAL (UNWIND, NO_NEW_POSITIONS, CRITICAL) —
    # NEVER overwrite, even if stale. A stale UNWIND is still UNWIND.
    if current_state and current_state != "NORMAL":
        print(f"[heartbeat] ABSTAIN: active non-NORMAL state ({current_state})")
        # Alert on stale non-NORMAL so operator knows
        if not is_risk_state_fresh(rs):
            age_min = (int(time.time() * 1000) - rs["ts"]) // 60000
            bot_token = env.get("TELEGRAM_BOT_TOKEN")
            chat_id = env.get("ALERT_TELEGRAM_CHAT_ID")
            if bot_token and chat_id:
                send_telegram_alert(
                    bot_token,
                    chat_id,
                    f"⚠️ HEARTBEAT ALERT: risk state is {current_state} "
                    f"and STALE ({age_min} min old). Heartbeat did NOT renew."
                )
        return 0

    # If state is missing or NORMAL but stale → write fresh NORMAL
    reason = "heartbeat"
    if not rs:
        reason = "heartbeat (was missing)"
    elif not is_risk_state_fresh(rs):
        age_min = (int(time.time() * 1000) - rs["ts"]) // 60000
        reason = f"heartbeat (was stale {age_min} min)"

    write_risk_state(hmac_key, "NORMAL", reason)
    print(f"[heartbeat] WROTE: NORMAL ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())