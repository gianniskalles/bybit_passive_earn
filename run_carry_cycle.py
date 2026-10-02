#!/usr/bin/env python3
"""run_carry_cycle.py — the funding-carry cycle (CARRY_PLAN §6, every
CYCLE_MINUTES). No LLM anywhere (principle 3).

One cycle:
  1. config (strict schema; unusable -> CONFIG_INCOMPLETE, nothing else)
  2. carry risk state (own signed file); staleness/invalid only ever makes it
     more conservative. The hold latch (risk.py) adds NO_NEW_POSITIONS until
     the operator writes the carry risk state after it.
  3. the book (signed; unreadable -> empty + CRITICAL -> BOOK_MISMATCH)
  4. exchange: DRY_RUN -> PaperExchange (real market data, simulated account,
     accrued funding and Earn interest); live -> LiveExchange, after
     resolve_pending() settles spot orders of unknown fate
  5. snapshot (market data always real) -> plan (adopt=True only with a valid
     /adopt carry request) -> execute
  6. book, ledger (orders, funding, expected funding, round trips), R36
     underperformance, escalations -> hold latch
  7. cycle record in LOG_DIR/<date>.jsonl (the heartbeat reads its alerts),
     Telegram (deduplicated), dead-man ping (R33)
A record is written in every case; a crash is CYCLE_CRASH (blocking).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import traceback
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import risk_state
import settings
from bybit_earn_tool import BybitAPIError
from carry import adopt as adopt_req
from carry import book as book_store
from carry import config as cc
from carry import execute as ex
from carry import ledger, risk
from carry import snapshot as snapshot_mod
from carry.paper import PaperExchange
from carry.plan import FLAT, OPEN, plan_cycle

CARRY_KEY, CARRY_SECRET = "BYBIT_CARRY_API_KEY", "BYBIT_CARRY_API_SECRET"
CONSERVATIVE = {"NORMAL": 0, "NO_NEW_POSITIONS": 1, "UNWIND": 2}
LEDGER_LOOKBACK_MS = 15 * 86_400_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stricter(a: str, b: str) -> str:
    return a if CONSERVATIVE.get(a, 1) >= CONSERVATIVE.get(b, 1) else b


def resolve_risk_state(key: str, now_ms: int) -> Tuple[str, Any, List[str]]:
    v = risk_state.verify(settings.carry_risk_state_file(), key, now_ms=now_ms,
                          profile=risk_state.CARRY_PROFILE)
    if v.signature_valid:
        eff = v.state if v.fresh else ("UNWIND" if v.state == "UNWIND" else "NO_NEW_POSITIONS")
    else:
        eff = "NO_NEW_POSITIONS"
    return eff, v, ([] if v.ok else [f"{v.code}: {v.detail}"])


def public_apr(client) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """(productId, latest layer A APR, error) from public Earn data."""
    try:
        pid, rows = client.get_usdt_flexible_apr_history()
        rows = sorted((r for r in rows if str(r.get("timestamp", "")).isdigit()),
                      key=lambda r: int(r["timestamp"]))
        if not rows:
            return pid, None, "no APR points"
        raw = str(rows[-1].get("apr", "")).strip()
        apr = float(raw.rstrip("%")) / 100 if raw.endswith("%") else float(raw)
        return pid, apr, None
    except (BybitAPIError, ValueError, KeyError, TypeError) as e:
        return None, None, f"{type(e).__name__}: {e}"


def realized_funding_live(client, last_ms: Optional[int], now_ms: int) -> List[Dict]:
    """Funding from the transaction log (type SETTLEMENT). Field meaning
    (`change` > 0 = received) is UNVERIFIED until a testnet capture (§12)."""
    if last_ms is None:
        return []
    rows = client.get_transaction_log(last_ms + 1, now_ms, "USDT", type_="SETTLEMENT")
    return [{"type": "funding", "symbol": r.get("symbol"), "ts_ms": int(r.get("transactionTime")
                                                                      or now_ms),
             "amount": float(r.get("change") or 0.0), "source": "transaction_log"} for r in rows]


def _read_json(path: Path) -> Dict:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: Dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    os.replace(tmp, path)


def write_record(log_dir: Path, rec: Dict, now_ms: int) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    path = log_dir / f"{day}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return path


def run_cycle(cfg: Optional[Dict], env: Dict[str, str], client, *,
              config_error: Optional[str] = None, now_ms: Optional[int] = None,
              snapshot_fn: Callable = snapshot_mod.take, public=None,
              notifier=None, opener: Callable = urllib.request.urlopen,
              clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep,
              paper_start: Tuple[float, float] = (0.0, 0.0),
              exchange=None) -> Tuple[Dict, int]:
    """One carry cycle; returns (record, exit code). Exit codes: 0 ok,
    3 CONFIG_INCOMPLETE, 4 CYCLE_CRASH. `exchange` overrides the live
    exchange (tests); DRY_RUN always uses the paper account."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cycle_id = datetime.fromtimestamp(now / 1000, timezone.utc).strftime("%Y%m%d_%H%M%S") + \
        "_" + uuid.uuid4().hex[:6]
    alerts: List[str] = []
    rec: Dict[str, Any] = {"ts": _now_iso(), "cycle_ms": now, "cycle_id": cycle_id,
                           "system": "carry", "alerts": alerts, "dry_run": True,
                           "risk_state": "NO_NEW_POSITIONS", "actions": [], "orders": [],
                           "decisions": {}, "no_entry": {}}
    cfg = cfg if isinstance(cfg, dict) else {}
    log_dir = Path(cfg["LOG_DIR"]) if isinstance(cfg.get("LOG_DIR"), str) else \
        settings.default_log_dir() / "carry"
    started = time.monotonic()
    try:
        rc = _cycle(cfg, env, client, config_error, now, cycle_id, rec, alerts, snapshot_fn,
                    public, clock, sleep, paper_start, exchange, log_dir)
    except Exception as e:                                       # noqa: BLE001
        alerts.append(f"CYCLE_CRASH: {type(e).__name__}: {e}")
        rec["crash"] = traceback.format_exc(limit=8)
        rc = 4
    rec["cycle_duration_seconds"] = round(time.monotonic() - started, 3)
    try:
        write_record(log_dir, rec, now)
    except OSError as e:
        print(f"[carry] could not write the cycle record: {e}", file=sys.stderr)
    if notifier is not None:
        notify_cycle(rec, notifier)
    if rc == 0 and not any(a.startswith("CYCLE_CRASH") for a in alerts):
        ping = risk.deadman_ping(cfg.get("DEADMAN_URL"), opener=opener)
        if ping:
            alerts.append(ping)
    return rec, rc


def _cycle(cfg, env, client, config_error, now, cycle_id, rec, alerts, snapshot_fn, public,
           clock, sleep, paper_start, exchange, log_dir) -> int:
    testnet = bool(getattr(client, "testnet", False))
    errors = [config_error] if config_error else cc.validate(
        cfg, testnet, alt_report=cc.read_alt_report(cc.ALT_REPORT_FILE))
    if errors:
        alerts.append("CONFIG_INCOMPLETE: " + "; ".join(errors))
        return 3
    key = env.get("HERMES_RISK_HMAC_KEY", "")
    if not key:
        alerts.append("CONFIG_INCOMPLETE: HERMES_RISK_HMAC_KEY not set (book and risk state are "
                      "signed with it)")
        return 3
    dry = bool(cfg["DRY_RUN"])
    rec["dry_run"] = dry

    # ---- risk state + hold latch ---------------------------------------------------
    state, verification, rs_alerts = resolve_risk_state(key, now)
    alerts.extend(rs_alerts)
    # The heartbeat promotes its bootstrap record only after a clean cycle
    # verified exactly that record (heartbeat.clean_cycle_verified).
    rec["risk_state_meta"] = verification.as_dict()
    hold_file = settings.carry_hold_file()
    hold = risk.read_hold(hold_file)
    if hold and risk.hold_released(hold, verification):
        risk.clear_hold(hold_file)
        alerts.append(f"HOLD_RELEASED: operator wrote the carry risk state after "
                      f"'{hold['reason']}'")
        hold = None
    if hold:
        state = stricter(state, "NO_NEW_POSITIONS")
        alerts.append(f"CARRY_HOLD: {hold['reason']} (since {hold['since_ms']}); released only "
                      f"by an operator write of the carry risk state")
    rec["risk_state"] = state

    # ---- book ------------------------------------------------------------------------
    book, book_alert = book_store.load(settings.carry_book_file(), key)
    if book_alert:
        alerts.append(book_alert)
    cycle_state = _read_json(settings.carry_cycle_file())
    last_ms = cycle_state.get("last_ms")
    events: List[Dict] = []

    # ---- exchange + snapshot ---------------------------------------------------------
    if dry:
        paper = PaperExchange.load(settings.carry_paper_file(), cfg, public=public,
                                   initial_usdt=paper_start[0], initial_earn=paper_start[1])
        raw = snapshot_fn(client, cfg, now_ms=now, private=False)
        pid, apr, apr_err = public_apr(client)
        if apr_err:
            alerts.append(f"LAYER_A_UNREADABLE: {apr_err}")
        events.extend(paper.accrue(raw, apr))
        snap = paper.overlay(raw, apr, pid or "paper")
        venue = paper
    else:
        venue = exchange or ex.LiveExchange(client, dry_run=False)
        upd, pend_alerts = ex.resolve_pending(book, venue)
        book.update(upd)
        alerts.extend(pend_alerts)
        snap = snapshot_fn(client, cfg, now_ms=now, private=True)
        try:
            events.extend(realized_funding_live(client, last_ms, now))
        except BybitAPIError as e:
            alerts.append(f"FUNDING_LOG_UNREADABLE: {e}")
    rec["snapshot_errors"] = dict(snap.errors)
    events.extend(risk.expected_funding(snap, book, last_ms))

    # ---- plan + execute ----------------------------------------------------------------
    request = adopt_req.read_request(settings.carry_adopt_file(), key, now)
    plan = plan_cycle(snap, cfg, state, book, cycle_id, adopt=request is not None)
    alerts.extend(plan.alerts)
    rec["decisions"] = {s: {"action": d.action, "kind": d.kind, "reason": d.reason}
                        for s, d in plan.decisions.items()}
    rec["no_entry"] = {s: list(w) for s, w in plan.no_entry.items()}
    rec["funding"] = funding_view(plan.decisions, snap, cfg)
    rec["actions"] = [a.__dict__ for a in plan.actions]
    result = ex.execute_plan(plan, snap, cfg, state, book, cycle_id, venue, clock=clock,
                             sleep=sleep)
    alerts.extend(result.alerts)
    rec["orders"] = result.orders
    before = dict(book)
    book.update(plan.book_updates)
    book.update(result.book_updates)
    if plan.adopted:
        adopt_req.consume(settings.carry_adopt_file())
        rec["adopted"] = list(plan.adopted)

    # ---- ledger, R36, escalations ---------------------------------------------------------
    events.extend(ledger.order_events(result.orders))
    history = ledger.read(log_dir, since_ms=now - LEDGER_LOOKBACK_MS) + events
    for sym, sb in book.items():
        prev = before.get(sym)
        if prev is not None and prev.status == OPEN and sb.status == FLAT:
            opened = prev.entered_ms or (prev.entry_times[-1] if prev.entry_times else now)
            legs = ledger.read(log_dir, since_ms=min(opened, now - LEDGER_LOOKBACK_MS)) + events
            rt = ledger.round_trip(sym, legs, now)
            if rt:
                events.append(rt)
    ledger.write(log_dir, events, now, cycle_id)
    under = risk.underperformance(history, cfg, now)
    if under:
        alerts.append(under)
    reasons = [r for r in (plan.escalate and "BOOK_MISMATCH",
                           result.escalate and "orphan protection / incomplete close",
                           under and "UNDERPERFORMANCE (R36)") if r]
    if reasons:
        risk.write_hold(hold_file, "; ".join(reasons), now)

    # ---- persist -------------------------------------------------------------------------
    book_store.save(settings.carry_book_file(), key, book, now)
    if dry:
        paper.save(settings.carry_paper_file())
        rec["paper"] = {"usdt": paper.s.usdt, "earn_staked": paper.s.earn_staked,
                        "coins": dict(paper.s.coins), "shorts": dict(paper.s.shorts)}
    _write_json(settings.carry_cycle_file(), {"last_ms": now, "cycle_id": cycle_id})
    rec["book"] = {s: sb.__dict__ for s, sb in book.items()}
    return 0


def funding_view(decisions, snap, cfg) -> Dict[str, Dict]:
    """Per symbol: the smoothed funding as an APR now, and what the entry rule
    needs it to be (ENTRY_MIN_EXPECTED_APR above layer A, decide.py) — the
    daily summary shows how close an entry is."""
    layer_a = snap.earn.apr if snap.earn is not None else None
    need = float(cfg["ENTRY_MIN_EXPECTED_APR"])
    return {s: {"smoothed_apr": d.expected_apr, "layer_a_apr": layer_a,
                "entry_min_expected_apr": need,
                "required_apr": None if layer_a is None else need + layer_a}
            for s, d in decisions.items()}


def notify_cycle(rec: Dict, notifier) -> None:
    from heartbeat import CARRY_BLOCKING_CODES, _is_blocking_code
    blocking = sorted({c for c in (_is_blocking_code(a, CARRY_BLOCKING_CODES)
                                   for a in rec.get("alerts", [])) if c})
    notifier.observe("carry_cycle", ",".join(blocking) or None,
                     f"🚨 CARRY {rec.get('cycle_id')}: {', '.join(blocking)}. "
                     f"Alerts: {'; '.join(rec.get('alerts', []))[:1500]}",
                     resolved_text="✅ CARRY: no blocking codes any more")
    state = rec.get("risk_state")
    notifier.observe("carry_risk_state", None if state == "NORMAL" else state,
                     f"⚠️ CARRY RISK STATE {state}: no new positions",
                     resolved_text="✅ CARRY risk state back to NORMAL")
    if not rec.get("dry_run"):
        for o in rec.get("orders", []):
            if o.get("filled"):
                req = o.get("request") or {}
                notifier.event(f"✅ CARRY ORDER {req.get('category')} {req.get('side')} "
                               f"{o.get('filled')} {req.get('symbol')} @ {o.get('avg_price')} "
                               f"(slippage {o.get('slippage_bps')} bps)")


def main() -> int:
    from carry.client import CarryClient
    from notify import Notifier
    env = settings.load_env()
    testnet = settings.testnet()
    cfg, error = None, None
    try:
        cfg = cc.load(settings.carry_config_file(), testnet=testnet)
    except cc.CarryConfigError as e:
        error = str(e)
    if testnet and cfg is not None and cfg.get("TESTNET_ONLY") is not True:
        # A testnet run with a mainnet config would write the mainnet LOG_DIR
        # (cycle records the mainnet heartbeat reads, ledger).
        cfg, error = None, (f"{settings.carry_config_file()}: BYBIT_TESTNET is set but the config "
                            f"is not TESTNET_ONLY; testnet runs use config/carry.testnet.yaml")
    # Its own key, for its own subaccount (CARRY_PLAN §3.6, 0A). Never the
    # yield rotation's BYBIT_API_KEY: two systems never share an account.
    client = CarryClient(api_key=env.get(CARRY_KEY), api_secret=env.get(CARRY_SECRET),
                         testnet=testnet)
    lock_path = settings.carry_cycle_file().with_name("carry_cycle.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[carry] another cycle is running; skipped", file=sys.stderr)
            return 0
        # The paper account starts as decision 13.3 has it: the whole capital
        # in Easy Earn; it then lives in YIELD_CARRY_PAPER_FILE.
        start = (0.0, float((cfg or {}).get("TOTAL_CAPITAL_CAP_USD") or 0.0))
        rec, rc = run_cycle(cfg, env, client, config_error=error, public=client,
                            notifier=Notifier(env, cfg or {}), paper_start=start)
    print(json.dumps({"cycle_id": rec["cycle_id"], "rc": rc, "alerts": rec["alerts"]}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
