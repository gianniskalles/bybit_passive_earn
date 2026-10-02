#!/usr/bin/env python3
"""settings.py — the single place that knows where things live.

Every filesystem path the project uses is resolved here, from an env var
with a VPS default.  Nothing in this module has a side effect at import
time: paths are computed on call, nothing is created, nothing is read, and
os.environ is never written.

Path overrides (all optional; defaults match the VPS layout):
  YIELD_HERMES_HOME      /opt/hermes
  YIELD_CONFIG_FILE      <repo>/config/yield_rotation.yaml
  YIELD_STATE_FILE       $YIELD_HERMES_HOME/state/risk_state.json
  YIELD_SESSION_DIR      $YIELD_HERMES_HOME/state/yield_rotation_sessions
  YIELD_HERMES_BIN       $YIELD_HERMES_HOME/.venv/bin/hermes
  YIELD_ENV_FILE         $YIELD_HERMES_HOME/.env      (profile secrets)
  YIELD_SHARED_ENV_FILE  /opt/data/.env               (shared; TELEGRAM_BOT_TOKEN only)
  YIELD_NOTIFY_STATE     $YIELD_HERMES_HOME/state/notify_state.json (Telegram dedup)
  YIELD_CARRY_CONFIG_FILE  <repo>/config/carry.yaml   (testnet: config/carry.testnet.yaml)
  YIELD_CARRY_STATE_FILE   <carry state>/carry_risk_state.json
  YIELD_CARRY_ADOPT_FILE   <carry state>/carry_adopt_request.json
  YIELD_CARRY_BOOK_FILE    <carry state>/carry_book.json
  YIELD_CARRY_PAPER_FILE   <carry state>/carry_paper.json   (DRY_RUN account)
  YIELD_CARRY_HOLD_FILE    <carry state>/carry_hold.json    (NO_NEW_POSITIONS latch)
  YIELD_CARRY_CYCLE_FILE   <carry state>/carry_cycle.json   (last cycle time, and the lock)
  <carry state> = $YIELD_HERMES_HOME/state on mainnet, and
                  $YIELD_HERMES_HOME/state/carry-testnet when BYBIT_TESTNET is set
                  (the same switch as the Bybit client's): a testnet run never
                  reads or writes the mainnet carry book, risk state, hold,
                  paper account or adopt request (decision of 2/10).

Env precedence (load_env):
  process env  >  profile .env  >  shared .env (TELEGRAM_BOT_TOKEN only)
The shared file is used by other services on the VPS; it must never be able
to supply or replace Bybit credentials or the risk-state HMAC key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Mapping, Optional

ROOT = Path(__file__).resolve().parent

# The only keys the shared /opt/data/.env may contribute.
SHARED_ENV_KEYS = ("TELEGRAM_BOT_TOKEN",)


def _path(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else default


def hermes_home() -> Path:
    return _path("YIELD_HERMES_HOME", Path("/opt/hermes"))


def config_file() -> Path:
    return _path("YIELD_CONFIG_FILE", ROOT / "config" / "yield_rotation.yaml")


def risk_state_file() -> Path:
    return _path("YIELD_STATE_FILE", hermes_home() / "state" / "risk_state.json")


def default_log_dir() -> Path:
    """Fallback only; the config's LOG_DIR is the source of truth."""
    return hermes_home() / "logs" / "yield_rotation"


def session_dir() -> Path:
    return _path("YIELD_SESSION_DIR", hermes_home() / "state" / "yield_rotation_sessions")


def hermes_bin() -> Path:
    return _path("YIELD_HERMES_BIN", hermes_home() / ".venv" / "bin" / "hermes")


def env_file() -> Path:
    return _path("YIELD_ENV_FILE", hermes_home() / ".env")


def shared_env_file() -> Path:
    return _path("YIELD_SHARED_ENV_FILE", Path("/opt/data/.env"))


def testnet() -> bool:
    """BYBIT_TESTNET as the Bybit client reads it (process env > profile .env)."""
    return str(load_env().get("BYBIT_TESTNET") or "").strip().lower() in ("1", "true", "yes")


def carry_config_file() -> Path:
    name = "carry.testnet.yaml" if testnet() else "carry.yaml"
    return _path("YIELD_CARRY_CONFIG_FILE", ROOT / "config" / name)


def carry_state_dir() -> Path:
    base = hermes_home() / "state"
    return base / "carry-testnet" if testnet() else base


def carry_risk_state_file() -> Path:
    return _path("YIELD_CARRY_STATE_FILE", carry_state_dir() / "carry_risk_state.json")


def carry_adopt_file() -> Path:
    return _path("YIELD_CARRY_ADOPT_FILE", carry_state_dir() / "carry_adopt_request.json")


def carry_book_file() -> Path:
    return _path("YIELD_CARRY_BOOK_FILE", carry_state_dir() / "carry_book.json")


def carry_paper_file() -> Path:
    return _path("YIELD_CARRY_PAPER_FILE", carry_state_dir() / "carry_paper.json")


def carry_hold_file() -> Path:
    return _path("YIELD_CARRY_HOLD_FILE", carry_state_dir() / "carry_hold.json")


def carry_cycle_file() -> Path:
    return _path("YIELD_CARRY_CYCLE_FILE", carry_state_dir() / "carry_cycle.json")


def notify_state_file() -> Path:
    return _path("YIELD_NOTIFY_STATE", hermes_home() / "state" / "notify_state.json")


def load_env_file(path: Path) -> Dict[str, str]:
    """Parse KEY=VALUE lines.  A missing or unreadable file yields {}."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    env: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env


def load_env(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return the merged environment.  Never writes os.environ."""
    process = dict(os.environ if environ is None else environ)
    shared = load_env_file(shared_env_file())
    merged = {k: shared[k] for k in SHARED_ENV_KEYS if k in shared}
    merged.update(load_env_file(env_file()))
    merged.update(process)
    return merged
