"""carry/live.py — the live switch (18/10): how the carry runs with real
money while config/carry.yaml says DRY_RUN: true, for ever.

config/carry.yaml is DRY_RUN: true in the repo (a test enforces it) and
deploy.sh refuses anything else. Live is a separate, signed file outside the
repo, <state>/carry_live.json, written ONLY by scripts/carry_live.py enable:
run by Giannis on a terminal, after its checks, with a one-time code sent to
his Telegram and a typed confirmation. deploy.sh never writes or removes it,
and refuses to run while it exists (its manual cycle would trade live).

effective(cfg, env, ...) is what every carry cycle runs with:
  - DEADMAN_URL comes from the environment (/opt/hermes/.env), never from
    the repo's config (decision of 18/10).
  - a config that is not DRY_RUN: true (the supervised round trip's
    in-memory config, the testnet config) is returned as it is.
  - a valid switch: DRY_RUN false, and TOTAL_CAPITAL_CAP_USD no higher than
    the cap Giannis signed. A config beyond the switch (a symbol it does not
    list, limits its cap cannot hold) runs NO_NEW_POSITIONS, CRITICAL.
  - no valid switch (absent, unsigned, tampered) and the LIVE book holds
    nothing: paper, as before (a broken switch is CRITICAL).
  - no valid switch while the LIVE book holds a position: live, UNWIND only
    (CRITICAL) — a lost or broken switch never leaves a real position
    unmanaged, and never opens anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from signing import sign, verify

PROFILE = "hermes-carry-live"


def write(path: Path, key: str, capital_cap: float, symbols: List[str], by: str,
          now_ms: int) -> Dict:
    body = {"profile": PROFILE, "enabled_ms": int(now_ms), "capital_cap": float(capital_cap),
            "symbols": list(symbols), "by": str(by)}
    body["sig"] = sign(key, {k: v for k, v in body.items()})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, sort_keys=True))
    os.replace(tmp, path)
    return body


def read(path: Path, key: str) -> Tuple[Optional[Dict], Optional[str]]:
    """(body, None) for a valid switch; (None, None) when there is none;
    (None, reason) when a file is there but cannot be trusted."""
    path = Path(path)
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("profile") != PROFILE:
            raise ValueError("not a carry live switch")
        body = {k: v for k, v in data.items() if k != "sig"}
        if not key or not isinstance(data.get("sig"), str) or not verify(key, body, data["sig"]):
            raise ValueError("bad signature")
        cap = body.get("capital_cap")
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
            raise ValueError(f"capital_cap {cap!r}")
        return body, None
    except (OSError, ValueError, TypeError) as e:
        return None, f"{path}: {e}"


def live_book_holds(book) -> List[str]:
    from carry.plan import FLAT
    return [s for s, sb in sorted(book.items())
            if sb.status != FLAT or sb.perp_qty > 0 or sb.spot_qty > 0 or sb.pending_spot]


def _within_switch(out: Dict, body: Dict, alerts: List[str]):
    """The live config inside what Giannis signed. The cycle reads
    config/carry.yaml every run, so a `git pull` alone (deploy refuses while
    live) can change it under the switch:
      - a symbol the switch does not list (BTC added while live on ETH);
      - limits the signed cap cannot hold (2 x 90 + 20 against a cap of 100,
        which would fail validation and stop every cycle, position or not).
    Either: NO_NEW_POSITIONS (exits, trims and Earn still run) and CRITICAL
    until the sequence /unwind carry -> /live disable -> deploy -> /live enable
    <cap>. Otherwise the cap is the signed one when lower."""
    from carry import config as cc
    signed = body["capital_cap"]
    why = []
    extra = [s for s in out.get("SYMBOLS") or [] if s not in (body.get("symbols") or [])]
    if extra:
        why.append(f"symbols {extra} not in the live switch {body.get('symbols')}")
    cap = out.get("TOTAL_CAPITAL_CAP_USD")
    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > signed:
        clamped = dict(out, TOTAL_CAPITAL_CAP_USD=signed)
        broken = [e for e in cc.validate(clamped, testnet=False)
                  if e.startswith(("USDT_BUFFER_USD", "MAX_NOTIONAL_PER_SYMBOL_USD"))]
        if broken:
            why.append(f"the config's limits need TOTAL_CAPITAL_CAP_USD {cap}, the switch "
                       f"signed {signed:g} ({broken[0]})")
        else:
            out = clamped
    if why:
        alerts.append("CRITICAL: LIVE_CONFIG_BEYOND_SWITCH: " + "; ".join(why) + ". No new "
                      "positions: /unwind carry, /live disable, deploy, /live enable <cap>")
        return out, alerts, "NO_NEW_POSITIONS", body
    return out, alerts, None, body


def effective(cfg: Mapping, env: Mapping[str, str], switch_file: Path, live_book_file: Path,
              key: str) -> Tuple[Dict, List[str], Optional[str], Optional[Dict]]:
    """(config to run, alerts, forced risk state or None, the switch or None)."""
    out = dict(cfg)
    if env.get("DEADMAN_URL"):
        out["DEADMAN_URL"] = env["DEADMAN_URL"]
    if out.get("DRY_RUN") is not True:
        return out, [], None, None
    body, broken = read(switch_file, key)
    alerts: List[str] = []
    if body is not None:
        out["DRY_RUN"] = False
        return _within_switch(out, body, alerts)
    if broken:
        alerts.append(f"CRITICAL: LIVE_SWITCH_INVALID: {broken}; never trusted")
    from carry import book as book_store
    book, book_alert = book_store.load(live_book_file, key)
    if book_alert:
        alerts.append(f"CRITICAL: LIVE_BOOK_UNREADABLE without a live switch: {book_alert}")
    held = live_book_holds(book)
    if held:
        alerts.append(f"CRITICAL: LIVE_WITHOUT_SWITCH: the live book holds {held} and there is "
                      f"no valid live switch; running live, UNWIND only, until FLAT")
        out["DRY_RUN"] = False
        return out, alerts, "UNWIND", None
    return out, alerts, None, None
