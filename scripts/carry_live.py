#!/usr/bin/env python3
"""carry_live.py — the carry's live switch from a terminal (Giannis only).

The same switch, the same checks and the same signed file as Telegram
`/live enable <cap>` / `/live disable` (carry/live_ops.py, carry/live.py);
this is the way in for whoever has a shell on the server:

  sudo -u hermes env HOME=/opt/hermes $PY scripts/carry_live.py status
  sudo -u hermes env HOME=/opt/hermes $PY scripts/carry_live.py enable --capital-cap 100
  sudo -u hermes env HOME=/opt/hermes $PY scripts/carry_live.py disable

config/carry.yaml stays DRY_RUN: true for ever; deploy.sh refuses anything
else and refuses to run while the switch exists.

enable: every check of carry/live_ops.LiveOps.problems, then a terminal
(not a pipe or an agent), a one-time code sent to Giannis's Telegram typed
back within 10 minutes, then the exact phrase "LIVE <cap> USDT"; the checks
run again right before the switch is written. The cap is signed into the
switch: a later change of TOTAL_CAPITAL_CAP_USD in the repo never raises it.

disable: only while the carry is FLAT (exchange and live book). With a
position open, Telegram /unwind carry first.

Exit codes: 0 done, 1 refused, 2 usage.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Callable, Dict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import settings  # noqa: E402
from carry import config as cc  # noqa: E402
from carry import live_ops  # noqa: E402
from carry import snapshot as snapshot_mod  # noqa: E402

CODE_TTL_S = 600


def _fp(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


class LiveCli:
    def __init__(self, cfg: Dict, env: Dict[str, str], client, notifier, *,
                 ask: Callable[[str], str] = input, isatty: Callable[[], bool] = sys.stdin.isatty,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 clock: Callable[[], float] = time.monotonic,
                 snapshot_fn: Callable = snapshot_mod.take,
                 roundtrip_active: Callable[[], bool] = live_ops.roundtrip_running,
                 out: Callable[[str], None] = print):
        self.ops = live_ops.LiveOps(cfg, env, client, now_ms=now_ms, snapshot_fn=snapshot_fn,
                                    roundtrip_active=roundtrip_active)
        self.cfg, self.env, self.notifier = cfg, env, notifier
        self.ask, self.isatty, self.clock, self.out = ask, isatty, clock, out

    def status(self) -> int:
        for line in self.ops.status():
            self.out(line)
        return 0

    def problems(self, cap: float):
        p = self.ops.problems(cap)
        if self.notifier is None or not getattr(self.notifier, "enabled", False):
            p.append("Telegram not configured (TELEGRAM_BOT_TOKEN and the operator chat): the "
                     "one-time code cannot be sent")
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
        want = live_ops.phrase(cap)
        if self.ask(f'Type exactly "{want}" to run the carry with real money: ').strip() != want:
            self.out("refusing: the phrase did not match; nothing changed")
            return 1
        by = os.environ.get("SUDO_USER") or getpass.getuser()
        try:
            body = self.ops.enable(cap, f"terminal {by}")
        except live_ops.LiveRefused as e:
            self.out(f"refusing: the checks no longer pass ({e}); run enable again")
            return 1
        self.out(f"LIVE: {settings.carry_live_file()} written; the next carry cycle trades real "
                 f"money (capital cap {cap:g} USDT)")
        self.notifier.event(f"🟢 CARRY LIVE enabled by {body['by']}: capital cap {cap:g} USDT, "
                            f"symbols {body['symbols']}. Next cycle trades real money. Stop: "
                            f"/unwind carry, then /live disable.")
        return 0

    def disable(self) -> int:
        try:
            removed = self.ops.disable()
        except live_ops.LiveRefused as e:
            self.out(f"refusing: {e}")
            return 1
        if not removed:
            self.out(f"paper already: no live switch at {settings.carry_live_file()}")
            return 0
        self.out(f"paper: {settings.carry_live_file()} removed; the next carry cycle runs DRY_RUN")
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
    from notify import Notifier
    env = settings.load_env()
    try:
        ops = live_ops.from_env(env)
    except cc.CarryConfigError as e:
        print(f"refusing: {e}")
        return 1
    cli = LiveCli(ops.cfg, env, ops.client, Notifier(env, ops.cfg))
    if args.cmd == "status":
        return cli.status()
    if args.cmd == "enable":
        return cli.enable(args.capital_cap)
    return cli.disable()


if __name__ == "__main__":
    sys.exit(main())
