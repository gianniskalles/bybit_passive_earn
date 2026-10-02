"""Synthetic funding regimes for the carry replay tests (decision 13.6).

A regime is a list of (days, funding rate per 8h settlement). market_at(t)
builds the Market a real snapshot would show at time t: the settlements
before t on the 8-hour grid (00/08/16 UTC), the predicted rate of the next
one, a price path. Settlements, prices and the clock are all synthetic; the
cycle, plan, execution, paper account and ledger are the production ones.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Callable, List, Sequence, Tuple

from carry_builders import market, snapshot

H = 3_600_000
E8 = 8 * H
T0 = 1_790_006_400_000 - (1_790_006_400_000 % E8)        # a settlement instant


class Regime:
    def __init__(self, phases: Sequence[Tuple[float, float]], start_ms: int = T0,
                 price: Callable[[int], float] = lambda t: 2500.0, history: int = 12):
        self.start = start_ms
        self.phases = list(phases)
        self.price = price
        self.history = history
        self.end = start_ms + int(sum(d for d, _ in phases) * 24 * H)

    def rate(self, ts: int) -> float:
        t = self.start
        for days, r in self.phases:
            t += int(days * 24 * H)
            if ts < t:
                return r
        return self.phases[-1][1]

    def market_at(self, now: int, symbol: str = "ETHUSDT"):
        last = now - (now % E8)                                # last settlement at or before now
        if last == now:
            last -= E8
        nft = last + E8
        settled = [self.rate(last - i * E8) for i in reversed(range(self.history))]
        return market(symbol, rate=self.rate(nft), settled=settled, now=now,
                      minutes_to_settlement=(nft - now) // 60000, price=self.price(now))

    def snapshot_fn(self):
        def take(client, cfg, now_ms=None, private=True):
            m = self.market_at(now_ms)
            s = snapshot(markets={"ETHUSDT": m}, now=now_ms)
            errors = {f"positions:{x}": "not read" for x in s.symbols}
            errors.update(account="not read", orders="not read", earn="not read")
            return replace(s, positions=MappingProxyType({}), account=None, open_orders=None,
                           earn=None, errors=MappingProxyType(errors))
        return take

    def cycle_times(self, every_h: float = 2.0, offset_min: int = 30) -> List[int]:
        t = self.start + offset_min * 60_000
        out = []
        while t < self.end:
            out.append(t)
            t += int(every_h * H)
        return out


class FakeClient:
    """What the cycle reads besides the snapshot: layer A's public APR."""
    testnet = False

    def __init__(self, apr: str = "1.73%"):
        self.apr = apr

    def get_usdt_flexible_apr_history(self):
        return "1", [{"timestamp": "1", "apr": self.apr}]
