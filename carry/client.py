"""carry/client.py — Bybit v5 for the carry strategy (CARRY_PLAN §6, Phase 2).

Built on the same fail-closed transport as bybit_earn_tool: every failure
(HTTP, timeout, non-JSON, retCode != 0, a missing list, an unparseable row,
a pagination that does not end) raises BybitAPIError. Nothing becomes an
empty list: "no positions" and "could not read positions" never look the
same. An empty list from a SUCCESSFUL call is a real answer.

Phase 2 is read-only for trading: market, account, position, order and
execution reads. The only write remains the Earn place-order inherited from
BybitEarnTool (Stake/Redeem, built by place_order_request). Trading orders
(POST /v5/order/create) arrive with execute.py in Phase 4.
"""

from __future__ import annotations

import urllib.parse
from typing import Dict, List, Optional, Tuple

from bybit_earn_tool import BybitAPIError, BybitEarnTool

FUNDING_PAGE = 200
MAX_PAGES = 20                 # a cursor that does not end is a failure, not "all rows"
PAGE_LIMIT = 50

# Our orderLinkIds start with this; anything else on the account is foreign
# (R2 FOREIGN_ACTIVITY).
LINK_PREFIX = "cy-"

# R1: how Bybit refuses a region-restricted account is UNVERIFIED (§12). Both
# signals only make the system more conservative (NO_NEW_POSITIONS); the first
# real refusal gets captured and this is locked on it.
REGION_RET_CODES = frozenset({10024})              # "compliance rules triggered"
REGION_BODY_MARKERS = ("from your country", "restricted region", "not available in your region")


def is_region_restricted(err: BaseException) -> bool:
    if not isinstance(err, BybitAPIError):
        return False
    if err.ret_code in REGION_RET_CODES:
        return True
    body = str(err.body or "").lower()
    return any(m in body for m in REGION_BODY_MARKERS)


# Parameters that change on every call; recordings and replays ignore them.
_VOLATILE = frozenset({"endTime", "startTime", "cursor", "limit"})


def response_key(url: str) -> str:
    """Key of a recorded response: path + sorted stable query parameters.
    Shared by scripts/testnet.py (recording) and tests/replay.py (replay)."""
    parsed = urllib.parse.urlparse(url)
    params = sorted((k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
                    if k not in _VOLATILE)
    return parsed.path + ("?" + urllib.parse.urlencode(params) if params else "")


class CarryPublicClient(BybitEarnTool):
    """Public market data only (no key needed)."""

    def __init__(self, session=None, testnet: Optional[bool] = None,
                 api_key: Optional[str] = None, api_secret: Optional[str] = None):
        super().__init__(api_key=api_key, api_secret=api_secret, session=session, testnet=testnet)

    def _one(self, endpoint: str, params: Dict, what: str, signed: bool = False) -> Dict:
        lst = self._list(self._request("GET", endpoint, params, signed=signed), endpoint, "list")
        if not lst or not isinstance(lst[0], dict):
            raise BybitAPIError(f"{endpoint}: no {what}")
        return lst[0]

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
            for ts, rate in self._funding_rows(page, endpoint):
                if start_ms <= ts <= end_ms:
                    rows[ts] = rate
                oldest = ts if oldest is None else min(oldest, ts)
            if oldest is None or oldest <= start_ms or len(page) < FUNDING_PAGE:
                break
            cursor = oldest - 1
        return sorted(rows.items())

    def get_recent_funding(self, symbol: str, count: int) -> List[Tuple[int, float]]:
        """The last `count` settled rates, oldest first (one page, no endTime)."""
        endpoint = "/v5/market/funding/history"
        if not 1 <= count <= FUNDING_PAGE:
            raise ValueError(f"count must be in 1..{FUNDING_PAGE}")
        page = self._list(self._request("GET", endpoint, {"category": "linear", "symbol": symbol,
                                                          "limit": count}), endpoint, "list")
        return sorted(self._funding_rows(page, endpoint))[-count:]

    @staticmethod
    def _funding_rows(page, endpoint):
        out = []
        for r in page:
            try:
                out.append((int(r["fundingRateTimestamp"]), float(r["fundingRate"])))
            except (KeyError, TypeError, ValueError) as e:
                raise BybitAPIError(f"{endpoint}: unparseable row {r!r}") from e
        return out

    def get_instrument(self, category: str, symbol: str) -> Dict:
        return self._one("/v5/market/instruments-info", {"category": category, "symbol": symbol},
                         f"{category} instrument {symbol}")

    def get_ticker(self, category: str, symbol: str) -> Dict:
        return self._one("/v5/market/tickers", {"category": category, "symbol": symbol},
                         f"{category} ticker {symbol}")

    def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> Dict:
        endpoint = "/v5/market/orderbook"
        result = self._request("GET", endpoint, {"category": category, "symbol": symbol,
                                                 "limit": limit})
        if not (isinstance(result.get("b"), list) and isinstance(result.get("a"), list)):
            raise BybitAPIError(f"{endpoint}: response has no b/a lists")
        return result

    def get_usdt_flexible_apr_history(self) -> Tuple[str, List[Dict]]:
        """(productId, raw APR history) of the USDT FlexibleSaving product —
        the Easy Earn rate of layer A."""
        products = [p for p in self.get_earn_products(coin="USDT") if p.get("coin") == "USDT"]
        if not products:
            raise BybitAPIError("no USDT FlexibleSaving product")
        pid = str(products[0]["productId"])
        return pid, self.get_earn_apr_history(product_id=pid)


class CarryClient(CarryPublicClient):
    """Public + private (signed) reads. Needs BYBIT_API_KEY/SECRET."""

    def _paged(self, endpoint: str, params: Dict) -> List[Dict]:
        rows: List[Dict] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            p = dict(params, limit=PAGE_LIMIT)
            if cursor:
                p["cursor"] = cursor
            result = self._request("GET", endpoint, p, signed=True)
            rows.extend(self._list(result, endpoint, "list"))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                return rows
        raise BybitAPIError(f"{endpoint}: more than {MAX_PAGES} pages; refusing a partial list")

    # ---- account --------------------------------------------------------------

    def get_account_info(self) -> Dict:
        return self._request("GET", "/v5/account/info", {}, signed=True)

    def get_fee_rate(self, category: str, symbol: str) -> Dict:
        return self._one("/v5/account/fee-rate", {"category": category, "symbol": symbol},
                         f"fee rate for {category} {symbol}", signed=True)

    def get_collateral_info(self, currency: str) -> Dict:
        return self._one("/v5/account/collateral-info", {"currency": currency},
                         f"collateral info for {currency}", signed=True)

    def get_transaction_log(self, start_ms: int, end_ms: int, currency: str = "USDT",
                            type_: Optional[str] = None) -> List[Dict]:
        params = {"accountType": "UNIFIED", "currency": currency, "startTime": start_ms,
                  "endTime": end_ms}
        if type_:
            params["type"] = type_
        return self._paged("/v5/account/transaction-log", params)

    # ---- positions, orders, executions -----------------------------------------

    def get_positions(self, symbol: str) -> List[Dict]:
        return self._paged("/v5/position/list", {"category": "linear", "symbol": symbol})

    def get_open_orders(self, category: str) -> List[Dict]:
        params = {"category": category}
        if category == "linear":
            params["settleCoin"] = "USDT"
        return self._paged("/v5/order/realtime", params)

    def find_order(self, category: str, order_link_id: str) -> Optional[Dict]:
        """R23: look an order up by orderLinkId BEFORE any retry. Open orders
        first, then history. None only when BOTH successful reads say it does
        not exist; any read failure raises."""
        for endpoint in ("/v5/order/realtime", "/v5/order/history"):
            lst = self._list(self._request("GET", endpoint, {"category": category,
                                                             "orderLinkId": order_link_id},
                                           signed=True), endpoint, "list")
            for o in lst:
                if o.get("orderLinkId") == order_link_id:
                    return o
        return None

    def get_executions(self, category: str, symbol: str, start_ms: int) -> List[Dict]:
        return self._paged("/v5/execution/list", {"category": category, "symbol": symbol,
                                                  "startTime": start_ms})
