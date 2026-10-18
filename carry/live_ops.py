"""carry/live_ops.py — the checks and writes behind the live switch, shared by
Giannis's two ways in (18/10): Telegram `/live enable <cap>` / `/live
disable` (telegram_bot.py) and scripts/carry_live.py (a terminal). Both
write the same signed file (carry/live.py); neither changes
config/carry.yaml, which stays DRY_RUN: true.

problems(cap) is everything that must hold before the carry trades real
money; enable() runs it again right before writing, so nothing that moved
between the request and the confirmation slips through. disable() removes
the switch only while the carry is FLAT (exchange and live book).
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable, Dict, List, Optional

import risk_state
import settings
from carry import config as cc
from carry import exposure as cx
from carry import live
from carry import risk
from carry import snapshot as snapshot_mod


class LiveRefused(RuntimeError):
    """A check failed; nothing was written."""


def roundtrip_running(run=subprocess.run) -> bool:
    try:
        out = run(["systemctl", "is-active", "yield-carry-roundtrip.service"],
                  capture_output=True, text=True).stdout
    except OSError:
        return False
    return out.strip() == "active"


def phrase(cap: float) -> str:
    """The exact confirmation, e.g. "LIVE 100 USDT"."""
    return f"LIVE {cap:g} USDT"


class LiveOps:
    def __init__(self, cfg: Dict, env: Dict[str, str], client, *,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 snapshot_fn: Callable = snapshot_mod.take,
                 roundtrip_active: Callable[[], bool] = roundtrip_running):
        self.cfg, self.env, self.client = cfg, env, client
        self.now_ms, self.snapshot_fn, self.roundtrip_active = now_ms, snapshot_fn, \
            roundtrip_active
        self.key = env.get("HERMES_RISK_HMAC_KEY", "")

    def exposure(self) -> cx.Exposure:
        """The exchange and the LIVE book (the paper account is not money)."""
        if self.client is None:
            return cx.Exposure(cx.UNKNOWN, ["the carry API key is not set"])
        return cx.check(self.client, dict(self.cfg, DRY_RUN=False), settings.carry_book_file(),
                        self.key, None, now_ms=self.now_ms(), take=self.snapshot_fn)

    def live_cfg(self, cap: float) -> Dict:
        return dict(self.cfg, DRY_RUN=False, DEADMAN_URL=self.env.get("DEADMAN_URL"),
                    TOTAL_CAPITAL_CAP_USD=cap)

    def status(self) -> List[str]:
        path = settings.carry_live_file()
        body, broken = live.read(path, self.key)
        if body:
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(body["enabled_ms"] / 1000))
            lines = [f"LIVE: {path} valid — enabled {when} by {body['by']}, capital cap "
                     f"{body['capital_cap']:g} USDT, symbols {body['symbols']}"]
        elif broken:
            lines = [f"LIVE SWITCH INVALID: {broken} (the carry does not trust it)"]
        else:
            lines = [f"paper (DRY_RUN): no live switch at {path}"]
        e = self.exposure()
        return lines + [f"carry exposure: {e.verdict}"] + [f"  {line}" for line in e.lines]

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
        if not (self.env.get("DEADMAN_URL") or "").startswith("https://"):
            p.append("DEADMAN_URL (https://...) not set in /opt/hermes/.env")
        if self.cfg.get("DEADMAN_URL") is not None:
            p.append(f"{settings.carry_config_file()}: DEADMAN_URL must stay null in the repo")
        top = self.cfg.get("TOTAL_CAPITAL_CAP_USD")
        if isinstance(cap, bool) or not (isinstance(cap, (int, float)) and 0 < cap <= (top or 0)):
            p.append(f"capital cap {cap!r} must be > 0 and <= TOTAL_CAPITAL_CAP_USD {top}")
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

    def enable(self, cap: float, by: str) -> Dict:
        """The checks again, then the signed switch. Raises LiveRefused."""
        problems = self.problems(cap)
        if problems:
            raise LiveRefused("; ".join(problems))
        return live.write(settings.carry_live_file(), self.key, cap,
                          list(self.cfg.get("SYMBOLS") or []), by, self.now_ms())

    def disable(self) -> bool:
        """True when removed, False when there was none. Raises LiveRefused
        unless the carry is FLAT."""
        path = settings.carry_live_file()
        if not path.exists():
            return False
        e = self.exposure()
        if e.verdict != cx.FLAT:
            raise LiveRefused(f"the carry is not FLAT ({e.verdict}): " + "; ".join(e.lines)
                              + ". Telegram /unwind carry, wait for FLAT, then disable again.")
        path.unlink()
        return True


def from_env(env: Optional[Dict[str, str]] = None) -> LiveOps:
    """The real thing: the carry config, the carry subaccount's own key."""
    from carry.client import CarryClient
    env = settings.load_env() if env is None else env
    cfg = cc.load(settings.carry_config_file(), testnet=settings.testnet())
    key_name, secret_name = settings.carry_api_key_names(testnet_=False)
    client = CarryClient(api_key=env[key_name], api_secret=env[secret_name], testnet=False) \
        if env.get(key_name) and env.get(secret_name) else None
    return LiveOps(cfg, env, client)
