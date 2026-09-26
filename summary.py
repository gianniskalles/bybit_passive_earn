#!/usr/bin/env python3
"""summary.py — daily Telegram summary (T5.2), run by yield-summary.timer.

Over the last 24 h of decision records: number of cycles, cycles per
effective risk state, count per alert code, orders (would-call / executed /
failed), plus the current risk_state record and its age.
"""

from __future__ import annotations

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


def main() -> int:
    env = settings.load_env()
    cfg = yaml.safe_load(settings.config_file().read_text()) or {}
    text = build_summary(Path(cfg.get("LOG_DIR") or settings.default_log_dir()),
                         settings.risk_state_file(), env.get("HERMES_RISK_HMAC_KEY", ""))
    print(text)
    sent = Notifier(env, cfg).event(text)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
