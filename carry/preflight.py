"""carry/preflight.py — which symbols can hold a position at all
(decision 13.11, R21, R22).

The minimum neutral position of a symbol, from instruments-info:
  perp  qty >= minOrderQty, on qtyStep, qty * mark >= minNotionalValue
  spot  the fee is taken from the coin bought (R22), so the spot buy is
        perp_qty / (1 - spot fee) rounded UP to basePrecision, and must meet
        the spot minOrderQty and minOrderAmt
The capital it needs is the spot buy at the ask. A symbol whose minimum does
not fit its notional limit (MAX_NOTIONAL_PER_SYMBOL_USD, or
MAX_NOTIONAL_PER_ALT_USD for an altcoin) is left out of the cycle with a
SYMBOL_BELOW_MIN_SIZE alert. So is a symbol whose market is unreadable, stale
or not Trading on both legs (no new exposure; exits never consult this).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import List, Mapping, Tuple

from carry.config import notional_cap
from carry.snapshot import Market, Snapshot

MAX_STEPS = 10_000


@dataclass(frozen=True)
class MinPosition:
    symbol: str
    perp_qty: float
    spot_buy_qty: float
    notional_usd: float


def _ceil_to(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


def min_position(m: Market, spot_fee: float) -> MinPosition:
    d = lambda v: Decimal(str(v))  # noqa: E731
    perp_step, spot_step = d(m.perp.qty_step), d(m.spot.qty_step)
    mark, ask, keep = d(m.mark_price), d(m.spot_ask), 1 - d(spot_fee)
    need = max(d(m.perp.min_qty), d(m.spot.min_qty) * keep)
    if m.perp.min_notional:
        need = max(need, d(m.perp.min_notional) / mark)
    if m.spot.min_notional:
        need = max(need, d(m.spot.min_notional) * keep / ask)
    qty = _ceil_to(need, perp_step)
    for _ in range(MAX_STEPS):
        spot = _ceil_to(qty / keep, spot_step)
        if spot >= d(m.spot.min_qty) and (not m.spot.min_notional or spot * ask >= d(m.spot.min_notional)):
            return MinPosition(m.symbol, float(qty), float(spot), float(spot * ask))
        qty += perp_step
    raise ValueError(f"{m.symbol}: no minimum position within {MAX_STEPS} steps")


def spot_fee(snap: Snapshot, symbol: str, cfg: Mapping) -> float:
    """The account's spot taker fee when read, else the config fallback."""
    if snap.account is not None and ("spot", symbol) in snap.account.fees:
        return snap.account.fees[("spot", symbol)][0]
    return float(cfg["SPOT_TAKER_FEE"])


def tradable_symbols(snap: Snapshot, cfg: Mapping) -> Tuple[List[str], List[str]]:
    fit: List[str] = []
    alerts: List[str] = []
    for sym in cfg["SYMBOLS"]:
        m = snap.markets.get(sym)
        if m is None or f"market:{sym}" in snap.stale:
            alerts.append(f"SYMBOL_EXCLUDED: {sym} market unreadable or stale")
            continue
        if not (m.perp.trading and m.spot.trading):
            alerts.append(f"SYMBOL_EXCLUDED: {sym} not Trading "
                          f"(perp {m.perp.status}, spot {m.spot.status})")
            continue
        mp = min_position(m, spot_fee(snap, sym, cfg))
        limit = notional_cap(cfg, sym)
        if mp.notional_usd > limit:
            alerts.append(f"SYMBOL_BELOW_MIN_SIZE: {sym} minimum position "
                          f"{mp.notional_usd:.2f} USD (perp {mp.perp_qty}, spot {mp.spot_buy_qty}) "
                          f"> limit {limit} USD")
            continue
        fit.append(sym)
    return fit, alerts
