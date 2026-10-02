"""carry/book.py — the carry book on disk (CARRY_PLAN Phase 5).

One signed JSON file (signing.py, the risk-state key) holding a SymbolBook
per symbol. load() never guesses: a missing file is an empty book (first
start); an unreadable, unsigned or tampered file is ALSO an empty book, but
with a CRITICAL alert. An empty book next to an open position is then a
BOOK_MISMATCH in the plan (decision 13.15): nothing is traded until the
operator adopts it. The file is written atomically after every cycle.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, fields
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from carry.plan import SymbolBook
from signing import sign, verify

PROFILE = "hermes-carry-book"
_TUPLES = {f.name for f in fields(SymbolBook) if f.name in ("pending_spot", "entry_times")}


def _encode(book: Mapping[str, SymbolBook]) -> Dict:
    return {sym: asdict(sb) for sym, sb in sorted(book.items())}


def _decode(raw: Dict) -> Dict[str, SymbolBook]:
    known = {f.name for f in fields(SymbolBook)}
    out: Dict[str, SymbolBook] = {}
    for sym, d in raw.items():
        if not isinstance(d, dict) or set(d) - known:
            raise ValueError(f"book entry {sym!r} has unknown fields")
        kw = {k: (tuple(v) if k in _TUPLES else v) for k, v in d.items()}
        out[str(sym)] = SymbolBook(**kw)
    return out


def save(path: Path, key: str, book: Mapping[str, SymbolBook], now_ms: int) -> None:
    body = {"profile": PROFILE, "ts_ms": int(now_ms), "books": _encode(book)}
    body["sig"] = sign(key, {k: v for k, v in body.items()})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, sort_keys=True))
    os.replace(tmp, path)


def load(path: Path, key: str) -> Tuple[Dict[str, SymbolBook], Optional[str]]:
    """(book, alert). alert is None for a good file or a first start."""
    path = Path(path)
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("profile") != PROFILE:
            raise ValueError("not a carry book")
        body = {k: v for k, v in data.items() if k != "sig"}
        if not isinstance(data.get("sig"), str) or not verify(key, body, data["sig"]):
            raise ValueError("bad signature")
        return _decode(body["books"]), None
    except (OSError, ValueError, TypeError, KeyError) as e:
        return {}, (f"CRITICAL: BOOK_UNREADABLE: {path}: {e}; treated as empty (open positions "
                    f"become a BOOK_MISMATCH until /adopt carry)")
