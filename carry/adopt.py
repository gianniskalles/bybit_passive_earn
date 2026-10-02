"""carry/adopt.py — the operator's request to adopt a lost carry book
(decision 13.15).

A short outside the book with spot in the wallet is a BOOK_MISMATCH: the
cycle trades neither leg and holds NO_NEW_POSITIONS. Only the operator can
end it: Telegram /adopt carry + /confirm <one-time code> writes this signed
request; the next carry cycle reads it, plans with adopt=True (min(short,
spot) into the book, no trade) and consumes it. A request is single use,
expires after ADOPT_TTL_MS, and is refused unless its HMAC (the same key as
the risk state, signing.py) verifies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

from signing import sign, verify

ADOPT_TTL_MS = 30 * 60 * 1000


def write_request(path: Path, key: str, by: str, now_ms: int) -> None:
    body = {"kind": "carry_adopt", "requested_ms": int(now_ms), "by": str(by)}
    body["sig"] = sign(key, {k: v for k, v in body.items()})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, sort_keys=True))
    os.replace(tmp, path)


def read_request(path: Path, key: str, now_ms: int) -> Optional[Dict]:
    """The request when present, signed with `key` and fresh; else None."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sig"), str):
        return None
    body = {k: v for k, v in data.items() if k != "sig"}
    if body.get("kind") != "carry_adopt" or not verify(key, body, data["sig"]):
        return None
    age = now_ms - int(body.get("requested_ms") or 0)
    if not 0 <= age <= ADOPT_TTL_MS:
        return None
    return body


def consume(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
