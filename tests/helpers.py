"""Test doubles and helpers shared by the wrapper / heartbeat tests.

FakeBybit stands in for BybitEarnTool; FakeAgent stands in for `hermes chat`.
Neither touches the network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import yaml

import risk_state

KEY = "test-secret-key-123"
NOW_MS = lambda: int(time.time() * 1000)  # noqa: E731


def product(pid="1", coin="USDT", apr="1.2%", status="Available", min_stake="1",
            max_stake="100000", remaining="-1", precision="8", redeem_minutes="0",
            **extra) -> Dict:
    p = {"category": "FlexibleSaving", "productId": pid, "coin": coin,
         "estimateApr": apr, "status": status, "minStakeAmount": min_stake,
         "maxStakeAmount": max_stake, "remainingPoolAmount": remaining,
         "precision": precision, "hasTieredApr": False,
         "redeemProcessingMinute": redeem_minutes}
    p.update(extra)
    return {k: v for k, v in p.items() if v is not None}


def apr_history(apr="1.1%", points=24, newest_age_s=600, step_s=3600) -> List[Dict]:
    now = NOW_MS()
    return [{"timestamp": str(now - (newest_age_s + i * step_s) * 1000), "apr": apr}
            for i in range(points)]


class FakeBybit:
    """Stand-in for BybitEarnTool.

    `fail` names methods that raise BybitAPIError (as the real tool does on
    retCode != 0 or an HTTP error). `orders` is what get_earn_orders returns;
    place_order appends a Pending order to it, like Bybit would.
    """

    def __init__(self, products=None, positions=None, wallet="100", history=None,
                 orders=None, fail=()):
        self.products = [product()] if products is None else products
        self.positions = positions or []
        self.wallet = wallet
        self.history = history
        self.orders = list(orders or [])
        self.fail = set(fail)
        self.calls: List[tuple] = []
        self.placed: List[Dict] = []

    def _maybe_fail(self, name):
        if name in self.fail:
            from bybit_earn_tool import BybitAPIError
            raise BybitAPIError(f"{name}: retCode=10002 retMsg=simulated failure")

    def get_earn_products(self):
        self._maybe_fail("get_earn_products")
        return list(self.products)

    def get_earn_apr_history(self, coin=None, product_id=None, **kw):
        self._maybe_fail("get_earn_apr_history")
        return list(self.history) if self.history is not None else apr_history()

    def get_wallet_balance(self, account_type="UNIFIED"):
        self._maybe_fail("get_wallet_balance")
        return {"list": [{"coin": [{"coin": "USDT", "walletBalance": self.wallet,
                                    "equity": self.wallet}]}]}

    def get_earn_positions(self, coin=None):
        self._maybe_fail("get_earn_positions")
        return [p for p in self.positions
                if coin is None or str(p.get("coin")).upper() == coin.upper()]

    def get_earn_orders(self, **kw):
        self._maybe_fail("get_earn_orders")
        return list(self.orders)

    def place_order(self, request):
        self._maybe_fail("place_order")
        body = request["body"]
        self.placed.append(request)
        self.calls.append(("place_order", body["orderType"], body["productId"], body["amount"]))
        order_id = f"oid-{len(self.placed)}"
        self.orders.append({"orderId": order_id, "orderLinkId": body["orderLinkId"],
                            "orderType": body["orderType"], "coin": body["coin"],
                            "productId": body["productId"], "orderValue": body["amount"],
                            "status": "Pending", "createdAt": str(NOW_MS())})
        return {"orderId": order_id, "orderLinkId": body["orderLinkId"]}


def order(order_type="Redeem", pid="1", coin="USDT", status="Pending", link="prev-1", age_s=60):
    return {"orderId": "o-" + link, "orderLinkId": link, "orderType": order_type,
            "coin": coin, "productId": pid, "orderValue": "5", "status": status,
            "createdAt": str(NOW_MS() - age_s * 1000)}


def position(pid="1", coin="USDT", amount="5", status="Active") -> Dict:
    return {"productId": pid, "coin": coin, "amount": amount, "status": status}


Reply = Union[str, Callable[[str], str]]


class FakeAgent:
    """Callable with the same signature as run_yield_cycle.call_agent.

    `reply` is either raw stdout, or a function(cycle_id) -> raw stdout.
    """

    def __init__(self, reply: Reply):
        self.reply = reply
        self.prompts: List[str] = []

    @property
    def called(self) -> bool:
        return bool(self.prompts)

    def __call__(self, prompt, cfg, cycle_id):
        self.prompts.append(prompt)
        raw = self.reply(cycle_id) if callable(self.reply) else self.reply
        return raw, f"{cycle_id}_fake"


def decision_json(cycle_id: str, decisions: List[Dict], risk="NORMAL", **extra) -> str:
    obj = {"cycle_id": cycle_id, "risk_state": risk, "decisions": decisions,
           "holds": [], "alerts": []}
    obj.update(extra)
    return json.dumps(obj)


def reply_with(*decisions: Dict, **extra) -> Callable[[str], str]:
    return lambda cycle_id: decision_json(cycle_id, list(decisions), **extra)


def write_state(path: Path, state="NORMAL", source=risk_state.SOURCE_OPERATOR,
                age_s: float = 0, reason="test", key=KEY) -> Dict:
    return risk_state.write(path, key, state, reason, source,
                            ts_ms=NOW_MS() - int(age_s * 1000))


def load_cfg(isolated_paths, **overrides) -> Dict:
    cfg = yaml.safe_load(Path(isolated_paths["YIELD_CONFIG_FILE"]).read_text())
    cfg.update(overrides)
    return cfg


def read_log(log_dir: Path) -> List[Dict]:
    out = []
    for f in sorted(Path(log_dir).glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return out


def stake_executions(rec: Dict) -> List[Dict]:
    return [e for e in rec.get("executions", []) if e.get("action") == "STAKE"]


def redeem_executions(rec: Dict) -> List[Dict]:
    return [e for e in rec.get("executions", []) if e.get("action") == "REDEEM"]
