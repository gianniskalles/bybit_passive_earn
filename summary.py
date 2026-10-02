#!/usr/bin/env python3
"""summary.py — daily Telegram summary (T5.2).

  summary.py                 yield rotation (yield-summary.timer)
  summary.py --system carry  carry (yield-carry-summary.timer)

Yield rotation, over the last 24 h of decision records: number of cycles,
cycles per effective risk state, count per alert code, orders (would-call /
executed / failed), plus the current risk_state record and its age.

Carry: the same counts from the carry records, the hold if any, one line per
symbol with the smoothed funding now against what the entry rule needs
("ETH: 2,6% — χρειάζεται 6,7%": ENTRY_MIN_EXPECTED_APR above the layer A
APR), the book, and the ledger of the last 24 h (funding, Earn interest,
fees, net; under DRY_RUN the paper account).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

import risk_state
import settings
from notify import Notifier


def _records(log_dir: Path, since_ms: int) -> List[Dict]:
    out = []
    for f in sorted(Path(log_dir).glob("*.jsonl"))[-3:]:
        for line in f.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(str(rec["ts"]).replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError):
                continue
            if ts.timestamp() * 1000 >= since_ms:
                out.append(rec)
    return out


def build_summary(log_dir: Path, state_file: Path, hmac_key: str,
                  now_ms: Optional[int] = None, hours: int = 24) -> str:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    recs = _records(log_dir, now - hours * 3600 * 1000)
    states = Counter(r.get("risk_state") for r in recs)
    codes = Counter(str(a).split(":", 1)[0] for r in recs for a in r.get("alerts", []))
    execs = [e for r in recs for e in r.get("executions", []) if e.get("would_call")]
    v = risk_state.verify(state_file, hmac_key, now_ms=now)
    age = f"{v.age_ms // 60000} min" if v.age_ms is not None else "?"
    lines = [
        f"📊 Yield rotation — last {hours} h",
        f"cycles: {len(recs)}  (NORMAL {states.get('NORMAL', 0)}, "
        f"NO_NEW_POSITIONS {states.get('NO_NEW_POSITIONS', 0)}, UNWIND {states.get('UNWIND', 0)})",
        f"risk_state now: {v.state or '-'} ({v.code}, source={v.source}, age {age})",
        f"orders: planned {len(execs)}, executed {sum(1 for e in execs if e.get('executed'))}, "
        f"failed {sum(1 for e in execs if e.get('error'))}"
        + ("  [DRY_RUN]" if recs and recs[-1].get("dry_run") else ""),
        "alerts: " + (", ".join(f"{c}×{n}" for c, n in codes.most_common()) or "none"),
    ]
    return "\n".join(lines)


def pct(x: float) -> str:
    """0.026 -> "2,6%", 0.05 -> "5%" (Greek decimal comma)."""
    return f"{x * 100:.1f}".rstrip("0").rstrip(".").replace(".", ",") + "%"


def funding_line(sym: str, f: Dict, status: Optional[str]) -> str:
    coin = sym[:-len("USDT")] if sym.endswith("USDT") else sym
    now = f.get("smoothed_apr")
    if now is None:
        return f"{coin}: εξομαλυμένο funding άγνωστο (λίγα settlements)"
    if status == "OPEN":
        return f"{coin}: {pct(now)} — θέση ανοιχτή"
    need, layer_a = f.get("required_apr"), f.get("layer_a_apr")
    if need is None:
        return (f"{coin}: {pct(now)} — χρειάζεται {pct(f['entry_min_expected_apr'])} πάνω από "
                f"το Earn (APR του Earn άγνωστο)")
    return (f"{coin}: {pct(now)} — χρειάζεται {pct(need)} "
            f"({pct(f['entry_min_expected_apr'])} πάνω από το Earn {pct(layer_a)})")


def build_carry_summary(log_dir: Path, state_file: Path, hold_file: Path, hmac_key: str,
                        now_ms: Optional[int] = None, hours: int = 24) -> str:
    from carry import ledger
    from carry import risk as carry_risk

    now = int(time.time() * 1000) if now_ms is None else now_ms
    since = now - hours * 3600 * 1000
    recs = _records(log_dir, since)
    last = recs[-1] if recs else {}
    states = Counter(r.get("risk_state") for r in recs)
    codes = Counter(str(a).split(":", 1)[0] for r in recs for a in r.get("alerts", []))
    v = risk_state.verify(state_file, hmac_key, now_ms=now, profile=risk_state.CARRY_PROFILE)
    age = f"{v.age_ms // 60000} min" if v.age_ms is not None else "?"
    lines = [
        f"📊 Carry — last {hours} h" + ("  [DRY_RUN]" if last.get("dry_run") else ""),
        f"cycles: {len(recs)}  (NORMAL {states.get('NORMAL', 0)}, "
        f"NO_NEW_POSITIONS {states.get('NO_NEW_POSITIONS', 0)}, UNWIND {states.get('UNWIND', 0)})",
        f"risk state now: {v.state or '-'} ({v.code}, source={v.source}, age {age})",
    ]
    hold = carry_risk.read_hold(hold_file)
    if hold:
        lines.append(f"hold: {hold['reason']} — released by /resume carry")
    book = last.get("book") or {}
    for sym, f in sorted((last.get("funding") or {}).items()):
        lines.append(funding_line(sym, f, (book.get(sym) or {}).get("status")))
    for sym, b in sorted(book.items()):
        lines.append(f"{sym}: {b.get('status')}  perp {b.get('perp_qty')}  spot {b.get('spot_qty')}")
    s = ledger.summary(ledger.read(log_dir, since_ms=since, until_ms=now))
    lines.append(f"ledger: funding {s['funding']:+.4f}, Earn interest {s['earn_interest']:+.4f}, "
                 f"fees {s['fees']:.4f}, net {s['net']:+.4f} USDT; orders {s['orders']}, "
                 f"max slippage {s['slippage_bps_max']:.1f} bps")
    if last.get("paper"):
        p = last["paper"]
        lines.append(f"paper account: USDT {p.get('usdt', 0):.2f}, Earn {p.get('earn_staked', 0):.2f}")
    lines.append("alerts: " + (", ".join(f"{c}×{n}" for c, n in codes.most_common()) or "none"))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="daily Telegram summary")
    ap.add_argument("--system", choices=("yield", "carry"), default="yield")
    args = ap.parse_args([] if argv is None else argv)
    env = settings.load_env()
    if args.system == "carry":
        cfg = yaml.safe_load(settings.carry_config_file().read_text()) or {}
        log = cfg.get("LOG_DIR")
        text = build_carry_summary(Path(log) if isinstance(log, str)
                                   else settings.default_log_dir() / "carry",
                                   settings.carry_risk_state_file(), settings.carry_hold_file(),
                                   env.get("HERMES_RISK_HMAC_KEY", ""))
        print(text)
        return 0 if Notifier(env, cfg).event(text) else 1
    cfg = yaml.safe_load(settings.config_file().read_text()) or {}
    text = build_summary(Path(cfg.get("LOG_DIR") or settings.default_log_dir()),
                         settings.risk_state_file(), env.get("HERMES_RISK_HMAC_KEY", ""))
    print(text)
    sent = Notifier(env, cfg).event(text)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
