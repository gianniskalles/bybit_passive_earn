"""
executor.py — dry-run vs live execution for Bybit Earn.

The cycle runner (run_yield_cycle.py) calls into here after the agent decides.
The split exists for one reason: audit. Every would-call is recorded with
identical fields whether DRY_RUN is true or false; only the actual HTTP POST
differs. That way the same JSONL log can be diffed across the DRY_RUN → LIVE
transition without re-deriving intent.

DRY_RUN=true:  we record what we *would* send. The tool is not called.
DRY_RUN=false: we POST through bybit_earn_tool. The response is recorded.
"""

import os
import sys
import time
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any


class Executor:
    def __init__(self, bybit_tool=None, dry_run: bool = True):
        self.tool = bybit_tool
        self.dry_run = dry_run

    # ---- plan execution ----
    def execute_plan(self, decisions: List[Dict], positions: List[Dict],
                     balance_usdt: float, min_move_usd: float) -> List[Dict]:
        """
        Walk the agent's decisions, build would-call / real-call records.
        Returns a list of execution records (one per would-be action).
        """
        records = []
        for d in decisions:
            action = d.get("action")
            coin = d.get("coin")
            product_id = d.get("product_id")
            amount = d.get("amount_usd")
            if amount is None:
                # v5: `amount` is reserved for held-position reporting. If a
                # STAKE/REDEEM decision arrives without amount_usd, treat
                # it as a schema error (validator should have caught it,
                # but belt-and-braces for direct executor calls).
                amount = d.get("amount", 0)
                if amount:
                    return [{
                        "ts": _now(), "action": action, "coin": coin,
                        "product_id": product_id, "executed": False,
                        "would_call": None,
                        "reason": "schema_error: decision used deprecated `amount` field; expected `amount_usd`",
                    }]
            from_pid = d.get("from_product_id")

            if action in ("HOLD", "ALERT_ONLY", "NO_NEW_POSITIONS"):
                # No trade. Record as would-skip for audit symmetry.
                records.append({
                    "ts": _now(),
                    "action": action,
                    "coin": coin,
                    "would_call": None,
                    "executed": False,
                    "reason": "decision is non-trading; nothing to do",
                })
                continue

            if action == "STAKE":
                if amount < min_move_usd:
                    records.append({
                        "ts": _now(),
                        "action": "STAKE",
                        "coin": coin,
                        "product_id": product_id,
                        "amount_usd": amount,
                        "would_call": {"method": "POST", "path": "/v5/earn/subscribe",
                                       "params": {"productId": product_id, "amount": str(amount)}},
                        "executed": False,
                        "reason": f"amount {amount} < MIN_MOVE_USD {min_move_usd}; would churn state for nothing",
                    })
                    continue
                if amount > balance_usdt:
                    records.append({
                        "ts": _now(),
                        "action": "STAKE",
                        "coin": coin,
                        "product_id": product_id,
                        "amount_usd": amount,
                        "would_call": {"method": "POST", "path": "/v5/earn/subscribe",
                                       "params": {"productId": product_id, "amount": str(amount)}},
                        "executed": False,
                        "reason": f"amount {amount} > available balance {balance_usdt}; skip",
                    })
                    continue
                rec = self._subscribe(product_id, amount)
                records.append(rec)
                continue

            if action == "REDEEM":
                if amount < min_move_usd:
                    records.append({
                        "ts": _now(),
                        "action": "REDEEM",
                        "coin": coin,
                        "product_id": product_id,
                        "from_product_id": from_pid,
                        "amount_usd": amount,
                        "would_call": {"method": "POST", "path": "/v5/earn/redeem",
                                       "params": {"productId": product_id, "amount": str(amount)}},
                        "executed": False,
                        "reason": f"amount {amount} < MIN_MOVE_USD {min_move_usd}; would churn state for nothing",
                    })
                    continue
                rec = self._redeem(product_id, amount)
                records.append(rec)
                continue

            if action == "REDEEM_ALL":
                rec = self._redeem_all(coin, positions)
                records.append(rec)
                continue

            # Unknown action: log and continue.
            records.append({
                "ts": _now(),
                "action": action,
                "would_call": None,
                "executed": False,
                "reason": f"unknown action {action!r}; not handled",
            })

        return records

    # ---- low-level ----
    def _subscribe(self, product_id: str, amount: float) -> Dict:
        would = {"method": "POST", "path": "/v5/earn/subscribe",
                 "params": {"productId": product_id, "amount": str(amount)}}
        if self.dry_run or self.tool is None:
            return {"ts": _now(), "action": "STAKE", "product_id": product_id,
                    "amount_usd": amount, "would_call": would, "executed": False,
                    "reason": "DRY_RUN" if self.dry_run else "no tool wired"}
        try:
            resp = self.tool.subscribe_earn_product(product_id, str(amount))
            return {"ts": _now(), "action": "STAKE", "product_id": product_id,
                    "amount_usd": amount, "would_call": would, "executed": True,
                    "response": resp}
        except Exception as e:
            return {"ts": _now(), "action": "STAKE", "product_id": product_id,
                    "amount_usd": amount, "would_call": would, "executed": False,
                    "error": str(e)}

    def _redeem(self, product_id: str, amount: float) -> Dict:
        would = {"method": "POST", "path": "/v5/earn/redeem",
                 "params": {"productId": product_id, "amount": str(amount)}}
        if self.dry_run or self.tool is None:
            return {"ts": _now(), "action": "REDEEM", "product_id": product_id,
                    "amount_usd": amount, "would_call": would, "executed": False,
                    "reason": "DRY_RUN" if self.dry_run else "no tool wired"}
        try:
            resp = self.tool.redeem_earn_product(product_id, str(amount))
            return {"ts": _now(), "action": "REDEEM", "product_id": product_id,
                    "amount_usd": amount, "would_call": would, "executed": True,
                    "response": resp}
        except Exception as e:
            return {"ts": _now(), "action": "REDEEM", "product_id": product_id,
                    "amount_usd": amount, "would_call": would, "executed": False,
                    "error": str(e)}

    def _redeem_all(self, coin: Optional[str], positions: List[Dict]) -> Dict:
        # Find positions for this coin and build one record per product.
        # (REDEEM_ALL is a per-coin unwind; if there are multiple products we
        # record each one separately. The decision record keeps the umbrella
        # action; the execution log has the fine-grained calls.)
        if not positions:
            return {"ts": _now(), "action": "REDEEM_ALL", "coin": coin,
                    "would_call": None, "executed": False,
                    "reason": "no positions to redeem"}

        records = []
        for p in positions:
            if coin and p.get("coin") != coin:
                continue
            pid = p.get("productId")
            amt = p.get("amount", "0")
            records.append(self._redeem(pid, float(amt) if amt else 0))
        return {"ts": _now(), "action": "REDEEM_ALL", "coin": coin,
                "subrecords": records, "executed": any(r.get("executed") for r in records)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
