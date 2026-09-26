#!/usr/bin/env python3
"""
Bybit Earn API client (FlexibleSaving).

Every failure — HTTP error, timeout, non-JSON body, retCode != 0, or a
response without the expected list — raises BybitAPIError.  Nothing is
ever turned into an empty list: "no positions" and "could not read
positions" must never look the same to the wrapper.

Orders go through ONE endpoint, POST /v5/earn/place-order.  The request is
built by place_order_request() — the same dict is logged as `would_call`
in dry-run and sent verbatim in live mode.

BYBIT_TESTNET (1/true/yes) selects https://api-testnet.bybit.com.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

import settings

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
RECV_WINDOW = "5000"
TIMEOUT = 30
CATEGORY = "FlexibleSaving"
PLACE_ORDER_PATH = "/v5/earn/place-order"


class BybitAPIError(RuntimeError):
    """Any failure to get a valid answer from Bybit."""


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes")


def order_link_id(cycle_id: str, order_type: str, product_id: str) -> str:
    """Deterministic client order id: same cycle + action + product -> same
    id, so a retried request is deduplicated by Bybit.  ≤ 36 chars of
    [A-Za-z0-9_-]."""
    raw = f"{cycle_id}-{order_type[:1].upper()}-{product_id}"
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(clean) <= 36:
        return clean
    return "yr-" + hashlib.sha256(raw.encode()).hexdigest()[:33]


def place_order_request(order_type: str, account_type: str, coin: str,
                        product_id: str, amount: str, order_link_id: str) -> Dict[str, Any]:
    """The exact request for POST /v5/earn/place-order (FINISH_PLAN, Appendix A)."""
    if order_type not in ("Stake", "Redeem"):
        raise ValueError(f"orderType must be Stake or Redeem, got {order_type!r}")
    return {
        "method": "POST",
        "path": PLACE_ORDER_PATH,
        "body": {
            "category": CATEGORY,
            "orderType": order_type,
            "accountType": account_type,
            "amount": str(amount),
            "coin": coin,
            "productId": str(product_id),
            "orderLinkId": order_link_id,
        },
    }


class BybitEarnTool:
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 testnet: Optional[bool] = None, session: Optional[requests.Session] = None):
        env = settings.load_env()
        self.api_key = api_key or env.get("BYBIT_API_KEY")
        self.api_secret = api_secret or env.get("BYBIT_API_SECRET")
        self.testnet = _truthy(env.get("BYBIT_TESTNET")) if testnet is None else bool(testnet)
        self.base_url = TESTNET_URL if self.testnet else MAINNET_URL
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"Content-Type": "application/json",
                                     "X-BAPI-RECV-WINDOW": RECV_WINDOW})

    # ---- transport ---------------------------------------------------------

    def _sign(self, payload: str, timestamp: str) -> str:
        msg = f"{timestamp}{self.api_key}{RECV_WINDOW}{payload}"
        return hmac.new(self.api_secret.encode("utf-8"), msg.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                 signed: bool = False) -> Dict[str, Any]:
        """Return the `result` object of a successful call; raise otherwise."""
        method = method.upper()
        params = params or {}
        url = f"{self.base_url}{endpoint}"
        query = urllib.parse.urlencode(sorted(params.items())) if method == "GET" else ""
        if query:
            url += f"?{query}"
        body = json.dumps(params, separators=(",", ":")) if method == "POST" else ""

        headers = {"Content-Type": "application/json", "X-BAPI-RECV-WINDOW": RECV_WINDOW}
        if signed:
            if not (self.api_key and self.api_secret):
                raise BybitAPIError(f"{endpoint}: API credentials not set")
            ts = str(int(time.time() * 1000))
            headers.update({"X-BAPI-API-KEY": self.api_key, "X-BAPI-TIMESTAMP": ts,
                            "X-BAPI-SIGN": self._sign(query if method == "GET" else body, ts)})
        try:
            resp = self.session.request(method=method, url=url, headers=headers,
                                        data=body or None, timeout=TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as e:
            raise BybitAPIError(f"{endpoint}: HTTP error: {e}") from e
        except ValueError as e:
            raise BybitAPIError(f"{endpoint}: response is not JSON") from e
        if not isinstance(payload, dict):
            raise BybitAPIError(f"{endpoint}: response is not an object")
        if payload.get("retCode") != 0:
            raise BybitAPIError(f"{endpoint}: retCode={payload.get('retCode')} "
                                f"retMsg={payload.get('retMsg')!r}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BybitAPIError(f"{endpoint}: missing `result` object")
        return result

    @staticmethod
    def _list(result: Dict[str, Any], endpoint: str, *keys: str) -> List[Dict]:
        for key in keys:
            if isinstance(result.get(key), list):
                return result[key]
        raise BybitAPIError(f"{endpoint}: response has no list ({'/'.join(keys)})")

    # ---- reads -------------------------------------------------------------

    def get_earn_products(self, coin: Optional[str] = None) -> List[Dict]:
        params = {"category": CATEGORY}
        if coin:
            params["coin"] = coin
        return self._list(self._request("GET", "/v5/earn/product", params),
                          "/v5/earn/product", "list")

    def get_earn_apr_history(self, product_id: Optional[str] = None, coin: Optional[str] = None,
                             category: str = CATEGORY) -> List[Dict]:
        if not product_id:
            if not coin:
                raise ValueError("product_id or coin required")
            products = [p for p in self.get_earn_products(coin=coin) if p.get("coin") == coin]
            if not products:
                raise BybitAPIError(f"no {category} product for {coin}")
            product_id = products[0]["productId"]
        result = self._request("GET", "/v5/earn/apr-history",
                               {"category": category, "productId": product_id})
        return self._list(result, "/v5/earn/apr-history", "list")

    def get_earn_positions(self, coin: Optional[str] = None) -> List[Dict]:
        params = {"category": CATEGORY}
        if coin:
            params["coin"] = coin
        result = self._request("GET", "/v5/earn/position", params, signed=True)
        return self._list(result, "/v5/earn/position", "list", "positionList")

    def get_wallet_balance(self, account_type: str = "UNIFIED") -> Dict:
        result = self._request("GET", "/v5/account/wallet-balance",
                               {"accountType": account_type}, signed=True)
        self._list(result, "/v5/account/wallet-balance", "list")
        return result

    def get_earn_orders(self, order_link_id: Optional[str] = None,
                        order_id: Optional[str] = None,
                        product_id: Optional[str] = None) -> List[Dict]:
        """Recent Earn orders (GET /v5/earn/order)."""
        params = {"category": CATEGORY}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        if order_id:
            params["orderId"] = order_id
        if product_id:
            params["productId"] = product_id
        result = self._request("GET", "/v5/earn/order", params, signed=True)
        return self._list(result, "/v5/earn/order", "list")

    # ---- the only write ----------------------------------------------------

    def place_order(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send a request built by place_order_request(), verbatim."""
        if request.get("method") != "POST" or request.get("path") != PLACE_ORDER_PATH:
            raise ValueError("place_order only sends POST /v5/earn/place-order requests")
        return self._request("POST", PLACE_ORDER_PATH, request["body"], signed=True)

    # ---- diagnostics -------------------------------------------------------

    def health(self) -> Dict:
        """Check API connectivity and (if set) credential validity."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            server_time = self._request("GET", "/v5/market/time")
            if not (self.api_key and self.api_secret):
                return {"status": "warning", "message": "Public API OK, no credentials",
                        "base_url": self.base_url, "server_time": server_time, "timestamp": now}
            account = self._request("GET", "/v5/account/info", signed=True)
            return {"status": "ok", "message": "API connection and credentials valid",
                    "base_url": self.base_url, "account": account, "timestamp": now}
        except BybitAPIError as e:
            return {"status": "error", "message": str(e), "base_url": self.base_url,
                    "timestamp": now}


def main():
    """Read-only command-line interface (orders go through run_yield_cycle)."""
    import argparse

    parser = argparse.ArgumentParser(description="Bybit Earn API client (read-only)")
    parser.add_argument("--health", action="store_true", help="Check API health")
    parser.add_argument("--products", action="store_true", help="List Earn products")
    parser.add_argument("--positions", action="store_true", help="Show Earn positions")
    parser.add_argument("--orders", action="store_true", help="Show recent Earn orders")
    parser.add_argument("--apr-history", action="store_true", help="APR history (--product-id)")
    parser.add_argument("--balance", action="store_true", help="Show UNIFIED wallet balance")
    parser.add_argument("--coin", type=str, help="Filter by coin (e.g., USDT)")
    parser.add_argument("--product-id", type=str, help="Product id")
    args = parser.parse_args()

    tool = BybitEarnTool()
    try:
        if args.health:
            out = tool.health()
        elif args.products:
            out = tool.get_earn_products(coin=args.coin)
        elif args.positions:
            out = tool.get_earn_positions(coin=args.coin)
        elif args.orders:
            out = tool.get_earn_orders(product_id=args.product_id)
        elif args.apr_history:
            out = tool.get_earn_apr_history(product_id=args.product_id, coin=args.coin)
        elif args.balance:
            out = tool.get_wallet_balance("UNIFIED")
        else:
            parser.print_help()
            return 0
    except BybitAPIError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
