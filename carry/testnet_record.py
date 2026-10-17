"""carry/testnet_record.py — raw Bybit responses of the testnet cycle (16/10).

The forced testnet cycle keeps every Bybit response it receives, so the
unverified fields of CARRY_PLAN §12 (cumFeeDetail, retCodes 110017/110072,
the transaction log's SETTLEMENT `change`) can be locked in tests on real
answers.

Testnet, or mainnet only for the one supervised round trip
(scripts/mainnet_roundtrip.py, approved_mainnet=True): the constructor
refuses anything else.
Only the RESPONSE is kept: its body, the HTTP status, the method and the URL
path. Never a request header (X-BAPI-API-KEY, X-BAPI-SIGN, X-BAPI-TIMESTAMP,
...), never the query string or the request body, never a key, a secret or
a signature. One JSONL file per UTC day under <LOG_DIR>/raw/.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests


class ResponseRecorder(requests.Session):
    def __init__(self, out_dir: Path, testnet: bool, approved_mainnet: bool = False):
        if testnet is not True and approved_mainnet is not True:
            raise ValueError("refusing: raw responses are recorded on testnet only "
                             "(or by the approved mainnet round trip)")
        super().__init__()
        self.out_dir = Path(out_dir)

    def request(self, method, url, *args, **kwargs):
        resp = super().request(method, url, *args, **kwargs)
        try:
            self._keep(method, url, resp)
        except OSError:
            pass                     # recording never breaks the cycle
        return resp

    def _keep(self, method, url, resp) -> None:
        try:
            body = resp.json()
        except ValueError:
            body = {"_non_json": (resp.text or "")[:2000]}
        now = time.time()
        row = {"ts_ms": int(now * 1000), "method": str(method).upper(),
               "path": urllib.parse.urlparse(url).path, "status": resp.status_code,
               "body": body}
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with (self.out_dir / f"raw_{day}.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
