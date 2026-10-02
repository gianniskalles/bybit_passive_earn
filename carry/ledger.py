"""carry/ledger.py — the carry ledger (CARRY_PLAN §6, Phase 5).

Append-only JSONL, one file per UTC day under <LOG_DIR>/ledger/. Every row
has a type:
  order          one order of execute.py: fill, average price, reference
                 price and slippage, fee (and its coin when Bybit says)
  funding        a settlement while short (paper: computed; live: the
                 transaction log); > 0 = received
  expected_funding  what the book's short should have received at that
                 settlement (rate x qty x mark) — R36's yardstick
  earn_interest  Easy Earn interest on the staked USDT (paper)
  round_trip     a position closed: basis + price PnL of both legs, fees
summary() adds them up over any window.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

DAY_MS = 86_400_000


def _file(log_dir: Path, ts_ms: int) -> Path:
    day = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    return Path(log_dir) / "ledger" / f"carry_ledger_{day}.jsonl"


def write(log_dir: Path, events: Iterable[Dict], now_ms: int, cycle_id: str) -> int:
    n = 0
    for e in events:
        row = {"cycle_id": cycle_id, "logged_ms": now_ms, **e}
        row.setdefault("ts_ms", now_ms)
        path = _file(log_dir, row["ts_ms"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        n += 1
    return n


def read(log_dir: Path, since_ms: int = 0, until_ms: Optional[int] = None) -> List[Dict]:
    rows: List[Dict] = []
    for path in sorted((Path(log_dir) / "ledger").glob("carry_ledger_*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("ts_ms", 0)
            if ts >= since_ms and (until_ms is None or ts <= until_ms):
                rows.append(r)
    return sorted(rows, key=lambda r: r.get("ts_ms", 0))


def order_events(orders: Iterable[Mapping]) -> List[Dict]:
    """execute.py's order records as ledger rows (filled or not: an unknown
    or rejected order is part of the record too)."""
    out = []
    for o in orders:
        req = o.get("request") or {}
        out.append({"type": "order", "ts_ms": o.get("ts_ms"), "symbol": o.get("symbol"),
                    "action": o.get("action"), "leg": o.get("leg"),
                    "category": req.get("category"), "side": req.get("side"),
                    "order_type": req.get("orderType"), "order_link_id": req.get("orderLinkId"),
                    "qty": req.get("qty"), "filled": o.get("filled"),
                    "avg_price": o.get("avg_price"), "ref_price": o.get("ref_price"),
                    "slippage_bps": o.get("slippage_bps"), "fee": o.get("fee"),
                    "fee_detail": o.get("fee_detail"), "outcome": o.get("outcome"),
                    "error": o.get("error")})
    return out


def fee_usdt(row: Mapping) -> float:
    """An order's fee in USDT: a fee taken in the coin is valued at the fill."""
    fee = float(row.get("fee") or 0.0)
    detail = row.get("fee_detail")
    if isinstance(detail, dict) and detail and "USDT" not in detail:
        return fee * float(row.get("avg_price") or 0.0)
    return fee


def summary(rows: Iterable[Mapping]) -> Dict[str, float]:
    s = {"funding": 0.0, "expected_funding": 0.0, "earn_interest": 0.0, "fees": 0.0,
         "basis_pnl": 0.0, "orders": 0, "slippage_bps_max": 0.0}
    for r in rows:
        t = r.get("type")
        if t in ("funding", "expected_funding", "earn_interest"):
            s[t] += float(r.get("amount") or 0.0)
        elif t == "order" and float(r.get("filled") or 0.0) > 0:
            s["orders"] += 1
            s["fees"] += fee_usdt(r)
            slip = r.get("slippage_bps")
            if slip is not None:
                s["slippage_bps_max"] = max(s["slippage_bps_max"], float(slip))
        elif t == "round_trip":
            s["basis_pnl"] += float(r.get("pnl") or 0.0)
    s["net"] = s["funding"] + s["earn_interest"] + s["basis_pnl"] - s["fees"]
    return s


def round_trip(symbol: str, rows: Iterable[Mapping], closed_ms: int) -> Optional[Dict]:
    """The PnL of the legs of the position just closed: every filled order of
    the symbol since its last ENTER, as signed cash flows (spot buy -, spot
    sell +, perp sell +, perp buy -). Delta-neutral, so this is the basis
    and execution PnL; fees are reported apart."""
    orders = [r for r in rows if r.get("type") == "order" and r.get("symbol") == symbol
              and float(r.get("filled") or 0.0) > 0]
    starts = [i for i, r in enumerate(orders) if r.get("action") == "ENTER"
              and r.get("leg") == "perp"]
    if not starts:
        return None
    legs = orders[starts[-1]:]
    cash = 0.0
    for r in legs:
        v = float(r["filled"]) * float(r.get("avg_price") or 0.0)
        cash += v if r.get("side") == "Sell" else -v
    return {"type": "round_trip", "symbol": symbol, "ts_ms": closed_ms, "pnl": cash,
            "fees": sum(fee_usdt(r) for r in legs), "orders": len(legs)}
