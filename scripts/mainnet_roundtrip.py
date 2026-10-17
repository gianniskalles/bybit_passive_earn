#!/usr/bin/env python3
"""mainnet_roundtrip.py — ONE supervised carry round trip with real money on
the carry subaccount (decision of 17/10, instead of testnet).

  redeem from Easy Earn -> entry at the MINIMUM ETHUSDT size -> wait one
  funding settlement -> exit both legs -> USDT back to Earn -> stop.

Every step is one production cycle (run_carry_cycle.run_cycle: the same
plan, execution, book, ledger and Telegram), live, with an in-memory config
that only this script can build: the entry thresholds are forced, the size
is the minimum position, and nothing exceeds MAX_NOTIONAL_USD (50 USDT, a
hard limit no option lifts). After each cycle the carry heartbeat runs, as
the timer would. Every Bybit response body is kept in <LOG_DIR>/raw/ (no
header, query, request body, key or signature).

It refuses to start unless:
  --i-approve-real-money is given; not testnet; config/carry.yaml still says
  DRY_RUN: true (it is never changed, here or anywhere); every carry timer
  is stopped; the carry holds nothing (exchange, paper, book); the carry
  risk state is NORMAL and there is no CARRY_HOLD; the minimum position
  plus the buffer fits in 50 USDT and in the USDT available.
While it runs it holds the carry cycle lock: a timer started by mistake only
logs "another cycle is running; skipped". It never writes the risk state:
the exit is forced in memory (UNWIND, then NO_NEW_POSITIONS while the USDT
goes back to Earn), only ever stricter than the signed state.
A stop (SIGTERM/SIGINT, `systemctl stop yield-carry-roundtrip`) or any
blocking alert while a position is open goes straight to the exit, and the
script keeps cycling until the carry is FLAT. --finish resumes an
interrupted run from its exit.

  sudo systemd-run --uid=hermes --gid=hermes --unit=yield-carry-roundtrip \\
      --setenv=HOME=/opt/hermes --property=WorkingDirectory=/opt/hermes/yield_rotation \\
      --property=TimeoutStopSec=600 \\
      /opt/hermes/venvs/yield_rotation/bin/python \\
      /opt/hermes/yield_rotation/scripts/mainnet_roundtrip.py --i-approve-real-money
  .../mainnet_roundtrip.py --check          # read only: what it would do

Exit codes: 0 done (FLAT, USDT back in Earn), 2 refused, 3 config error,
4 stopped with a position still open (see the report), 5 finished FLAT but
no entry happened (the report says why).

Run it with --property=TimeoutStopSec=600 (as below), so that a stop leaves
time for the exit cycles before systemd would kill it.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
import risk_state  # noqa: E402
import run_carry_cycle as rcc  # noqa: E402
import settings  # noqa: E402
from carry import book as book_store  # noqa: E402
from carry import config as cc  # noqa: E402
from carry import exposure as cx  # noqa: E402
from carry import ledger, risk  # noqa: E402
from carry import snapshot as snapshot_mod  # noqa: E402
from carry.plan import FLAT, OPEN, REDEEMING, SymbolBook  # noqa: E402
from carry.preflight import min_position, spot_fee  # noqa: E402

SYMBOL = "ETHUSDT"
MAX_NOTIONAL_USD = cc.ROUNDTRIP_MAX_NOTIONAL_USD     # 50, hard
BUFFER_USD = 5.0                                     # 10 % of the 50 cap (decision 13.9)
SIZE_SLACK = 1.02                                    # the limit = minimum position + 2 %
REDEEM_SLACK = 1.01
CYCLE_S = 300
FAST_S = 30                                          # between exit cycles
ENTER_DEADLINE_H = 3
AFTER_SETTLEMENT_MIN = 10
MAX_RUN_H = 24
QUIET_CYCLES_TO_FINISH = 2
MAX_LOOP_ERRORS = 3                                  # in a row, while a position may be open
WAIT_STEP_S = 5                                      # a stop is seen within this
CARRY_TIMERS = ("yield-carry-cycle.timer", "yield-carry-heartbeat.timer",
                "yield-carry-summary.timer", "yield-carry-calibrate.timer")
# The entry is forced; everything else (risk limits, settlement window,
# basis and spread checks) stays as config/carry.yaml has it.
FORCED_ENTRY = {"ENTRY_MIN_EXPECTED_APR": -100.0, "ENTRY_MIN_PREDICTED_RATE": -1.0,
                "ENTRY_EV_MULTIPLE": 0, "EXIT_PREDICTED_FLOOR": -2.0, "MIN_HOLD_HOURS": 0,
                "SMOOTHING_SETTLEMENTS": 1}
FORCE = {"enter": None, "hold": None, "exit": "UNWIND", "return": "NO_NEW_POSITIONS"}


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--i-approve-real-money", action="store_true", dest="approved",
                    help="required: real orders on the carry subaccount")
    ap.add_argument("--check", action="store_true", help="read only: the checks and the size")
    ap.add_argument("--finish", action="store_true",
                    help="resume an interrupted round trip from its exit")
    return ap.parse_args(argv)


def roundtrip_cfg(base: Dict, notional: float) -> Dict:
    """The in-memory config of the round trip. Never written anywhere."""
    if notional > MAX_NOTIONAL_USD - BUFFER_USD or notional <= 0:
        raise ValueError(f"notional {notional:.2f} outside (0, {MAX_NOTIONAL_USD} - "
                         f"{BUFFER_USD}] USDT (hard limit {MAX_NOTIONAL_USD})")
    cfg = dict(base)
    cfg.update(FORCED_ENTRY)
    cfg.update(SYMBOLS=[SYMBOL], DRY_RUN=False, TOTAL_CAPITAL_CAP_USD=MAX_NOTIONAL_USD,
               USDT_BUFFER_USD=BUFFER_USD, MAX_NOTIONAL_PER_SYMBOL_USD=round(notional, 2),
               CALIBRATION_SOURCE="supervised mainnet round trip (17/10) — NOT a calibration")
    cfg.pop("TESTNET_ONLY", None)
    return cfg


def systemd_timers_active(run=subprocess.run) -> List[str]:
    active = []
    for t in CARRY_TIMERS:
        try:
            out = run(["systemctl", "is-active", t], capture_output=True, text=True).stdout
        except OSError:
            return [f"{t} (systemctl unavailable)"]
        if out.strip() == "active":
            active.append(t)
    return active


@contextlib.contextmanager
def cycle_lock():
    """The carry cycle's own lock, held for the whole run."""
    path = settings.carry_cycle_file().with_name("carry_cycle.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("the carry cycle lock is held: a carry cycle is running")
        yield


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Roundtrip:
    def __init__(self, base_cfg: Dict, env: Dict, client, *, exchange=None,
                 snapshot_fn: Callable = snapshot_mod.take,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], None] = time.sleep,
                 heartbeat: Optional[Callable[[], None]] = None,
                 timers_active: Callable[[], List[str]] = systemd_timers_active,
                 notifier=None, finish: bool = False):
        self.base, self.env, self.client = base_cfg, env, client
        self.exchange, self.snapshot_fn = exchange, snapshot_fn
        self.now_ms, self._sleep = now_ms, sleep
        self.heartbeat = heartbeat or (lambda: heartbeat_module_main())
        self.timers_active, self.notifier = timers_active, notifier
        self.key = env.get("HERMES_RISK_HMAC_KEY", "")
        self.phase = "exit" if finish else "enter"
        self.finish = finish
        self.timeline: List[Tuple[str, int]] = []
        self.stop_reason: Optional[str] = None
        self.exit_reason: Optional[str] = None
        self.entered_ms: Optional[int] = None
        self.settlement_ms: Optional[int] = None
        self.notional: Optional[float] = None
        self.entry: Dict = {}
        self.cycles = 0
        self.report: Dict = {}
        self.report_file: Optional[str] = None
        self.yaml_sha = _sha(settings.carry_config_file())

    # ---- checks -----------------------------------------------------------------------
    def _snap(self, now: int):
        return self.snapshot_fn(self.client, roundtrip_cfg(self.base, 1.0), now_ms=now,
                                private=True)

    def _min_entry(self, snap) -> Tuple[float, float]:
        m = snap.markets[SYMBOL]
        mp = min_position(m, spot_fee(snap, SYMBOL, self.base))
        return mp.perp_qty, mp.notional_usd

    def checks(self) -> List[str]:
        p: List[str] = []
        if settings.testnet():
            p.append("BYBIT_TESTNET is set: this round trip is mainnet only")
        if self.base.get("DRY_RUN") is not True:
            p.append(f"{settings.carry_config_file()}: DRY_RUN must stay true (it is "
                     f"{self.base.get('DRY_RUN')!r}); the round trip never changes it")
        active = self.timers_active()
        if active:
            p.append(f"carry timers still active: {active} — stop them first")
        if not self.finish:
            e = cx.check(self.client, roundtrip_cfg(self.base, 1.0), settings.carry_book_file(),
                         self.key, settings.carry_paper_file(), now_ms=self.now_ms(),
                         take=self.snapshot_fn)
            if e.verdict != cx.FLAT:
                p.append(f"the carry is not FLAT ({e.verdict}): " + "; ".join(e.lines))
            v = risk_state.verify(settings.carry_risk_state_file(), self.key,
                                  now_ms=self.now_ms(), profile=risk_state.CARRY_PROFILE)
            if not (v.ok and v.state == "NORMAL"):
                p.append(f"carry risk state is {v.state} ({v.code}); NORMAL is needed — "
                         f"run the carry heartbeat")
            hold = risk.read_hold(settings.carry_hold_file())
            if hold:
                p.append(f"CARRY_HOLD: {hold['reason']} — release it first (/resume carry)")
            try:
                snap = self._snap(self.now_ms())
                perp_qty, notional = self._min_entry(snap)
                need = notional * SIZE_SLACK
                if need > MAX_NOTIONAL_USD - BUFFER_USD:
                    p.append(f"minimum position {perp_qty} ETH = {notional:.2f} USDT; with the "
                             f"{BUFFER_USD} buffer it exceeds the hard limit of "
                             f"{MAX_NOTIONAL_USD} USDT")
                acct = snap.account
                if acct is None:
                    p.append("account unreadable")
                else:
                    if not acct.cross_margin:
                        p.append(f"margin mode {acct.margin_mode}: Cross Margin is needed")
                    coll = acct.collateral.get("ETH")
                    if coll is None or not coll.active:
                        p.append("ETH is not active collateral (Bybit UI: Collateral -> ETH On)")
                usdt = snap.account.balance("USDT").wallet if snap.account else 0.0
                staked = snap.earn.staked if snap.earn else 0.0
                total = need * REDEEM_SLACK + BUFFER_USD
                if usdt + staked < total:
                    p.append(f"not enough USDT: {usdt:.2f} in the UTA + {staked:.2f} in Earn < "
                             f"{total:.2f} (minimum position + buffer)")
                self.entry = {"perp_qty": perp_qty, "min_notional": notional,
                              "usdt_needed": total, "usdt_uta": usdt, "usdt_earn": staked}
            except Exception as e:                                  # noqa: BLE001
                p.append(f"market or account unreadable: {type(e).__name__}: {e}")
        return p

    # ---- the run ----------------------------------------------------------------------
    def request_stop(self, why: str) -> None:
        self.stop_reason = why

    def _wait(self, seconds: float) -> None:
        """Sleep in short steps, so a stop is acted on within WAIT_STEP_S."""
        left = seconds
        while left > 0 and not (self.stop_reason and self.phase in ("enter", "hold")):
            step = min(WAIT_STEP_S, left)
            self._sleep(step)
            left -= step

    def _to(self, phase: str, why: str = "") -> None:
        self.phase = phase
        self.timeline.append((phase, self.now_ms()))
        if why:
            print(f"[roundtrip] -> {phase}: {why}", file=sys.stderr)
        self._save_state()

    def _save_state(self) -> None:
        path = settings.carry_state_dir() / "roundtrip.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"phase": self.phase, "timeline": self.timeline,
                                    "entered_ms": self.entered_ms,
                                    "settlement_ms": self.settlement_ms,
                                    "notional": self.notional, "cycles": self.cycles}))

    def _book(self) -> SymbolBook:
        book, _ = book_store.load(settings.carry_book_file(), self.key)
        return book.get(SYMBOL, SymbolBook())

    def _event(self, text: str) -> None:
        print(f"[roundtrip] {text}", file=sys.stderr)
        if self.notifier is not None:
            self.notifier.event(f"🧪 CARRY ROUNDTRIP: {text}")

    def run(self) -> int:
        started = self.now_ms()
        self._to(self.phase, "start")
        self._event(f"started ({self.phase}); hard limit {MAX_NOTIONAL_USD} USDT")
        quiet = 0
        errors = 0
        rc_final = 0
        while self.phase != "done":
            try:
                step = self._step(started, quiet)
            except Exception as e:                                  # noqa: BLE001
                # Never fall over with a position possibly open: log, retry,
                # and after MAX_LOOP_ERRORS in a row go to the exit.
                errors += 1
                print(f"[roundtrip] loop error {errors}: {type(e).__name__}: {e}", file=sys.stderr)
                if errors >= MAX_LOOP_ERRORS and self.phase in ("enter", "hold"):
                    self.exit_reason = f"{errors} loop errors in a row: {type(e).__name__}: {e}"
                    self._to("exit", self.exit_reason)
                self._wait(FAST_S)
                continue
            errors = 0
            if step is None:
                continue
            quiet, rc_final, fast, stop = step
            if stop:
                break
            self._wait(FAST_S if fast else CYCLE_S)

        sb = self._book()
        if rc_final == 0 and (sb.status != FLAT or sb.perp_qty or sb.spot_qty):
            rc_final = 4
        elif rc_final == 0 and self.entered_ms is None:
            rc_final = 5
        return self._finish(rc_final, sb)

    def _step(self, started: int, quiet: int):
        """One loop: one production cycle, the heartbeat, the next phase.
        Returns (quiet, rc, fast, stop)."""
        rc_final = 0
        now = self.now_ms()
        if self.stop_reason and self.phase in ("enter", "hold"):
            self.exit_reason = f"stop requested ({self.stop_reason})"
            self._to("exit", self.exit_reason)
        if now - started > MAX_RUN_H * 3_600_000 and self.phase in ("enter", "hold"):
            self.exit_reason = f"{MAX_RUN_H} h limit"
            self._to("exit", self.exit_reason)
        if self.timers_active():
            print(f"[roundtrip] WARNING: carry timers active {self.timers_active()}; they "
                  f"only skip while this run holds the lock", file=sys.stderr)

        snap = None
        if self.phase == "enter":
            snap = self._snap(now)
            perp_qty, notional = self._min_entry(snap)
            self.notional = min(notional * SIZE_SLACK, MAX_NOTIONAL_USD - BUFFER_USD)
        cfg = roundtrip_cfg(self.base, self.notional or 1.0)
        rec, rc = rcc.run_cycle(cfg, self.env, self.client, now_ms=now,
                                snapshot_fn=self.snapshot_fn, notifier=self.notifier,
                                exchange=self.exchange, sleep=self._sleep,
                                roundtrip=True, force_state=FORCE[self.phase])
        self.cycles += 1
        self.heartbeat()
        if rc == 3:
            self._event("CONFIG_INCOMPLETE: " + "; ".join(rec.get("alerts", [])))
            return quiet, 3, False, True
        blocking = sorted({c for c in (heartbeat._is_blocking_code(a, heartbeat.CARRY_BLOCKING_CODES)
                                       for a in rec.get("alerts", [])) if c})
        sb = self._book()

        if self.phase == "enter":
            if sb.status != OPEN:
                d = (rec.get("decisions") or {}).get(SYMBOL) or {}
                why = list((rec.get("no_entry") or {}).get(SYMBOL) or ())
                if d.get("action") != "ENTER":
                    why.append(f"decision {d.get('action')}: {d.get('reason')}")
                if why:
                    print(f"[roundtrip] no entry yet: {why}", file=sys.stderr)
                    self.entry["last_no_entry"] = why
            if sb.status == OPEN:
                self.entered_ms = now
                m = snap.markets.get(SYMBOL) if snap is not None else None
                self.settlement_ms = m.next_funding_time_ms if m is not None else \
                    now + 8 * 3_600_000                  # at most one interval away
                self.entry.update(perp_qty=sb.perp_qty, spot_qty=sb.spot_qty,
                                  limit_usdt=self.notional)
                self._event(f"entered: short {sb.perp_qty} perp, {sb.spot_qty} spot; "
                            f"waiting for the settlement at {_iso(self.settlement_ms)}")
                self._to("hold", "entered")
            elif blocking:
                self.exit_reason = f"blocking alert before the entry: {blocking}"
                self._to("exit", self.exit_reason)
            elif now - started > ENTER_DEADLINE_H * 3_600_000:
                self.exit_reason = f"no entry within {ENTER_DEADLINE_H} h"
                self._to("exit", self.exit_reason)
        elif self.phase == "hold":
            if sb.status != OPEN:
                self.exit_reason = f"position closed by a risk exit ({sb.status})"
                self._to("exit", self.exit_reason)
            elif blocking:
                self.exit_reason = f"blocking alert: {blocking}"
                self._to("exit", self.exit_reason)
            elif now >= self.settlement_ms + AFTER_SETTLEMENT_MIN * 60_000:
                self.exit_reason = "one settlement passed"
                self._to("exit", self.exit_reason)
        elif self.phase == "exit":
            if sb.status == FLAT and sb.perp_qty == 0 and sb.spot_qty == 0 \
                    and not sb.pending_spot:
                self._event("FLAT: both legs closed")
                self._to("return", "flat")
        elif self.phase == "return":
            busy = rec.get("actions") or sb.status == REDEEMING
            quiet = 0 if busy else quiet + 1
            if quiet >= QUIET_CYCLES_TO_FINISH:
                self._to("done", "nothing left to do")
                return quiet, 0, False, True
            if self.stop_reason:
                self._to("done", f"stop requested ({self.stop_reason}) while FLAT")
                return quiet, 0, False, True
        fast = self.phase == "exit" or bool(self.stop_reason and self.phase != "return")
        return quiet, rc_final, fast, False

    def _finish(self, rc: int, sb: SymbolBook) -> int:
        log_dir = Path(self.base["LOG_DIR"])
        rows = ledger.read(log_dir, since_ms=self.timeline[0][1] if self.timeline else 0)
        self.report = {
            "result": {0: "done", 3: "config error", 4: "POSITION STILL OPEN",
                       5: "no entry (FLAT)"}.get(rc, str(rc)),
            "timeline": [(p, _iso(t)) for p, t in self.timeline],
            "exit_reason": self.exit_reason, "entry": self.entry, "cycles": self.cycles,
            "book": sb.__dict__, "orders": [r for r in rows if r.get("type") == "order"],
            "funding": [r for r in rows if r.get("type") == "funding"],
            "round_trip": [r for r in rows if r.get("type") == "round_trip"],
            "summary": ledger.summary(rows),
            "carry_yaml_unchanged": _sha(settings.carry_config_file()) == self.yaml_sha,
            "raw_responses": str(log_dir / "raw"),
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = log_dir / f"roundtrip_{stamp}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.report, indent=2, default=str, ensure_ascii=False))
        self.report_file = str(out)
        s = self.report["summary"]
        self._event(f"{self.report['result']}: funding {s['funding']:+.4f}, fees {s['fees']:.4f}, "
                    f"basis/execution {s['basis_pnl']:+.4f}, net {s['net']:+.4f} USDT; "
                    f"report {out}")
        return rc


def heartbeat_module_main() -> None:
    heartbeat.main(["--system", "carry"])


def _iso(ms: Optional[int]) -> Optional[str]:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not (args.approved or args.check):
        print("refusing: real money. Pass --i-approve-real-money (or --check to only look).",
              file=sys.stderr)
        return 2
    if settings.testnet():
        print("refusing: BYBIT_TESTNET is set; this round trip is mainnet only", file=sys.stderr)
        return 2
    from carry.client import CarryClient
    from carry.testnet_record import ResponseRecorder
    from notify import Notifier
    env = settings.load_env()
    try:
        base = cc.load(settings.carry_config_file(), testnet=False)
    except cc.CarryConfigError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 3
    key_name, secret_name = settings.carry_api_key_names(testnet_=False)
    session = None
    if args.approved and not args.check:
        session = ResponseRecorder(Path(base["LOG_DIR"]) / "raw", testnet=False,
                                   approved_mainnet=True)
    client = CarryClient(api_key=env.get(key_name), api_secret=env.get(secret_name),
                         testnet=False, session=session)
    run = Roundtrip(base, env, client, notifier=Notifier(env, base), finish=args.finish)
    problems = run.checks()
    print(json.dumps({"entry": run.entry, "problems": problems}, indent=2, default=str))
    if args.check or problems:
        if problems:
            print("refusing:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2 if problems else 0
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, f: run.request_stop(signal.Signals(s).name))
    try:
        with cycle_lock():
            return run.run()
    except RuntimeError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
