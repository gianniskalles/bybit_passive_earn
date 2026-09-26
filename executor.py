"""
executor.py — dry-run vs live execution for Bybit Earn.

The cycle runner (run_yield_cycle.py) builds a plan of orders — every amount
already computed by the wrapper, never by the LLM — and hands it here.

`would_call` is the exact request, built by
bybit_earn_tool.place_order_request() in both modes:
  DRY_RUN=true:  recorded, not sent.
  DRY_RUN=false: sent verbatim via BybitEarnTool.place_order(); the
                 response (orderId, orderLinkId) is recorded.

A successful response means Bybit ACCEPTED the order, not that it is done:
Earn orders are asynchronous (a redemption can take up to 48 h). The
wrapper tracks them via GET /v5/earn/order on the following cycles.

Defence in depth: a STAKE is refused unless the executor was built with
allow_new_positions=True, even if an upstream gate failed to drop it.

Order shape (from run_yield_cycle.build_plan):
  {"action": "STAKE" | "REDEEM", "coin": str, "product_id": str,
   "amount": str (decimal), "origin": "agent" | "wrapper", "reason": str}
"""

from datetime import datetime, timezone
from typing import Dict, List

from bybit_earn_tool import order_link_id, place_order_request

ORDER_TYPES = {"STAKE": "Stake", "REDEEM": "Redeem"}


class Executor:
    def __init__(self, bybit_tool=None, dry_run: bool = True,
                 allow_new_positions: bool = False, account_type: str = "UNIFIED",
                 cycle_id: str = "nocycle"):
        self.tool = bybit_tool
        self.dry_run = dry_run
        self.allow_new_positions = allow_new_positions
        self.account_type = account_type
        self.cycle_id = cycle_id

    def execute(self, orders: List[Dict]) -> List[Dict]:
        return [self._execute_one(o) for o in orders]

    def _execute_one(self, order: Dict) -> Dict:
        action = order.get("action")
        product_id = order.get("product_id")
        rec = {"ts": _now(), "action": action, "coin": order.get("coin"),
               "product_id": product_id, "amount": order.get("amount"),
               "origin": order.get("origin"), "decision_reason": order.get("reason"),
               "would_call": None, "order_link_id": None}

        order_type = ORDER_TYPES.get(action)
        if order_type is None:
            return {**rec, "executed": False, "reason": f"unknown action {action!r}; not handled"}
        if action == "STAKE" and not self.allow_new_positions:
            return {**rec, "executed": False,
                    "reason": "RISK_GATE: new positions not allowed in this cycle"}

        link = order_link_id(self.cycle_id, order_type, str(product_id))
        request = place_order_request(order_type, self.account_type, order.get("coin"),
                                      str(product_id), order.get("amount"), link)
        rec.update(would_call=request, order_link_id=link)
        if self.dry_run or self.tool is None:
            return {**rec, "executed": False,
                    "reason": "DRY_RUN" if self.dry_run else "no tool wired"}
        try:
            resp = self.tool.place_order(request)
            return {**rec, "executed": True, "response": resp,
                    "reason": "accepted by Bybit (asynchronous; tracked via /v5/earn/order)"}
        except Exception as e:
            return {**rec, "executed": False, "error": str(e), "reason": "place_order failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
