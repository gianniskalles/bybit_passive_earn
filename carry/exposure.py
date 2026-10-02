"""carry/exposure.py — does the carry account hold anything? (Phase 6)

deploy.sh asks this before any step that would orphan the carry book:
  - moving an unreadable book aside           (decision 13.15 / Phase 6)
  - generating a new HMAC key                 (the book is signed with it: a
                                               new key = BOOK_UNREADABLE + hold)
  - switching the active system back to the yield rotation (the carry timers
                                               would stop over an open position)

FLAT only when everything that can hold a position was READ and holds
nothing: the exchange (when a carry key is set: no perp position on any
SYMBOL, no base coin above dust), the paper account (under DRY_RUN the book
mirrors it, so a paper position counts as a position), and the book (when
readable). Any read failure is UNKNOWN, which every caller treats as OPEN
(fail closed). The exchange reads are the production snapshot readers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional

from carry import book as book_store
from carry import snapshot as snapshot_mod
from carry.paper import PaperExchange
from carry.plan import FLAT as BOOK_FLAT

FLAT = "FLAT"
OPEN = "OPEN"
UNKNOWN = "UNKNOWN"
NOT_CONFIGURED = "NOT_CONFIGURED"     # no carry key, book or paper account: carry never ran


@dataclass
class Exposure:
    verdict: str
    lines: List[str] = field(default_factory=list)

    @property
    def flat(self) -> bool:
        return self.verdict in (FLAT, NOT_CONFIGURED)


def book_holds(path: Path, key: str) -> Optional[List[str]]:
    """What a readable book holds (symbols not FLAT or with a quantity);
    None when the file is absent or unreadable."""
    if not Path(path).exists():
        return None
    book, alert = book_store.load(path, key)
    if alert:
        return None
    return [f"{s}: {sb.status} perp {sb.perp_qty} spot {sb.spot_qty}" for s, sb in sorted(book.items())
            if sb.status != BOOK_FLAT or sb.perp_qty > 0 or sb.spot_qty > 0]


def paper_holds(path: Path) -> Optional[List[str]]:
    """What the DRY_RUN paper account holds; None when there is no paper file.
    An unreadable paper file raises (the caller makes it UNKNOWN)."""
    if not Path(path).exists():
        return None
    st = PaperExchange.from_json(Path(path).read_text(), {}).s
    out = [f"paper {s}: short {q}" for s, q in sorted(st.shorts.items()) if q > 0]
    out += [f"paper {c}: {q}" for c, q in sorted(st.coins.items()) if q > 0]
    return out


def check(client, cfg: Mapping, book_path: Path, key: str, paper_path: Optional[Path] = None,
          now_ms: Optional[int] = None) -> Exposure:
    lines: List[str] = []
    book_exists = Path(book_path).exists()
    paper_exists = paper_path is not None and Path(paper_path).exists()
    if client is None and not book_exists and not paper_exists:
        return Exposure(NOT_CONFIGURED, ["no carry API key, no carry book, no paper account: "
                                         "carry never ran here"])
    open_ = False
    held = book_holds(book_path, key) if key else None
    if book_exists:
        lines.append("book: " + ("unreadable with the current key" if held is None
                                 else (", ".join(held) or "empty / all FLAT")))
        open_ = bool(held)
    if paper_exists:
        try:
            paper = paper_holds(paper_path)
        except Exception as e:                                # noqa: BLE001
            return Exposure(UNKNOWN, lines + [f"paper account unreadable: {type(e).__name__}: {e}"])
        lines.append("paper account: " + (", ".join(paper) or "nothing held"))
        open_ = open_ or bool(paper)
    if client is None:
        if book_exists and not (isinstance(cfg, Mapping) and cfg.get("DRY_RUN") is True):
            # A live book without a key to read the exchange with: whatever
            # the book says, the exchange itself was not seen. (Under DRY_RUN
            # the book mirrors the paper account, read above.)
            return Exposure(UNKNOWN, lines + ["the carry API key is not set: the exchange "
                                              "cannot be read"])
        return Exposure(OPEN if open_ else FLAT, lines)
    try:
        snap = snapshot_mod.take(client, cfg, now_ms=now_ms, private=True)
    except Exception as e:                                    # noqa: BLE001
        return Exposure(UNKNOWN, lines + [f"exchange unreadable: {type(e).__name__}: {e}"])
    needed = [f"positions:{s}" for s in snap.symbols] + ["account"] + \
        [f"market:{s}" for s in snap.symbols]
    errors = {k: v for k, v in snap.errors.items() if k in needed}
    if errors:
        return Exposure(UNKNOWN, lines + [f"exchange unreadable: {k}: {v}"
                                          for k, v in sorted(errors.items())])
    seen = False
    for sym in snap.symbols:
        p = snap.positions[sym]
        if not p.flat:
            seen = True
            lines.append(f"exchange {sym}: perp {p.side} {p.size}")
        coin = sym[:-len("USDT")]
        bal = snap.account.balance(coin).wallet
        dust = snap.markets[sym].spot.min_qty
        if bal > dust:
            seen = True
            lines.append(f"exchange {coin}: wallet {bal} (> dust {dust})")
    if not seen:
        lines.append("exchange: no perp position and no base coin above dust")
    return Exposure(OPEN if open_ or seen else FLAT, lines)
