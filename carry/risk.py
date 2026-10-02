"""carry/risk.py — risk checks that span cycles (CARRY_PLAN Phase 5).

Per-cycle risk (margin, ADL, CVR, borrowing, orphan legs, lost book) lives
in carry/plan.py and acts in the same cycle. Here:

  expected_funding  what the book's short should receive at each settlement
                    since the last cycle: rate x qty x mark.
  underperformance  R36: over the last UNDERPERFORMANCE_DAYS, the realized
                    funding is below UNDERPERFORMANCE_RATIO x expected ->
                    UNDERPERFORMANCE (the runner holds NO_NEW_POSITIONS).
                    Judged only once the window is fully covered.
  deadman_ping      R33: one GET to DEADMAN_URL per healthy cycle; a failure
                    is an alert, never a reason to stop the cycle.
  Hold              the escalation latch: an orphan leg, a lost book or an
                    underperformance holds NO_NEW_POSITIONS (alert
                    CARRY_HOLD, a blocking code) until the operator writes
                    the carry risk state after it.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional

from carry.plan import OPEN, SymbolBook
from carry.snapshot import Snapshot

DAY_MS = 86_400_000
UNDERPERFORMANCE_DAYS = 14


def expected_funding(snap: Snapshot, book: Mapping[str, SymbolBook],
                     last_ms: Optional[int]) -> List[Dict]:
    out: List[Dict] = []
    if last_ms is None:
        return out
    for sym, sb in book.items():
        m = snap.markets.get(sym)
        if sb.status != OPEN or sb.perp_qty <= 0 or m is None:
            continue
        for ts, rate in m.settled:
            if last_ms < ts <= snap.taken_ms:
                out.append({"type": "expected_funding", "symbol": sym, "ts_ms": ts, "rate": rate,
                            "qty": sb.perp_qty, "mark": m.mark_price,
                            "amount": sb.perp_qty * m.mark_price * rate})
    return out


def underperformance(rows: Iterable[Mapping], cfg: Mapping, now_ms: int) -> Optional[str]:
    start = now_ms - UNDERPERFORMANCE_DAYS * DAY_MS
    rows = list(rows)
    expected = [r for r in rows if r.get("type") == "expected_funding"]
    if not expected or min(r["ts_ms"] for r in expected) > start + DAY_MS:
        return None                                    # the window is not covered yet
    window = [r for r in rows if r.get("ts_ms", 0) >= start]
    exp = sum(float(r["amount"]) for r in window if r.get("type") == "expected_funding")
    real = sum(float(r["amount"]) for r in window if r.get("type") == "funding")
    ratio = float(cfg["UNDERPERFORMANCE_RATIO"])
    if exp > 0 and real < ratio * exp:
        return (f"UNDERPERFORMANCE: funding received {real:.4f} over {UNDERPERFORMANCE_DAYS} days "
                f"< {ratio} x expected {exp:.4f} (R36)")
    return None


def deadman_ping(url: Optional[str],
                 opener: Callable = urllib.request.urlopen, timeout: float = 10) -> Optional[str]:
    if not url:
        return None
    try:
        with opener(url, timeout=timeout) as r:
            status = getattr(r, "status", 200)
        if status != 200:
            return f"DEADMAN_PING_FAILED: HTTP {status}"
        return None
    except Exception as e:                              # noqa: BLE001
        return f"DEADMAN_PING_FAILED: {type(e).__name__}: {e}"


# ---- the escalation latch --------------------------------------------------------------

def read_hold(path: Path) -> Optional[Dict]:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) and d.get("reason") else None
    except (OSError, ValueError):
        return None


def write_hold(path: Path, reason: str, now_ms: int) -> None:
    if read_hold(path):
        return                                          # keep the first reason and time
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"reason": reason, "since_ms": int(now_ms)}))
    os.replace(tmp, path)


def clear_hold(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def hold_released(hold: Mapping, verification) -> bool:
    """The operator acknowledged: a signed carry risk state written by the
    operator after the hold began."""
    return bool(verification is not None and verification.signature_valid
                and verification.source == "operator" and verification.ts is not None
                and _ts_ms(verification.ts) > int(hold["since_ms"]))


def _ts_ms(ts) -> int:
    if isinstance(ts, (int, float)):
        return int(ts)
    from datetime import datetime
    return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
