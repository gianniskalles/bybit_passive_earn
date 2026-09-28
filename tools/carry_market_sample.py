#!/usr/bin/env python3
"""carry_market_sample.py — MAX_ENTRY_BASIS_BPS and MAX_SPREAD_BPS from live
market data (CARRY_PLAN §13.9, Phase 2). Public endpoints only, no key.

Samples the linear and spot tickers of every symbol every --interval-s for
--minutes, with the SAME basis/spread functions the cycle uses
(carry/snapshot.py), and recommends:

  MAX_ENTRY_BASIS_BPS = ceil(p95 of |basis|)   over all symbols (>= 1)
  MAX_SPREAD_BPS      = ceil(p99 of spread)    over all symbols (>= 1)
      spread = the wider of the two legs

i.e. entries are allowed while basis and spread are in their normal range and
blocked in the widest 5 % (basis) / 1 % (spread) of the sampled minutes.
No recommendation below MIN_SAMPLES per symbol.

  python tools/carry_market_sample.py --minutes 1440 --out calibration      # live, 24 h
  python tools/carry_market_sample.py --from-data FILE --out calibration    # offline

Writes carry_market_<utc>.json (every raw sample) and CARRY_MARKET_SAMPLE.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bybit_earn_tool import BybitAPIError  # noqa: E402
from carry.client import CarryPublicClient  # noqa: E402
from carry.snapshot import basis_bps, spread_bps  # noqa: E402

SYMBOLS = ("BTCUSDT", "ETHUSDT")
MIN_SAMPLES = 60
BASIS_PCTL, SPREAD_PCTL = 95, 99


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile."""
    s = sorted(values)
    if not s:
        raise ValueError("no values")
    k = max(1, math.ceil(p / 100 * len(s)))
    return s[k - 1]


def sample_once(client, symbols) -> List[Dict]:
    now = int(time.time() * 1000)
    out = []
    for sym in symbols:
        try:
            lt, st = client.get_ticker("linear", sym), client.get_ticker("spot", sym)
            out.append({"ts": now, "symbol": sym,
                        "perp_bid": float(lt["bid1Price"]), "perp_ask": float(lt["ask1Price"]),
                        "spot_bid": float(st["bid1Price"]), "spot_ask": float(st["ask1Price"])})
        except (BybitAPIError, KeyError, TypeError, ValueError) as e:
            out.append({"ts": now, "symbol": sym, "error": f"{type(e).__name__}: {e}"})
    return out


def analyze(samples: Sequence[Dict]) -> Dict:
    per: Dict[str, Dict] = {}
    for sym in sorted({s["symbol"] for s in samples}):
        good = [s for s in samples if s["symbol"] == sym and "error" not in s
                and min(s["perp_bid"], s["perp_ask"], s["spot_bid"], s["spot_ask"]) > 0
                and s["perp_ask"] >= s["perp_bid"] and s["spot_ask"] >= s["spot_bid"]]
        errors = sum(1 for s in samples if s["symbol"] == sym) - len(good)
        entry = {"samples": len(good), "rejected": errors}
        if good:
            basis = [abs(basis_bps(s["perp_bid"], s["perp_ask"], s["spot_bid"], s["spot_ask"]))
                     for s in good]
            spread = [max(spread_bps(s["perp_bid"], s["perp_ask"]),
                          spread_bps(s["spot_bid"], s["spot_ask"])) for s in good]
            for name, values in (("abs_basis_bps", basis), ("spread_bps", spread)):
                stats = {f"p{p}": percentile(values, p) for p in (50, 95, 99)}
                stats["max"] = max(values)
                entry[name] = stats
        per[sym] = entry
    enough = bool(per) and all(e["samples"] >= MIN_SAMPLES for e in per.values())
    rec = None
    if enough:
        rec = {"MAX_ENTRY_BASIS_BPS": max(1, math.ceil(max(e["abs_basis_bps"][f"p{BASIS_PCTL}"]
                                                           for e in per.values()))),
               "MAX_SPREAD_BPS": max(1, math.ceil(max(e["spread_bps"][f"p{SPREAD_PCTL}"]
                                                      for e in per.values())))}
    first = min((s["ts"] for s in samples), default=None)
    last = max((s["ts"] for s in samples), default=None)
    return {"per_symbol": per, "recommended": rec, "min_samples": MIN_SAMPLES,
            "from_ms": first, "to_ms": last,
            "hours": round((last - first) / 3_600_000, 2) if first is not None else 0}


def render(report: Dict, source: str) -> str:
    lines = ["# CARRY_MARKET_SAMPLE — basis & spread (Φάση 2)", "",
             f"Πηγή: **{source}**, {report['hours']} ώρες δειγματοληψίας.", "",
             "| Σύμβολο | Δείγματα | Απορρίφθηκαν | |basis| p50 / p95 / p99 / max (bps) | "
             "spread p50 / p95 / p99 / max (bps) |", "|---|---|---|---|---|"]
    for sym, e in report["per_symbol"].items():
        if "abs_basis_bps" in e:
            b, s = e["abs_basis_bps"], e["spread_bps"]
            lines.append(f"| {sym} | {e['samples']} | {e['rejected']} | "
                         f"{b['p50']:.2f} / {b['p95']:.2f} / {b['p99']:.2f} / {b['max']:.2f} | "
                         f"{s['p50']:.2f} / {s['p95']:.2f} / {s['p99']:.2f} / {s['max']:.2f} |")
        else:
            lines.append(f"| {sym} | 0 | {e['rejected']} | — | — |")
    lines += ["", f"Κανόνας: `MAX_ENTRY_BASIS_BPS` = ceil(p{BASIS_PCTL} |basis|), "
              f"`MAX_SPREAD_BPS` = ceil(p{SPREAD_PCTL} spread), το μέγιστο των συμβόλων, ≥ 1.", ""]
    if report["recommended"]:
        lines += ["```yaml"] + [f"{k}: {v}" for k, v in report["recommended"].items()] + ["```"]
    else:
        lines.append(f"**Καμία σύσταση:** λιγότερα από {MIN_SAMPLES} έγκυρα δείγματα ανά σύμβολο.")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, default=1440)
    ap.add_argument("--interval-s", type=float, default=60)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--from-data", type=Path, help="replay a saved carry_market_*.json (offline)")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "carry")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    if args.from_data:
        data = json.loads(args.from_data.read_text())
    else:
        client = CarryPublicClient()
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        data = {"source": f"bybit {client.base_url}", "symbols": symbols, "samples": []}
        deadline = time.time() + args.minutes * 60
        while True:
            data["samples"].extend(sample_once(client, symbols))
            if time.time() + args.interval_s > deadline:
                break
            time.sleep(args.interval_s)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (args.out / f"carry_market_{stamp}.json").write_text(json.dumps(data))

    report = analyze(data["samples"])
    (args.out / "carry_market_sample.json").write_text(json.dumps(report, indent=2))
    md = render(report, data.get("source", "?"))
    (args.out / "CARRY_MARKET_SAMPLE.md").write_text(md)
    print(md)
    return 0 if report["recommended"] else 1


if __name__ == "__main__":
    sys.exit(main())
