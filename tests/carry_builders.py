"""Builders for carry snapshots with exact control (Phase 3 tests)."""

from __future__ import annotations

from types import MappingProxyType
from typing import Dict, Optional, Sequence

import yaml

from carry.snapshot import (Account, CoinBalance, Collateral, Earn, EarnOrder, Instrument, Market,
                            OpenOrder, PerpPosition, Snapshot)
from pathlib import Path

NOW = 1_790_000_000_000
E8 = 8 * 3_600_000
MIN = 60_000
REPO = Path(__file__).resolve().parent.parent


def cfg(**over) -> Dict:
    c = yaml.safe_load((REPO / "config" / "carry.yaml").read_text())
    c.update(DEADMAN_URL="https://hc-ping.com/x")
    c.update(over)
    return c


def market(symbol="ETHUSDT", rate=0.0003, n=12, minutes_to_settlement=120, price=2500.0,
           interval=480, perp_status="Trading", spot_status="Trading", basis_bps=0.0,
           now=NOW, settled: Optional[Sequence[float]] = None) -> Market:
    nft = now + minutes_to_settlement * MIN
    last = nft - interval * MIN
    rates = list(settled) if settled is not None else [rate] * n
    hist = tuple((last - (len(rates) - 1 - i) * interval * MIN, r) for i, r in enumerate(rates))
    perp_mid = price * (1 + basis_bps / 1e4)
    eth = symbol.startswith("ETH")
    perp = Instrument("linear", symbol, perp_status, 0.01 if eth else 0.001,
                      0.01 if eth else 0.001, 0.01, 5.0, interval)
    spot = Instrument("spot", symbol, spot_status, 0.00001 if eth else 0.000001,
                      0.00062 if eth else 0.000048, 0.01, 1.0, None)
    return Market(symbol, perp, spot, nft, rates[-1] if settled is not None else rate,
                  perp_mid, price, perp_mid - 0.01, perp_mid + 0.01, price - 0.01, price + 0.01, hist)


def account(usdt=100.0, coins: Optional[Dict[str, float]] = None, mm_rate=0.02, margin_mode="REGULAR_MARGIN",
            borrow=0.0, collateral_active=True, collateral_ratio=0.95, symbols=("ETHUSDT", "BTCUSDT")) -> Account:
    c = {"USDT": CoinBalance(usdt, usdt, borrow)}
    for k, v in (coins or {}).items():
        c[k] = CoinBalance(v, v, 0.0)
    fees = {}
    for s in symbols:
        fees[("linear", s)] = (0.00055, 0.0002)
        fees[("spot", s)] = (0.001, 0.001)
    coll = {s[:-4]: Collateral(collateral_ratio, True, collateral_active) for s in symbols}
    return Account(margin_mode, mm_rate, mm_rate * 2, MappingProxyType(c), MappingProxyType(fees),
                   MappingProxyType(coll))


def earn(staked=500.0, orders=(), apr=0.0173, min_stake=1.0) -> Earn:
    return Earn("1", "Available", apr, 0.0, staked, tuple(orders), min_stake)


def redeem_order(link, status="Pending", value=90.0) -> EarnOrder:
    return EarnOrder("o-" + link, link, "Redeem", status, value, NOW - 60 * MIN)


def short(symbol="ETHUSDT", size=0.0, adl=1) -> PerpPosition:
    if size == 0:
        return PerpPosition(symbol, "", 0.0, None, None, 0)
    return PerpPosition(symbol, "Sell", size, 2500.0, 5000.0, adl)


def snapshot(markets=None, positions=None, acct="default", earn_=None, open_orders=(),
             errors=None, region=False, stale=None, now=NOW) -> Snapshot:
    markets = {"ETHUSDT": market(now=now)} if markets is None else markets
    positions = {s: short(s) for s in markets} if positions is None else positions
    acct = account() if acct == "default" else acct
    earn_ = earn() if earn_ is None else earn_
    return Snapshot(now, False, tuple(markets), MappingProxyType(dict(markets)),
                    MappingProxyType(dict(positions)), acct,
                    None if open_orders is None else tuple(open_orders),
                    earn_ if earn_ != "none" else None, MappingProxyType(dict(errors or {})), region,
                    MappingProxyType(dict(stale or {})))


def foreign_order(symbol="ETHUSDT") -> OpenOrder:
    return OpenOrder("spot", symbol, "x1", "", "Buy", 0.1, "New")
