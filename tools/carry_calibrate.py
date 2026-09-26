#!/usr/bin/env python3
"""carry_calibrate.py — CARRY_PLAN Phase 0B: is the funding carry worth it?

Public data only (no API key):
  - 180 days of settled funding per symbol   GET /v5/market/funding/history
  - current funding interval, lot sizes       GET /v5/market/instruments-info
  - USDT Easy Earn APR history (layer A)      GET /v5/earn/apr-history

Then it replays the data through the PRODUCTION decision function
(carry.decide.decide_symbol) over a grid of the ⊙ thresholds:
  - chooses parameters on the FIRST half of the period (in-sample),
  - checks them on the SECOND half (out-of-sample, cold start),
  - judges the §2 GO criteria on the full period with the conservative
    "lagged" predictor, and again with costs × 1.5.

  python tools/carry_calibrate.py --out reports/carry                 # fetch + report
  python tools/carry_calibrate.py --from-data FILE --out reports/carry # offline

Writes carry_calibration.json, CARRY_CALIBRATION.md and (when fetching)
carry_data_<utc>.json with every raw number used, so the run is reproducible.
Exit code: 0 report written (GO or not — read the verdict), 1 missing data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from carry import backtest as bt  # noqa: E402
from carry.decide import Params  # noqa: E402

DAY_MS = 24 * 3600 * 1000
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
# CARRY_PLAN §8 fee fallbacks (the real ones need a key: /v5/account/fee-rate).
BASE_PARAMS = dict(entry_min_expected_apr=0.03, entry_min_predicted_rate=0.0001,
                   exit_predicted_floor=-0.00005, exit_horizon_hours=168,
                   smoothing_settlements=9, min_hold_hours=168, max_round_trips_30d=4,
                   no_funding_action_before_settlement_min=15, max_entry_basis_bps=None,
                   max_spread_bps=None, spot_taker_fee=0.001, perp_taker_fee=0.00055)
GO = dict(min_excess=0.03, worst_30d_floor=-0.005)
# Params field -> config/carry.yaml key (CARRY_PLAN §8 names).
CONFIG_NAMES = {
    "entry_min_expected_apr": "ENTRY_MIN_EXPECTED_APR",
    "entry_min_predicted_rate": "ENTRY_MIN_PREDICTED_RATE",
    "exit_predicted_floor": "EXIT_PREDICTED_FLOOR",
    "exit_horizon_hours": "EXIT_HORIZON_HOURS",
    "smoothing_settlements": "SMOOTHING_SETTLEMENTS",
    "min_hold_hours": "MIN_HOLD_HOURS",
    "max_round_trips_30d": "MAX_ROUND_TRIPS_PER_30D",
    "no_funding_action_before_settlement_min": "NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN",
    "max_entry_basis_bps": "MAX_ENTRY_BASIS_BPS",
    "max_spread_bps": "MAX_SPREAD_BPS",
    "spot_taker_fee": "SPOT_TAKER_FEE",
    "perp_taker_fee": "PERP_TAKER_FEE",
}


# --------------------------------------------------------------------------- #
# Data                                                                         #
# --------------------------------------------------------------------------- #

def _parse_apr(value) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        s = value.strip()
        try:
            return float(s[:-1]) / 100 if s.endswith("%") else float(s)
        except ValueError:
            return None
    return None


def fetch(symbols, days: int) -> Dict:
    from carry.client import CarryPublicClient

    client = CarryPublicClient()
    end = int(time.time() * 1000)
    start = end - days * DAY_MS
    data = {"fetched_at_ms": end, "days": days, "source": f"bybit {client.base_url}",
            "symbols": {}, "layer_a": {}}
    for sym in symbols:
        data["symbols"][sym] = {
            "funding": client.get_funding_history(sym, start, end),
            "instrument_linear": client.get_instrument("linear", sym),
            "instrument_spot": client.get_instrument("spot", sym),
        }
    pid, raw = client.get_usdt_flexible_apr_history()
    points = sorted((int(r["timestamp"]), apr) for r in raw
                    if isinstance(r, dict) and r.get("timestamp") is not None
                    and (apr := _parse_apr(r.get("apr"))) is not None)
    data["layer_a"] = {"source": f"USDT FlexibleSaving productId {pid}", "points": points,
                       "raw_sample": raw[:3]}
    return data


# --------------------------------------------------------------------------- #
# Calibration                                                                  #
# --------------------------------------------------------------------------- #

def _run(data, params: Params, predictor: str, cost_multiplier: float = 1.0,
         eval_from=None, eval_to=None) -> Tuple[bt.Result, Dict[str, bt.Result]]:
    per = {}
    for sym, d in data["symbols"].items():
        per[sym] = bt.simulate([tuple(x) for x in d["funding"]], data["layer_a"]["points"],
                               params, predictor=predictor, cost_multiplier=cost_multiplier,
                               symbol=sym, eval_from_ms=eval_from, eval_to_ms=eval_to)
    return bt.combine(list(per.values())), per


def calibrate(data: Dict) -> Dict:
    all_ts = [t for d in data["symbols"].values() for t, _ in d["funding"]]
    start, end = min(all_ts), max(all_ts)
    mid = start + (end - start) // 2

    grid = list(bt.param_grid(BASE_PARAMS))
    scored = []
    for prm in grid:
        is_port, _ = _run(data, prm, "lagged", eval_to=mid)
        scored.append((is_port.excess_apr, is_port.worst_30d_return, prm))
    eligible = [s for s in scored if s[1] >= GO["worst_30d_floor"]] or scored
    eligible.sort(key=lambda s: s[0], reverse=True)
    best_is_excess, _, chosen = eligible[0]

    oos, _ = _run(data, chosen, "lagged", eval_from=mid)
    full, per = _run(data, chosen, "lagged")
    full_up, per_up = _run(data, chosen, "lagged", cost_multiplier=1.5)
    full_oracle, _ = _run(data, chosen, "oracle")
    verdict = bt.go_check(full, full_up, **GO)

    grid_pass = sum(
        1 for prm in grid
        if bt.go_check(_run(data, prm, "lagged")[0], _run(data, prm, "lagged", 1.5)[0], **GO)["go"])

    return {
        "period": {"start_ms": start, "end_ms": end, "days": round((end - start) / DAY_MS, 1),
                   "split_ms": mid},
        "verdict": verdict,
        "recommended_params": {k: getattr(chosen, k) for k in BASE_PARAMS},
        "in_sample": {"excess_apr": best_is_excess},
        "out_of_sample": oos.summary(),
        "full_period_lagged": full.summary(),
        "full_period_oracle": full_oracle.summary(),
        "full_period_costs_x1_5": full_up.summary(),
        "per_symbol": {sym: {"lagged": r.summary(), "costs_x1_5": per_up[sym].summary()}
                       for sym, r in per.items()},
        "grid": {"size": len(grid), "passing_go": grid_pass},
    }


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #

def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.2f}%"


def markdown(report: Dict, data: Dict, assumed_a: Optional[float]) -> str:
    v, per = report["verdict"], report["per_symbol"]
    when = datetime.fromtimestamp(data["fetched_at_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# CARRY_CALIBRATION — Φάση 0Β",
        "",
        f"Πηγή δεδομένων: **{data.get('source')}**, λήψη {when}, "
        f"{report['period']['days']} ημέρες. Layer A: {data['layer_a'].get('source')}"
        + (f" — **ASSUMED {assumed_a:.2%}** (όχι από δεδομένα)" if assumed_a is not None else ""),
        "",
        f"## Απόφαση: **{'GO' if v['go'] else 'NO-GO'}**",
        "",
        "| Κριτήριο (§2) | Τιμή | Όριο | |",
        "|---|---|---|---|",
        f"| Καθαρό APR στρατηγικής − layer A (180 ημ., lagged) | {_pct(v['excess_apr'])} | ≥ {_pct(GO['min_excess'])} | {'✅' if v['excess_ok'] else '❌'} |",
        f"| Χειρότερο 30ήμερο (καθαρό, επί του κεφαλαίου) | {_pct(v['worst_30d_return'])} | ≥ {_pct(GO['worst_30d_floor'])} | {'✅' if v['worst_30d_ok'] else '❌'} |",
        f"| Με έξοδα × 1,5: υπεροχή / χειρότερο 30ήμερο | {_pct(v['excess_apr_costs_x1_5'])} / {_pct(v['worst_30d_costs_x1_5'])} | ίδια όρια | {'✅' if v['robust_to_costs'] else '❌'} |",
        "",
        f"Out-of-sample (δεύτερο μισό, παράμετροι επιλεγμένες στο πρώτο): υπεροχή "
        f"**{_pct(report['out_of_sample']['excess_apr'])}**, χειρότερο 30ήμερο "
        f"{_pct(report['out_of_sample']['worst_30d_return'])}. "
        f"Συνδυασμοί του grid που περνούν το GO: {report['grid']['passing_go']}/{report['grid']['size']}.",
        "",
        "## Ανά σύμβολο (180 ημ., lagged predictor)",
        "",
        "| Σύμβολο | Καθαρό APR | Layer A | Υπεροχή | Round trips | Χρόνος σε θέση | Χειρ. 30ήμ. | Αρνητικό funding | Μεγαλύτερο αρνητικό διάστημα | Υπεροχή με έξοδα ×1,5 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for sym, r in per.items():
        l, u = r["lagged"], r["costs_x1_5"]
        lines.append(f"| {sym} | {_pct(l['net_apr'])} | {_pct(l['layer_a_apr'])} | {_pct(l['excess_apr'])} | "
                     f"{l['round_trips']} ({l['entries']} είσοδοι) | {_pct(l['time_in_position'])} | "
                     f"{_pct(l['worst_30d_return'])} | {_pct(l['negative_share'])} των settlements | "
                     f"{l['longest_negative_days']:.1f} ημ. | {_pct(u['excess_apr'])} |")
    t, o = report["full_period_lagged"], report["full_period_oracle"]
    lines += [
        f"| **Σύνολο** (ίση κατανομή) | {_pct(t['net_apr'])} | {_pct(t['layer_a_apr'])} | {_pct(t['excess_apr'])} | "
        f"{t['round_trips']} | {_pct(t['time_in_position'])} | {_pct(t['worst_30d_return'])} | "
        f"{_pct(t['negative_share'])} | {t['longest_negative_days']:.1f} ημ. | "
        f"{_pct(report['full_period_costs_x1_5']['excess_apr'])} |",
        "",
        f"Με τον «oracle» predictor (επιτόκιο που τελικά πληρώθηκε): υπεροχή {_pct(o['excess_apr'])}.",
        "",
        "## Προτεινόμενες παράμετροι (⊙ της §8)",
        "",
        "```yaml",
        *[f"{CONFIG_NAMES[k]}: {json.dumps(v)}" for k, v in report["recommended_params"].items()],
        "```",
        "",
        "## Τι ΔΕΝ μοντελοποιείται",
        "",
        "- Basis, spread και slippage κατά την είσοδο/έξοδο — μόνο taker fees· η ευαισθησία ×1,5 είναι το υποκατάστατο.",
        "- Οι χρεώσεις είναι οι fallback της §8 (οι πραγματικές θέλουν κλειδί: `/v5/account/fee-rate`).",
        "- Αποφάσεις μόνο μία φορά ανά settlement (16′ πριν)· ο πραγματικός κύκλος είναι κάθε 5′.",
        "- Το «predicted rate» δεν υπάρχει στο ιστορικό· ο lagged predictor (προηγούμενο settlement) κρίνει το GO.",
        "- Layer A = ιστορικό APR του USDT FlexibleSaving· αν περιέχει μπόνους που το BYUSDT δεν παίρνει, το layer A υπερεκτιμάται (άρα η υπεροχή υποεκτιμάται).",
        "- Κίνδυνοι margin, ADL, ρευστοποίησης, πλατφόρμας: εκτός αυτής της μέτρησης (§5).",
    ]
    lin = {s: d.get("instrument_linear", {}) for s, d in data["symbols"].items()}
    lines += ["", "## Τρέχοντα στοιχεία συμβόλων", ""]
    for s, i in lin.items():
        lot = i.get("lotSizeFilter", {})
        lines.append(f"- {s}: status {i.get('status')}, fundingInterval {i.get('fundingInterval')} min, "
                     f"qtyStep {lot.get('qtyStep')}, minOrderQty {lot.get('minOrderQty')}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--from-data", type=Path, help="replay a saved carry_data_*.json (offline)")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "carry")
    ap.add_argument("--assume-layer-a-apr", type=float, default=None,
                    help="ONLY if the APR history is unavailable; the report marks it ASSUMED")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    if args.from_data:
        data = json.loads(args.from_data.read_text())
    else:
        data = fetch([s.strip().upper() for s in args.symbols.split(",") if s.strip()], args.days)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (args.out / f"carry_data_{stamp}.json").write_text(json.dumps(data))

    for sym, d in data["symbols"].items():
        if len(d["funding"]) < 30:
            print(f"ERROR: only {len(d['funding'])} funding settlements for {sym}")
            return 1
    if not data["layer_a"].get("points"):
        if args.assume_layer_a_apr is None:
            print("ERROR: no layer A (Easy Earn USDT) APR history; unknown is not zero. "
                  "Re-run where it is available, or pass --assume-layer-a-apr (marked ASSUMED).")
            return 1
        first = min(t for d in data["symbols"].values() for t, _ in d["funding"])
        data["layer_a"] = {"source": "ASSUMED", "points": [[first, args.assume_layer_a_apr]]}

    report = calibrate(data)
    report["assumed_layer_a_apr"] = args.assume_layer_a_apr if data["layer_a"]["source"] == "ASSUMED" else None
    report["data_source"] = data.get("source")
    (args.out / "carry_calibration.json").write_text(json.dumps(report, indent=2))
    md = markdown(report, data, report["assumed_layer_a_apr"])
    (args.out / "CARRY_CALIBRATION.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
