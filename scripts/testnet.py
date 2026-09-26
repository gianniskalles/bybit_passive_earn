#!/usr/bin/env python3
"""Testnet tooling for FINISH_PLAN Phase 6, step 4. Refuses to run unless
BYBIT_TESTNET=1 — it never touches mainnet.

  capture    read-only: save the real responses of every endpoint the cycle
             reads to tests/data/testnet_<utc>.json (replayable format:
             {"recorded_at_ms", "responses": {path: raw response}}).
  roundtrip  Stake the product minimum, wait for a final status, Redeem it,
             wait again. Orders are built by the SAME code as production
             (executor -> place_order_request). Every raw response is saved
             to tests/data/testnet_roundtrip_<utc>.json.

Once saved, tests/test_recorded_payloads.py locks the parsing on them.

  sudo -u hermes env BYBIT_TESTNET=1 /opt/hermes/venvs/yield_rotation/bin/python \\
      /opt/hermes/yield_rotation/scripts/testnet.py capture
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import settings  # noqa: E402
from bybit_earn_tool import BybitEarnTool, _truthy  # noqa: E402
from executor import Executor  # noqa: E402

DATA_DIR = ROOT / "tests" / "data"


class RecordingSession(requests.Session):
    """A requests.Session that keeps every raw JSON response by path."""

    def __init__(self):
        super().__init__()
        self.log = []

    def request(self, method, url, **kw):
        resp = super().request(method, url, **kw)
        try:
            body = resp.json()
        except ValueError:
            body = {"_non_json": resp.text[:500]}
        path = urllib.parse.urlparse(url).path
        self.log.append({"method": method, "path": path, "query": urllib.parse.urlparse(url).query,
                         "request_body": json.loads(kw["data"]) if kw.get("data") else None,
                         "status": resp.status_code, "response": body,
                         "ts_ms": int(time.time() * 1000)})
        return resp


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _tool() -> tuple[BybitEarnTool, RecordingSession]:
    if not _truthy(settings.load_env().get("BYBIT_TESTNET")):
        raise SystemExit("refusing: BYBIT_TESTNET is not set to 1 (this script is testnet-only)")
    session = RecordingSession()
    tool = BybitEarnTool(session=session, testnet=True)
    if not (tool.api_key and tool.api_secret):
        raise SystemExit("BYBIT_API_KEY / BYBIT_API_SECRET (testnet) not set")
    return tool, session


def capture(coin: str) -> Path:
    tool, session = _tool()
    products = [p for p in tool.get_earn_products(coin=coin) if p.get("coin") == coin]
    if products:
        tool.get_earn_apr_history(product_id=products[0]["productId"])
    tool.get_earn_positions(coin=coin)
    tool.get_earn_orders()
    tool.get_wallet_balance("UNIFIED")
    out = {"_comment": f"REAL testnet capture ({coin}), scripts/testnet.py capture",
           "recorded_at_ms": int(time.time() * 1000),
           "responses": {e["path"]: e["response"] for e in session.log}}
    path = DATA_DIR / f"testnet_{_stamp()}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return path


def _wait_final(tool: BybitEarnTool, link: str, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    while True:
        orders = tool.get_earn_orders(order_link_id=link)
        if orders and str(orders[0].get("status", "")).lower() in ("success", "fail"):
            return orders[0]
        if time.time() > deadline:
            return orders[0] if orders else {}
        time.sleep(10)


def roundtrip(coin: str, timeout_s: int) -> Path:
    tool, session = _tool()
    [product] = [p for p in tool.get_earn_products(coin=coin)
                 if p.get("coin") == coin and p.get("status") == "Available"][:1] or [None]
    if product is None:
        raise SystemExit(f"no Available {coin} FlexibleSaving product on testnet")
    amount = str(product["minStakeAmount"])
    cycle_id = "testnet_" + _stamp()
    ex = Executor(bybit_tool=tool, dry_run=False, allow_new_positions=True,
                  account_type="UNIFIED", cycle_id=cycle_id)
    base = {"coin": coin, "product_id": str(product["productId"]), "amount": amount,
            "origin": "testnet", "reason": "testnet roundtrip"}
    report = {"cycle_id": cycle_id, "product": product, "steps": []}
    for action in ("STAKE", "REDEEM"):
        [rec] = ex.execute([{**base, "action": action}])
        final = _wait_final(tool, rec["order_link_id"], timeout_s) if rec["executed"] else {}
        report["steps"].append({"execution": rec, "final_order": final})
        print(f"{action}: executed={rec['executed']} orderId={rec.get('response', {}).get('orderId')} "
              f"final={final.get('status')}", file=sys.stderr)
        if not rec["executed"] or str(final.get("status", "")).lower() != "success":
            break
    report["http_log"] = session.log
    path = DATA_DIR / f"testnet_roundtrip_{_stamp()}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["capture", "roundtrip"])
    ap.add_argument("--coin", default="USDT")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for a final status")
    args = ap.parse_args(argv)
    path = capture(args.coin) if args.command == "capture" else roundtrip(args.coin, args.timeout)
    print(f"saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
