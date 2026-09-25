"""
executor.py — dry-run vs live execution for Bybit Earn.

The cycle runner (run_yield_cycle.py) builds a plan of orders — every amount
already computed by the wrapper, never by the LLM — and hands it here.  The
split exists for audit: every order is recorded with identical fields
whether DRY_RUN is true or false; only the actual HTTP POST differs.

DRY_RUN=true:  we record what we *would* send. The tool is not called.
DRY_RUN=false: we POST through bybit_earn_tool. The response is recorded.

Defence in depth: a STAKE is refused unless the executor was built with
allow_new_positions=True (i.e. the effective risk state is NORMAL), even if
an upstream gate failed to drop it.

Order shape (from run_yield_cycle.build_plan):
  {"action": "STAKE" | "REDEEM", "coin": str, "product_id": str,
   "amount": str (decimal), "origin": "agent" | "wrapper", "reason": str}
"""

from datetime import datetime, timezone
from typing import Dict, List


class Executor:
    def __init__(self, bybit_tool=None, dry_run: bool = True,
                 allow_new_positions: bool = False):
        self.tool = bybit_tool
        self.dry_run = dry_run
        self.allow_new_positions = allow_new_positions

    def execute(self, orders: List[Dict]) -> List[Dict]:
        return [self._execute_one(o) for o in orders]

    def _execute_one(self, order: Dict) -> Dict:
        action = order.get("action")
        product_id = order.get("product_id")
        amount = order.get("amount")
        rec = {"ts": _now(), "action": action, "coin": order.get("coin"),
               "product_id": product_id, "amount": amount,
               "origin": order.get("origin"), "decision_reason": order.get("reason")}

        if action == "STAKE":
            path, method = "/v5/earn/subscribe", "subscribe_earn_product"
        elif action == "REDEEM":
            path, method = "/v5/earn/redeem", "redeem_earn_product"
        else:
            return {**rec, "would_call": None, "executed": False,
                    "reason": f"unknown action {action!r}; not handled"}

        if action == "STAKE" and not self.allow_new_positions:
            return {**rec, "would_call": None, "executed": False,
                    "reason": "RISK_GATE: new positions not allowed in this risk state"}

        would = {"method": "POST", "path": path,
                 "params": {"productId": product_id, "amount": amount}}
        rec["would_call"] = would
        if self.dry_run or self.tool is None:
            return {**rec, "executed": False,
                    "reason": "DRY_RUN" if self.dry_run else "no tool wired"}
        try:
            resp = getattr(self.tool, method)(product_id, amount)
            return {**rec, "executed": True, "response": resp}
        except Exception as e:
            return {**rec, "executed": False, "error": str(e)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
