"""Replay recorded Bybit responses through the REAL BybitEarnTool.

A payload file (tests/data/*.json) holds full Bybit responses keyed by
endpoint path plus `recorded_at_ms`.  ReplaySession serves them to
BybitEarnTool, so the production parsing code runs unchanged.  APR-history
timestamps are shifted so the capture looks as fresh as when it was taken.
"""

from __future__ import annotations

import copy
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from bybit_earn_tool import BybitEarnTool

DATA_DIR = Path(__file__).resolve().parent / "data"


def load_payload(name: str) -> Dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text())


class _Response:
    def __init__(self, payload):
        self.payload, self.status_code = payload, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class ReplaySession:
    def __init__(self, payload: Dict[str, Any], now_ms: Optional[int] = None):
        self.payload = copy.deepcopy(payload)
        self.headers: Dict[str, str] = {}
        self.requests = []
        now = int(time.time() * 1000) if now_ms is None else now_ms
        self.shift_ms = now - int(self.payload["recorded_at_ms"])

    def request(self, method, url, headers=None, data=None, timeout=None):
        parsed = urllib.parse.urlparse(url)
        self.requests.append((method, parsed.path, parsed.query))
        response = self.payload["responses"].get(parsed.path)
        if response is None:
            raise AssertionError(f"no recorded response for {parsed.path}")
        response = copy.deepcopy(response)
        if parsed.path == "/v5/earn/apr-history":
            for point in response.get("result", {}).get("list", []):
                point["timestamp"] = str(int(point["timestamp"]) + self.shift_ms)
        return _Response(response)


def replay_tool(payload: Dict[str, Any]) -> BybitEarnTool:
    return BybitEarnTool(api_key="replay", api_secret="replay", testnet=False,
                         session=ReplaySession(payload))


def mutate(payload: Dict[str, Any], path: str, fn: Callable[[Any], None]) -> Dict[str, Any]:
    """Return a copy of `payload` with fn applied to responses[path]['result']."""
    out = copy.deepcopy(payload)
    fn(out["responses"][path]["result"])
    return out
