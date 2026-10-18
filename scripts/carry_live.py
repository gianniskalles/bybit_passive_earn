#!/usr/bin/env python3
"""carry_live.py — Giannis's live switch for the carry (18/10). Nobody else.

config/carry.yaml stays DRY_RUN: true for ever, and deploy.sh refuses
anything else. Live is the signed switch <state>/carry_live.json
(carry/live.py), and this is the only thing that writes it:

  sudo -u hermes env HOME=/opt/hermes $PY scripts/carry_live.py status
  sudo -u hermes env HOME=/opt/hermes $PY scripts/carry_live.py enable --capital-cap 100
  sudo -u hermes env HOME=/opt/hermes $PY scripts/carry_live.py disable

enable refuses unless ALL of these hold:
  - mainnet; config/carry.yaml valid and DRY_RUN: true; no switch yet;
  - in /opt/hermes/.env: HERMES_RISK_HMAC_KEY, the carry's own key and
    secret, an https DEADMAN_URL, Telegram (token and chat);
  - the live config (DRY_RUN false, this DEADMAN_URL, the capital cap given,
    which is never above TOTAL_CAPITAL_CAP_USD) passes the full validation;
  - the supervised round trip is not running;
  - the exchange reads: Cross Margin, the base coins active collateral,
    positions and Earn readable; the carry is FLAT (exchange and live book);
  - the carry risk state is NORMAL and verified, with no CARRY_HOLD;
  - a terminal (not a pipe or an agent), then a one-time code sent to
    Giannis's Telegram typed back within 10 minutes, then the exact phrase
    "LIVE <cap> USDT".
The next carry cycle runs live. The cap is signed into the switch: a later
change of TOTAL_CAPITAL_CAP_USD in the repo never raises it.

disable removes the switch only when the carry is FLAT (exchange and live
book): to stop with a position open, Telegram /unwind carry first. A lost
or broken switch with the live book open keeps the carry live, UNWIND only
(carry/live.py), until FLAT.

Exit codes: 0 done, 1 refused, 2 usage.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import risk_state  # noqa: E402
import settings  # noqa: E402
from carry import config as cc  # noqa: E402
from carry import exposure as cx  # noqa: E402
from carry import live  # noqa: E402
from carry import risk  # noqa: E402
from carry import snapshot as snapshot_mod  # noqa: E402

CODE_TTL_S = 600


def _fp(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


def roundtrip_running(run=subprocess.run) -> bool:
    try:
        out = run(["systemctl", "is-active", "yield-carry-roundtrip.service"],
                  capture_output=True, text=True).stdout
    except OSError:
        return False
    return out.strip() == "active"


class LiveCli:
    def __init__(self, cfg: Dict, env: Dict[str, str], client, notifier, *,
                 ask: Callable[[str], str] = input, isatty: Callable[[], bool] = sys.stdin.isatty,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 clock: Callable[[], float] = time.monotonic,
                 snapshot_fn: Callable = snapshot_mod.take,
                 roundtrip_active: Callable[[], bool] = roundtrip_running,
                 out: Callable[[str], None] = print):
        self.cfg, self.env, self.client, self.notifier = cfg, env, client, notifier
        self.ask, self.isatty, self.now_ms, self.clock = ask, isatty, now_ms, clock
        self.snapshot_fn, self.roundtrip_active, self.out = snapshot_fn, roundtrip_active, out
        self.key = env.get("HERMES_RISK_HMAC_KEY", "")

    # ---- shared --------------------------------------------------------------------
    def exposure(self) -> cx.Exposure:
        """The exchange and the LIVE book (the paper account is not money)."""
        if self.client is None:
            return cx.Exposure(cx.UNKNOWN, ["the carry API key is not set"])
        return cx.check(self.client, dict(self.cfg, DRY_RUN=False), settings.carry_book_file(),
                        self.key, None, now_ms=self.now_ms(), take=self.snapshot_fn)

    def live_cfg(self, cap: float) -> Dict:
        return dict(self.cfg, DRY_RUN=False, DEADMAN_URL=self.env.get("DEADMAN_URL"),
                    TOTAL_CAPITAL_CAP_USD=cap)

    # ---- status --------------------------------------------------------------------
    def status(self) -> int:
        path = settings.carry_live_file()
        body, broken = live.read(path, self.key)
        if body:
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(body["enabled_ms"] / 1000))
            self.out(f"LIVE: {path} valid — enabled {when} by {body['by']}, capital cap "
                     f"{body['capital_cap']:g} USDT, symbols {body['symbols']}")
        elif broken:
            self.out(f"LIVE SWITCH INVALID: {broken} (the carry does not trust it)")
        else:
            self.out(f"paper (DRY_RUN): no live switch at {path}")
        e = self.exposure()
        self.out(f"carry exposure: {e.verdict}")
        for line in e.lines:
            self.out(f"  {line}")
        return 0

    # ---- enable --------------------------------------------------------------------
    def problems(self, cap: float) -> List[str]:
        p: List[str] = []
        if settings.testnet():
            p.append("BYBIT_TESTNET is set: live is mainnet only")
        if self.cfg.get("DRY_RUN") is not True:
            p.append(f"{settings.carry_config_file()}: DRY_RUN must stay true in the repo; live "
                     f"is the switch, never the config")
        if settings.carry_live_file().exists():
            p.append(f"{settings.carry_live_file()} exists already (status shows it)")
        if not self.key:
            p.append("HERMES_RISK_HMAC_KEY not set in the .env")
        key_name, secret_name = settings.carry_api_key_names(testnet_=False)
        for k in (key_name, secret_name):
            if not self.env.get(k):
                p.append(f"{k} not set in the .env (the carry subaccount's own key)")
        dm = self.env.get("DEADMAN_URL") or ""
        if not dm.startswith("https://"):
            p.append("DEADMAN_URL (https://...) not set in /opt/hermes/.env")
        if self.cfg.get("DEADMAN_URL") is not None:
            p.append(f"{settings.carry_config_file()}: DEADMAN_URL must stay null in the repo")
        if self.notifier is None or not getattr(self.notifier, "enabled", False):
            p.append("Telegram not configured (TELEGRAM_BOT_TOKEN and the operator chat): the "
                     "one-time code cannot be sent")
        top = self.cfg.get("TOTAL_CAPITAL_CAP_USD")
        if not (isinstance(cap, (int, float)) and 0 < cap <= (top or 0)):
            p.append(f"--capital-cap {cap!r} must be > 0 and <= TOTAL_CAPITAL_CAP_USD {top}")
        else:
            p.extend(f"live config: {e}" for e in cc.validate(
                self.live_cfg(cap), testnet=False,
                alt_report=cc.read_alt_report(cc.ALT_REPORT_FILE)))
        if self.roundtrip_active():
            p.append("the supervised round trip (yield-carry-roundtrip) is running")
        v = risk_state.verify(settings.carry_risk_state_file(), self.key, now_ms=self.now_ms(),
                              profile=risk_state.CARRY_PROFILE)
        if not (v.ok and v.state == "NORMAL"):
            p.append(f"carry risk state is {v.state} ({v.code}); NORMAL is needed")
        hold = risk.read_hold(settings.carry_hold_file())
        if hold:
            p.append(f"CARRY_HOLD: {hold['reason']} — release it first (/resume carry)")
        if self.client is None:
            p.append("the carry API key is not set: the exchange cannot be read")
            return p
        try:
            snap = self.snapshot_fn(self.client, dict(self.cfg, DRY_RUN=False),
                                    now_ms=self.now_ms(), private=True)
            for section in ["account", "earn"] + [f"positions:{s}" for s in snap.symbols]:
                if section in snap.errors:
                    p.append(f"{section} unreadable: {snap.errors[section]}")
            acct = snap.account
            if acct is not None:
                if not acct.cross_margin:
                    p.append(f"margin mode {acct.margin_mode}: Cross Margin is needed")
                for sym in snap.symbols:
                    base = sym[:-len("USDT")]
                    c = acct.collateral.get(base)
                    if c is None or not c.active:
                        p.append(f"{base} is not active collateral (Bybit UI: Collateral -> On)")
        except Exception as e:                                      # noqa: BLE001
            p.append(f"exchange unreadable: {type(e).__name__}: {e}")
        e = self.exposure()
        if e.verdict != cx.FLAT:
            p.append(f"the carry is not FLAT ({e.verdict}): " + "; ".join(e.lines))
        return p

    def enable(self, cap: float) -> int:
        problems = self.problems(cap)
        self.out(json.dumps({"capital_cap": cap, "symbols": self.cfg.get("SYMBOLS"),
                             "deadman_url": _fp(self.env["DEADMAN_URL"])
                             if self.env.get("DEADMAN_URL") else None,
                             "problems": problems}, indent=2))
        if problems:
            self.out("refusing:\n  " + "\n  ".join(problems))
            return 1
        if not self.isatty():
            self.out("refusing: run it yourself on a terminal (stdin is not a TTY)")
            return 1
        code = f"{secrets.randbelow(10 ** 6):06d}"
        if not self.notifier.event(f"🔐 CARRY LIVE: code {code} (valid {CODE_TTL_S // 60} min). "
                                   f"If you did not ask to switch the carry to real money, "
                                   f"ignore it and tell nobody the code."):
            self.out("refusing: the code could not be sent to Telegram")
            return 1
        started = self.clock()
        typed = self.ask("One-time code from Telegram: ").strip()
        if self.clock() - started > CODE_TTL_S or not secrets.compare_digest(typed, code):
            self.out("refusing: wrong or expired code; nothing changed")
            self.notifier.event("⚠️ CARRY LIVE: a wrong or expired code was typed; nothing "
                                "changed")
            return 1
        phrase = f"LIVE {cap:g} USDT"
        if self.ask(f'Type exactly "{phrase}" to run the carry with real money: ').strip() \
                != phrase:
            self.out("refusing: the phrase did not match; nothing changed")
            return 1
        if self.problems(cap):                                      # nothing moved meanwhile
            self.out("refusing: the checks no longer pass; run enable again")
            return 1
        by = os.environ.get("SUDO_USER") or getpass.getuser()
        body = live.write(settings.carry_live_file(), self.key, cap,
                          list(self.cfg.get("SYMBOLS") or []), by, self.now_ms())
        self.out(f"LIVE: {settings.carry_live_file()} written; the next carry cycle trades real "
                 f"money (capital cap {cap:g} USDT)")
        self.notifier.event(f"🟢 CARRY LIVE enabled by {by}: capital cap {cap:g} USDT, symbols "
                            f"{body['symbols']}. Next cycle trades real money. Stop: /unwind carry, "
                            f"then carry_live.py disable.")
        return 0

    # ---- disable -------------------------------------------------------------------
    def disable(self) -> int:
        path = settings.carry_live_file()
        if not path.exists():
            self.out(f"paper already: no live switch at {path}")
            return 0
        e = self.exposure()
        if e.verdict != cx.FLAT:
            self.out(f"refusing: the carry is not FLAT ({e.verdict}); a live position needs the "
                     f"live cycle. Telegram /unwind carry, wait for FLAT, then disable again.")
            for line in e.lines:
                self.out(f"  {line}")
            return 1
        path.unlink()
        self.out(f"paper: {path} removed; the next carry cycle runs DRY_RUN")
        if self.notifier is not None:
            self.notifier.event("⚪ CARRY LIVE disabled: the carry is FLAT and back to paper "
                                "(DRY_RUN).")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the carry's live switch (Giannis only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    en = sub.add_parser("enable")
    en.add_argument("--capital-cap", type=float, required=True,
                    help="USDT; signed into the switch, <= TOTAL_CAPITAL_CAP_USD")
    sub.add_parser("disable")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    from carry.client import CarryClient
    from notify import Notifier
    env = settings.load_env()
    try:
        cfg = cc.load(settings.carry_config_file(), testnet=settings.testnet())
    except cc.CarryConfigError as e:
        print(f"refusing: {e}")
        return 1
    key_name, secret_name = settings.carry_api_key_names(testnet_=False)
    client = CarryClient(api_key=env[key_name], api_secret=env[secret_name], testnet=False) \
        if env.get(key_name) and env.get(secret_name) else None
    cli = LiveCli(cfg, env, client, Notifier(env, cfg))
    if args.cmd == "status":
        return cli.status()
    if args.cmd == "enable":
        return cli.enable(args.capital_cap)
    return cli.disable()


if __name__ == "__main__":
    sys.exit(main())
