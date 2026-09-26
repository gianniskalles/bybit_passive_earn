"""carry/client.py — Bybit v5 for the carry strategy.

Phase 0B: PUBLIC market data only (no key needed). Built on the same
fail-closed transport as bybit_earn_tool: every failure raises
BybitAPIError, never an empty list. Account, position, order and execution
endpoints are added in Phase 2.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from bybit_earn_tool import BybitAPIError, BybitEarnTool

FUNDING_PAGE = 200


class CarryPublicClient(BybitEarnTool):
    def __init__(self, session=None, testnet: Optional[bool] = None):
        super().__init__(session=session, testnet=testnet)

    def get_funding_history(self, symbol: str, start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
        """Settled funding rates in [start_ms, end_ms], oldest first.

        GET /v5/market/funding/history returns at most 200 rows, newest
        first, up to endTime; we page backwards until start_ms."""
        endpoint = "/v5/market/funding/history"
        rows: Dict[int, float] = {}
        cursor = end_ms
        while True:
            result = self._request("GET", endpoint, {"category": "linear", "symbol": symbol,
                                                     "endTime": cursor, "limit": FUNDING_PAGE})
            page = self._list(result, endpoint, "list")
            if not page:
                break
            oldest = None
            for r in page:
                try:
                    ts, rate = int(r["fundingRateTimestamp"]), float(r["fundingRate"])
                except (KeyError, TypeError, ValueError) as e:
                    raise BybitAPIError(f"{endpoint}: unparseable row {r!r}") from e
                if start_ms <= ts <= end_ms:
                    rows[ts] = rate
                oldest = ts if oldest is None else min(oldest, ts)
            if oldest is None or oldest <= start_ms or len(page) < FUNDING_PAGE:
                break
            cursor = oldest - 1
        return sorted(rows.items())

    def get_instrument(self, category: str, symbol: str) -> Dict:
        endpoint = "/v5/market/instruments-info"
        lst = self._list(self._request("GET", endpoint, {"category": category, "symbol": symbol}),
                         endpoint, "list")
        if not lst:
            raise BybitAPIError(f"{endpoint}: no {category} instrument {symbol}")
        return lst[0]

    def get_usdt_flexible_apr_history(self) -> Tuple[str, List[Dict]]:
        """(productId, raw APR history) of the USDT FlexibleSaving product —
        the Easy Earn rate that layer A (BYUSDT) is based on."""
        products = [p for p in self.get_earn_products(coin="USDT") if p.get("coin") == "USDT"]
        if not products:
            raise BybitAPIError("no USDT FlexibleSaving product")
        pid = str(products[0]["productId"])
        return pid, self.get_earn_apr_history(product_id=pid)
