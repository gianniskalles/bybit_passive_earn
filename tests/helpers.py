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
            max_stake="100000", remaining="-1", precision="8", **extra) -> Dict:
    p = {"category": "FlexibleSaving", "productId": pid, "coin": coin,
         "estimateApr": apr, "status": status, "minStakeAmount": min_stake,
         "maxStakeAmount": max_stake, "remainingPoolAmount": remaining,
         "precision": precision, "hasTieredApr": False}
    p.update(extra)
    return {k: v for k, v in p.items() if v is not None}


def apr_history(apr="1.1%", points=24, newest_age_s=600, step_s=3600) -> List[Dict]:
    now = NOW_MS()
    return [{"timestamp": str(now - (newest_age_s + i * step_s) * 1000), "apr": apr}
            for i in range(points)]


class FakeBybit:
    def __init__(self, products=None, positions=None, wallet="100", history=None):
        self.products = [product()] if products is None else products
        self.positions = positions or []
        self.wallet = wallet
        self.history = history
        self.calls: List[tuple] = []

    def get_earn_products(self):
        return list(self.products)

    def get_earn_apr_history(self, coin=None, product_id=None, **kw):
        return list(self.history) if self.history is not None else apr_history()

    def get_wallet_balance(self, account_type="UNIFIED"):
        return {"list": [{"coin": [{"coin": "USDT", "walletBalance": self.wallet,
                                    "equity": self.wallet}]}]}

    def get_earn_positions(self, coin=None):
        return [p for p in self.positions if coin is None or p.get("coin") == coin]

    def subscribe_earn_product(self, product_id, amount):
        self.calls.append(("subscribe", product_id, amount))
        return {"retCode": 0}

    def redeem_earn_product(self, product_id, amount):
        self.calls.append(("redeem", product_id, amount))
        return {"retCode": 0}


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
