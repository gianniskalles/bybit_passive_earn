#!/usr/bin/env python3
"""preflight.py — the checks behind deploy/deploy.sh (one subcommand each).

Every subcommand prints what it saw and exits 0 (PASS) or 1 (FAIL).
Secrets are never printed: only sha256 fingerprints (first 12 hex chars).

  dry-run-on              the config the cycle reads says DRY_RUN: true
                          (boolean); anything else FAILS
  keys                    HMAC key present and not the smoke-test key (else a
                          new one is generated), Bybit keys in /opt/hermes/.env
                          (copied from /opt/data/.env if only there), Telegram
                          token present, BYBIT_TESTNET not set, .env mode 600
  telegram-test           send one test message
  reset-state-if-invalid  move an unverifiable risk_state.json aside
                          (a valid one — any state, any source — is kept)
  check-cycle             the newest decision record is clean
  expect-normal           risk_state verifies and is NORMAL
  regression-report       print the report path for this prompt + model
  regression-ok PATH      that report exists and every run passed
  has-bot-token           YIELD_TELEGRAM_BOT_TOKEN is configured
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import risk_state  # noqa: E402
import settings  # noqa: E402
from notify import Notifier, send_telegram  # noqa: E402

SMOKE_MARKER = "smoke-test"
MIN_HMAC_LEN = 32


def fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _cfg() -> dict:
    return yaml.safe_load(settings.config_file().read_text()) or {}


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes")


def _rewrite_env(path: Path, updates: Dict[str, str]) -> None:
    """Replace/append KEY=VALUE lines, keep everything else; mode 600."""
    lines = path.read_text().splitlines() if path.exists() else []
    keep = [l for l in lines if l.split("=", 1)[0].strip() not in updates]
    keep += [f"{k}={v}" for k, v in updates.items()]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keep) + "\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #

def cmd_keys(args: List[str]) -> int:
    prof_path, shared_path = settings.env_file(), settings.shared_env_file()
    prof = settings.load_env_file(prof_path)
    shared = settings.load_env_file(shared_path)
    updates: Dict[str, str] = {}
    errors: List[str] = []
    rows = []

    hmac_key = prof.get("HERMES_RISK_HMAC_KEY", "")
    if not hmac_key or SMOKE_MARKER in hmac_key or len(hmac_key) < MIN_HMAC_LEN:
        why = "missing" if not hmac_key else ("smoke-test key" if SMOKE_MARKER in hmac_key
                                              else "too short")
        updates["HERMES_RISK_HMAC_KEY"] = secrets.token_hex(32)
        rows.append(("HERMES_RISK_HMAC_KEY", f"GENERATED (was {why})",
                     fingerprint(updates["HERMES_RISK_HMAC_KEY"])))
    else:
        rows.append(("HERMES_RISK_HMAC_KEY", "OK", fingerprint(hmac_key)))

    for k in ("BYBIT_API_KEY", "BYBIT_API_SECRET"):
        if prof.get(k):
            rows.append((k, "OK", fingerprint(prof[k])))
        elif shared.get(k):
            updates[k] = shared[k]
            rows.append((k, f"COPIED from {shared_path}", fingerprint(shared[k])))
        else:
            rows.append((k, "MISSING", "-"))
            errors.append(f"{k} is in neither {prof_path} nor {shared_path}")

    tg = prof.get("TELEGRAM_BOT_TOKEN") or shared.get("TELEGRAM_BOT_TOKEN")
    rows.append(("TELEGRAM_BOT_TOKEN", "OK" if tg else "MISSING", fingerprint(tg) if tg else "-"))
    if not tg:
        errors.append("TELEGRAM_BOT_TOKEN missing")
    bot = prof.get("YIELD_TELEGRAM_BOT_TOKEN")
    rows.append(("YIELD_TELEGRAM_BOT_TOKEN", "OK" if bot else "not set (commands bot off)",
                 fingerprint(bot) if bot else "-"))

    if _truthy(prof.get("BYBIT_TESTNET")) or _truthy(os.environ.get("BYBIT_TESTNET")):
        errors.append("BYBIT_TESTNET is set — deploy never runs against testnet; unset it")
    rows.append(("BYBIT_TESTNET", prof.get("BYBIT_TESTNET") or "(empty = mainnet)", "-"))

    if updates and not errors:
        if prof_path.exists():
            backup = prof_path.with_name(f"{prof_path.name}.bak.{_stamp()}")
            shutil.copy2(prof_path, backup)
            os.chmod(backup, 0o600)
            print(f"backup: {backup}")
        _rewrite_env(prof_path, updates)
    if prof_path.exists():
        os.chmod(prof_path, 0o600)

    if updates and errors:
        # Nothing is written while anything else is wrong; say so per row.
        rows = [(k, status.replace("GENERATED", "WOULD GENERATE").replace("COPIED", "WOULD COPY"),
                 fp) for k, status, fp in rows]
        print(f"NOT WRITTEN: {', '.join(updates)} — fix the errors below and re-run")
    for k, status, fp in rows:
        print(f"{k:26} {status:40} {fp}")
    for e in errors:
        print(f"ERROR: {e}")
    if prof_path.exists():
        print(f"{prof_path}: mode {oct(prof_path.stat().st_mode & 0o777)}")
    return 1 if errors else 0


def cmd_dry_run_on(args: List[str]) -> int:
    path = settings.config_file()
    try:
        cfg = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        print(f"{path}: unreadable: {e}")
        return 1
    value = cfg.get("DRY_RUN") if isinstance(cfg, dict) else None
    print(f"{path}: DRY_RUN = {value!r}")
    if value is True:
        return 0
    print("ERROR: DRY_RUN must be the boolean true. deploy.sh never runs with anything "
          "else; only Giannis switches to live, outside deploy.")
    return 1


def cmd_telegram_test(args: List[str]) -> int:
    n = Notifier(settings.load_env(), _cfg(), sender=lambda t, c, x: send_telegram(t, c, x))
    print(f"enabled: {n.enabled}  chat: {n.chat}")
    ok = n.enabled and n.event(f"🧪 yield rotation deploy test {_stamp()}")
    print(f"sent: {bool(ok)}")
    return 0 if ok else 1


def cmd_reset_state_if_invalid(args: List[str]) -> int:
    path = settings.risk_state_file()
    v = risk_state.verify(path, settings.load_env().get("HERMES_RISK_HMAC_KEY", ""))
    if v.signature_valid:
        print(f"keep: {v.state} (source={v.source}, {v.code})")
        return 0
    if v.code == risk_state.CODE_MISSING:
        print(f"{path} absent — the heartbeat will bootstrap it")
        return 0
    if v.code == risk_state.CODE_NO_KEY:
        print("ERROR: HERMES_RISK_HMAC_KEY not set")
        return 1
    aside = path.with_name(f"{path.name}.pre-v6.{_stamp()}")
    os.replace(path, aside)
    print(f"moved unverifiable risk_state ({v.code}: {v.detail}) to {aside}")
    return 0


def _last_record() -> Optional[dict]:
    log_dir = Path(_cfg().get("LOG_DIR") or settings.default_log_dir())
    logs = sorted(log_dir.glob("*.jsonl"))
    if not logs:
        return None
    lines = [l for l in logs[-1].read_text().splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


def cmd_check_cycle(args: List[str]) -> int:
    from heartbeat import _is_blocking_code

    rec = _last_record()
    if rec is None:
        print("ERROR: no decision record found")
        return 1
    meta = rec.get("risk_state_meta") or {}
    summary = {k: rec.get(k) for k in ("cycle_id", "risk_state", "agent_called", "prompt_file",
                                       "dry_run", "alerts", "data_errors")}
    summary["risk_state_code"] = meta.get("code")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    problems = [f"blocking code: {a}" for a in rec.get("alerts", []) if _is_blocking_code(a)]
    if rec.get("dry_run") is not True:
        problems.append("dry_run is not true")
    if rec.get("data_errors"):
        problems.append(f"Bybit data unavailable: {sorted(rec['data_errors'])}")
    if meta.get("signature_valid") is not True:
        problems.append(f"risk_state not verified by the cycle ({meta.get('code')})")
    if any(str(a).startswith("NO_ELIGIBLE_PRODUCTS") for a in rec.get("alerts", [])):
        problems.append("no product survived the filters (likely a Bybit field mismatch); "
                        "filtered_by_wrapper:\n" + json.dumps(rec.get("filtered_by_wrapper"), indent=1))
    for p in problems:
        print(f"ERROR: {p}")
    return 1 if problems else 0


def cmd_expect_normal(args: List[str]) -> int:
    v = risk_state.verify(settings.risk_state_file(),
                          settings.load_env().get("HERMES_RISK_HMAC_KEY", ""))
    print(f"risk_state: {v.state} (code={v.code}, source={v.source}, "
          f"age={None if v.age_ms is None else v.age_ms // 1000}s)")
    if v.code == risk_state.CODE_OK and v.state == "NORMAL":
        return 0
    if v.source == risk_state.SOURCE_OPERATOR:
        print("ERROR: an operator state is in force; deploy does not override it")
    else:
        print("ERROR: expected NORMAL with code OK")
    return 1


def _report_path() -> Path:
    cfg = _cfg()
    prompt = ROOT / f"prompt_{cfg['PROMPT_VERSION']}.md"
    sha = hashlib.sha256(prompt.read_bytes()).hexdigest()[:12]
    model = re.sub(r"[^A-Za-z0-9_.-]", "_", str(cfg["RESOLVED_MODEL"]))
    return settings.hermes_home() / "logs" / "regression" / \
        f"regression_{cfg['PROMPT_VERSION']}_{sha}_{model}.json"


def cmd_regression_report(args: List[str]) -> int:
    print(_report_path())
    return 0


def cmd_regression_ok(args: List[str]) -> int:
    if len(args) != 1:
        print("usage: regression-ok PATH")
        return 1
    path = Path(args[0])
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        print(f"no regression report at {path}")
        return 1
    cfg = _cfg()
    ok = (report.get("total", 0) > 0 and report.get("passed") == report.get("total")
          and report.get("model") == cfg["RESOLVED_MODEL"]
          and report.get("prompt_version") == cfg["PROMPT_VERSION"])
    print(f"{path}: {report.get('passed')}/{report.get('total')} passed "
          f"(model {report.get('model')}, prompt {report.get('prompt_version')})")
    return 0 if ok else 1


def cmd_has_bot_token(args: List[str]) -> int:
    return 0 if settings.load_env().get("YIELD_TELEGRAM_BOT_TOKEN") else 1


COMMANDS = {
    "dry-run-on": cmd_dry_run_on,
    "keys": cmd_keys,
    "telegram-test": cmd_telegram_test,
    "reset-state-if-invalid": cmd_reset_state_if_invalid,
    "check-cycle": cmd_check_cycle,
    "expect-normal": cmd_expect_normal,
    "regression-report": cmd_regression_report,
    "regression-ok": cmd_regression_ok,
    "has-bot-token": cmd_has_bot_token,
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
