"""A Bybit stand-in for carry/execute.py tests (Phase 4). No network.

Models what execution depends on: one-way linear position (our short),
the spot coin the orders move, IOC limits against a static book, reduceOnly
(never more than the position), the spot buy fee taken in the base coin
(R22), Bybit's duplicate-orderLinkId rejection (R24), and a fake clock.

Faults are scripted per call kind and consumed in order; anything not
scripted succeeds:
  create kinds  "linear:Sell", "linear:Buy", "spot:Buy", "spot:Sell"
                 reject         answered retCode != 0, nothing executed
                 lost           timeout, the order never reached Bybit
                 timeout        timeout, but the order WAS executed
                 ("partial", f) executes only a fraction f
  "find", "find:spot", "find:linear"
                 error          the lookup read fails
                 hidden         a successful read that does not show it yet
  "book:spot", "book:linear"   error   the orderbook read fails
  "earn"         reject | timeout
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Dict, List, Optional

from bybit_earn_tool import BybitAPIError

TERMINAL_FILLED = "Filled"


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t
        self.slept = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self.slept += s


class FakeExchange:
    live = False

    def __init__(self, clock: FakeClock, base: str = "ETH", short: float = 0.0,
                 spot: float = 0.0, spot_fee: float = 0.001, perp_bid: float = 2500.0,
                 perp_ask: float = 2500.5, spot_bid: float = 2499.8, spot_ask: float = 2500.2,
                 depth: float = 100.0, fee_detail: bool = True, latency: float = 0.2):
        self.clock = clock
        self.base = base
        # quantity steps of tests/carry_builders.market()
        self.steps = ({"linear": 0.01, "spot": 0.00001} if base == "ETH"
                      else {"linear": 0.001, "spot": 0.000001})
        self.short = short                 # our linear short (base coin)
        self.long = 0.0                    # a long perp, never ours
        self.spot = spot                   # spot coin in the wallet
        self.spot_fee = spot_fee
        self.books = {"linear": ([[perp_bid, depth]], [[perp_ask, depth]]),
                      "spot": ([[spot_bid, depth]], [[spot_ask, depth]])}
        self.fee_detail = fee_detail
        self.latency = latency
        self.orders: Dict[str, Dict] = {}
        self.calls: List[tuple] = []
        self.faults: Dict[str, List] = {}
        self.earn_orders: Dict[str, Dict] = {}
        self._n = 0

    # ---- scripting -----------------------------------------------------------------
    def script(self, kind: str, *faults) -> "FakeExchange":
        self.faults.setdefault(kind, []).extend(faults)
        return self

    def _fault(self, kind: str):
        q = self.faults.get(kind)
        return q.pop(0) if q else None

    # ---- orders --------------------------------------------------------------------
    def create_order(self, request: Dict) -> Dict:
        b = request["body"]
        kind = f"{b['category']}:{b['side']}"
        self.calls.append(("create", kind, b["orderLinkId"], dict(b)))
        self.clock.t += self.latency
        if b["orderLinkId"] in self.orders:
            raise BybitAPIError("OrderLinkedID is duplicate", ret_code=110072)
        fault = self._fault(kind)
        if fault == "reject":
            raise BybitAPIError("rejected", ret_code=110007)
        if fault == "lost":
            raise BybitAPIError(f"{kind}: HTTP error: timeout")
        frac = fault[1] if isinstance(fault, tuple) and fault[0] == "partial" else 1.0
        if b.get("reduceOnly") and (self.short if b["side"] == "Buy" else self.long) <= 0:
            raise BybitAPIError("current position is zero, cannot fix reduce-only order qty",
                                ret_code=110017)
        self._execute(b, frac)
        if fault == "timeout":
            raise BybitAPIError(f"{kind}: HTTP error: timeout")
        return {"orderId": self.orders[b["orderLinkId"]]["orderId"],
                "orderLinkId": b["orderLinkId"]}

    def _execute(self, b: Dict, frac: float) -> None:
        cat, side, qty = b["category"], b["side"], float(b["qty"])
        bids, asks = self.books[cat]
        # Bybit fills on the instrument's quantity grid: a partial fill is a
        # whole number of steps.
        step = Decimal(str(self.steps[cat]))
        want = float((Decimal(b["qty"]) * Decimal(str(frac)) / step).to_integral_value(
            rounding=ROUND_FLOOR) * step)
        if b["orderType"] == "Limit":
            price = float(b["price"])
            if side == "Sell" and price > bids[0][0]:
                want = 0.0
            if side == "Buy" and price < asks[0][0]:
                want = 0.0
        fee = 0.0
        if cat == "linear":
            if b.get("reduceOnly"):
                if side == "Buy":
                    fill = min(want, self.short)
                    self.short = round(self.short - fill, 12)
                else:
                    fill = min(want, self.long)
                    self.long = round(self.long - fill, 12)
            elif side == "Sell":
                fill = want
                self.short = round(self.short + fill, 12)
            else:
                fill = want
                self.long = round(self.long + fill, 12)
        else:
            if side == "Buy":
                fill = want
                fee = round(fill * self.spot_fee, 12)
                self.spot = round(self.spot + fill - fee, 12)
            else:
                fill = min(want, self.spot)
                self.spot = round(self.spot - fill, 12)
        fill = round(fill, 12)
        status = TERMINAL_FILLED if fill >= qty - 1e-12 else (
            "PartiallyFilledCanceled" if fill > 0 else "Cancelled")
        self._n += 1
        order = {"orderId": f"o{self._n}", "orderLinkId": b["orderLinkId"], "symbol": b["symbol"],
                 "side": side, "orderType": b["orderType"], "orderStatus": status,
                 "qty": b["qty"], "cumExecQty": repr(fill), "cumExecFee": repr(fee),
                 "avgPrice": repr(bids[0][0] if side == "Sell" else asks[0][0]) if fill else "0"}
        if cat == "spot" and self.fee_detail and fee:
            order["cumFeeDetail"] = {self.base: repr(fee)}
        self.orders[b["orderLinkId"]] = order

    def find_order(self, category: str, order_link_id: str) -> Optional[Dict]:
        self.calls.append(("find", category, order_link_id))
        self.clock.t += self.latency
        fault = self._fault(f"find:{category}") or self._fault("find")
        if fault == "error":
            raise BybitAPIError("/v5/order/realtime: HTTP error: 502")
        if fault == "hidden":
            return None
        return self.orders.get(order_link_id)

    def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> Dict:
        self.calls.append(("book", category, symbol))
        self.clock.t += self.latency
        if self._fault(f"book:{category}") == "error":
            raise BybitAPIError("/v5/market/orderbook: HTTP error: 502")
        bids, asks = self.books[category]
        return {"s": symbol, "b": [[repr(p), repr(q)] for p, q in bids],
                "a": [[repr(p), repr(q)] for p, q in asks]}

    # ---- Earn ----------------------------------------------------------------------
    def get_earn_orders(self, order_link_id: str) -> List[Dict]:
        self.calls.append(("earn_find", order_link_id))
        return [self.earn_orders[order_link_id]] if order_link_id in self.earn_orders else []

    def place_earn(self, request: Dict) -> Dict:
        b = request["body"]
        self.calls.append(("earn", b["orderType"], b["orderLinkId"], dict(b)))
        fault = self._fault("earn")
        if fault == "reject":
            raise BybitAPIError("rejected", ret_code=180001)
        self.earn_orders[b["orderLinkId"]] = {"orderLinkId": b["orderLinkId"],
                                              "orderType": b["orderType"], "status": "Pending"}
        if fault == "timeout":
            raise BybitAPIError("earn: HTTP error: timeout")
        return {"orderId": "e1", "orderLinkId": b["orderLinkId"]}

    # ---- assertions helpers ----------------------------------------------------------
    def created(self, kind: Optional[str] = None) -> List[Dict]:
        return [c[3] for c in self.calls if c[0] == "create" and (kind is None or c[1] == kind)]
