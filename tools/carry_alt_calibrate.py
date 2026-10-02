#!/usr/bin/env python3
"""carry_alt_calibrate.py — the 180-day measurement for altcoins
(CARRY_PLAN §13.10). Public data only (no API key). Separate report.

Candidates (every rule must hold; each exclusion is reported with its reason):
  1. in the top --top USDT perpetuals by 24h turnover (BTC/ETH excluded:
     they are the core symbols, measured by carry_calibrate.py);
  2. a spot pair on Bybit with the SAME symbol and base coin, Trading
     (1000PEPEUSDT-style perps whose spot is PEPEUSDT are excluded);
  3. counts as UTA collateral (tiered collateral ratio > 0);
  4. perp listed for at least 6 months (launchTime), and 180 days of funding.
  Stablecoins are not candidates. Anything unreadable excludes the coin.

Each candidate gets a GO of its OWN: the §2 criteria (excess over layer A
>= 3 pp, worst 30 days >= -0.5 %, robust to costs x 1.5), on 180 days, with
the lagged predictor and the thresholds of config/carry.yaml — the ones that
would trade it — never thresholds optimised per coin. The config refuses an
altcoin in SYMBOLS without that GO measured with its current thresholds.

  python tools/carry_alt_calibrate.py --out calibration                 # fetch + report
  python tools/carry_alt_calibrate.py --from-data FILE --out calibration # offline

Writes carry_alt_calibration.json, CARRY_CALIBRATION_ALTS.md and (when
fetching) carry_alt_data_<utc>.json with every raw number used.
Exit code: 0 report written (read the verdicts), 1 missing data.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402

import carry_calibrate as cal  # noqa: E402
from bybit_earn_tool import BybitAPIError  # noqa: E402
from carry import backtest as bt  # noqa: E402
from carry import config as cc  # noqa: E402

DAY_MS = cal.DAY_MS
MIN_LISTED_DAYS = 183          # "at least 6 months"
MIN_HISTORY_DAYS = 175         # funding actually present (of the 180 measured)
STABLECOINS = frozenset({"USDC", "USDE", "FDUSD", "DAI", "TUSD", "USDD", "PYUSD", "USDP",
                         "BUSD", "EUR", "EURS", "RLUSD", "USD1"})


# --------------------------------------------------------------------------- #
# Candidates                                                                   #
# --------------------------------------------------------------------------- #

def select_candidates(client, top: int, now_ms: int) -> Dict:
    ranked = []
    for t in client.get_linear_tickers():
        sym = str(t.get("symbol") or "")
        if not sym.endswith("USDT") or sym in cc.CORE_SYMBOLS:
            continue
        try:
            ranked.append((float(t["turnover24h"]), sym))
        except (KeyError, TypeError, ValueError):
            continue
    ranked.sort(reverse=True)
    order = [s for _, s in ranked]
    try:
        collateral, collateral_err = client.get_collateral_ratios(), None
    except BybitAPIError as e:
        collateral, collateral_err = None, str(e)

    candidates: List[str] = []
    excluded: Dict[str, str] = {}
    details: Dict[str, Dict] = {}
    for sym in order[:top]:
        why = _exclusion(client, sym, now_ms, collateral, collateral_err, details)
        if why:
            excluded[sym] = why
        else:
            candidates.append(sym)
    return {"ranked": order, "top": top, "candidates": candidates, "excluded": excluded,
            "details": details, "collateral_source": "/v5/spot-margin-trade/collateral"}


def _exclusion(client, sym, now_ms, collateral, collateral_err, details) -> Optional[str]:
    base = sym[:-len("USDT")]
    if base.upper() in STABLECOINS:
        return "stablecoin"
    try:
        lin = client.get_instrument("linear", sym)
    except BybitAPIError as e:
        return f"perp instrument unreadable: {e}"
    if lin.get("contractType") != "LinearPerpetual":
        return f"contractType {lin.get('contractType')!r}, not LinearPerpetual"
    if lin.get("status") != "Trading":
        return f"perp status {lin.get('status')!r}, not Trading"
    try:
        launch = int(lin["launchTime"])
    except (KeyError, TypeError, ValueError):
        return "launchTime unknown: listing age cannot be proven"
    age = (now_ms - launch) / DAY_MS
    if age < MIN_LISTED_DAYS:
        return f"listed {age:.0f} days < 6 months ({MIN_LISTED_DAYS})"
    try:
        spot = client.get_instrument("spot", sym)
    except BybitAPIError as e:
        return f"no spot pair {sym} on Bybit: {e}"
    if spot.get("status") != "Trading":
        return f"spot status {spot.get('status')!r}, not Trading"
    perp_base, spot_base = str(lin.get("baseCoin") or ""), str(spot.get("baseCoin") or "")
    if not perp_base or perp_base != spot_base:
        return f"spot base {spot_base!r} differs from perp base {perp_base!r}"
    if collateral is None:
        return f"collateral unknown: {collateral_err}"
    ratio = collateral.get(spot_base.upper())
    if not ratio or ratio <= 0:
        return "not UTA collateral"
    details[sym] = {"listed_days": round(age), "collateral_ratio": ratio}
    return None


def fetch(top: int, days: int) -> Dict:
    from carry.client import CarryPublicClient

    client = CarryPublicClient()
    end = int(time.time() * 1000)
    start = end - days * DAY_MS
    selection = select_candidates(client, top, end)
    data = {"fetched_at_ms": end, "days": days, "source": f"bybit {client.base_url}",
            "selection": selection, "symbols": {}, "layer_a": {}}
    for sym in selection["candidates"]:
        data["symbols"][sym] = {
            "funding": client.get_funding_history(sym, start, end),
            "instrument_linear": client.get_instrument("linear", sym),
            "instrument_spot": client.get_instrument("spot", sym),
        }
    pid, raw = client.get_usdt_flexible_apr_history(start_ms=start, end_ms=end)
    points = sorted((int(r["timestamp"]), apr) for r in raw
                    if isinstance(r, dict) and r.get("timestamp") is not None
                    and (apr := cal._parse_apr(r.get("apr"))) is not None)
    data["layer_a"] = {"source": f"USDT FlexibleSaving productId {pid}", "points": points}
    return data


# --------------------------------------------------------------------------- #
# A GO of its own per altcoin                                                  #
# --------------------------------------------------------------------------- #

def calibrate_alts(data: Dict, cfg: Dict) -> Dict:
    thresholds = {k: cfg[k] for k in cc.GO_THRESHOLD_KEYS}
    # Funding history has no order book: basis/spread are not modelled (as in 0B).
    params = dataclasses.replace(cc.to_params(cfg), max_entry_basis_bps=None,
                                 max_favorable_basis_bps=None, max_spread_bps=None)
    layer_a = data["layer_a"]["points"]
    per: Dict[str, Dict] = {}
    for sym, d in data["symbols"].items():
        funding = [tuple(x) for x in d["funding"]]
        covered = (funding[-1][0] - funding[0][0]) / DAY_MS if len(funding) > 1 else 0.0
        if covered < MIN_HISTORY_DAYS:
            per[sym] = {"verdict": {"go": False,
                                    "reason": f"funding history covers {covered:.0f} days "
                                              f"< {MIN_HISTORY_DAYS}"}}
            continue
        base = bt.simulate(funding, layer_a, params, symbol=sym)
        up = bt.simulate(funding, layer_a, params, cost_multiplier=1.5, symbol=sym)
        oracle = bt.simulate(funding, layer_a, params, predictor="oracle", symbol=sym)
        mid = funding[0][0] + (funding[-1][0] - funding[0][0]) // 2
        oos = bt.simulate(funding, layer_a, params, symbol=sym, eval_from_ms=mid)
        v = bt.go_check(base, up, **cal.GO)
        failed = [name for name, ok in (("excess", v["excess_ok"]), ("worst 30d", v["worst_30d_ok"]),
                                        ("costs x1.5", v["robust_to_costs"])) if not ok]
        v["reason"] = "all §2 criteria met" if v["go"] else "fails: " + ", ".join(failed)
        per[sym] = {"verdict": v, "lagged": base.summary(), "costs_x1_5": up.summary(),
                    "oracle_excess_apr": oracle.excess_apr, "second_half": oos.summary(),
                    "covered_days": round(covered, 1)}
    return {"thresholds": thresholds, "per_symbol": per,
            "go_symbols": sorted(s for s, r in per.items() if r["verdict"]["go"])}


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #

def markdown(report: Dict, data: Dict) -> str:
    sel = data.get("selection", {})
    when = datetime.fromtimestamp(data["fetched_at_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    p = cal._pct
    lines = [
        "# CARRY_CALIBRATION_ALTS — altcoins (§13.10)", "",
        f"Πηγή: **{data.get('source')}**, λήψη {when}, {data.get('days')} ημέρες. "
        f"Layer A: {data['layer_a'].get('source')}.", "",
        f"Κάθε altcoin κρίνεται **μόνο του** με τα κριτήρια της §2 και τα κατώφλια του "
        f"`config/carry.yaml` (όχι βελτιστοποιημένα ανά νόμισμα). Κανένα altcoin στο "
        f"`SYMBOLS` χωρίς δικό του GO· το config το επιβάλλει.", "",
        f"## Υποψήφια (top {sel.get('top')} USDT perps σε όγκο 24ώρου, εκτός BTC/ETH)", "",
        "| Σύμβολο | Αποτέλεσμα |", "|---|---|",
    ]
    for sym in sel.get("ranked", [])[:sel.get("top", 0)]:
        if sym in sel.get("excluded", {}):
            lines.append(f"| {sym} | ✗ {sel['excluded'][sym]} |")
        else:
            d = sel.get("details", {}).get(sym, {})
            lines.append(f"| {sym} | ✓ υποψήφιο (εισηγμένο {d.get('listed_days')} ημ., "
                         f"collateral ratio {d.get('collateral_ratio')}) |")
    lines += ["", f"Collateral από `{sel.get('collateral_source')}` (δημόσιο· το σχήμα του "
              f"δεν έχει επιβεβαιωθεί — αν δεν διαβαστεί, κανένα υποψήφιο).", "",
              "## GO ανά altcoin (180 ημ., lagged predictor)", "",
              "| Σύμβολο | Απόφαση | Υπεροχή | Χειρ. 30ήμ. | Υπεροχή ×1,5 | Είσοδοι | Χρόνος σε θέση | "
              "Αρνητικό funding | 2ο μισό: υπεροχή | Oracle |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for sym, r in report["per_symbol"].items():
        v = r["verdict"]
        mark = "**GO**" if v["go"] else "NO-GO"
        if "lagged" not in r:
            lines.append(f"| {sym} | {mark} — {v['reason']} | — | — | — | — | — | — | — | — |")
            continue
        lg = r["lagged"]
        lines.append(f"| {sym} | {mark} ({v['reason']}) | {p(v['excess_apr'])} | "
                     f"{p(v['worst_30d_return'])} | {p(v['excess_apr_costs_x1_5'])} | "
                     f"{lg['entries']} | {p(lg['time_in_position'])} | {p(lg['negative_share'])} | "
                     f"{p(r['second_half']['excess_apr'])} | {p(r['oracle_excess_apr'])} |")
    if not report["per_symbol"]:
        lines.append("| — | κανένα υποψήφιο | | | | | | | | |")
    lines += ["", f"**Με GO:** {', '.join(report['go_symbols']) or 'κανένα'}.", "",
              "## Κατώφλια της μέτρησης (`config/carry.yaml`)", "", "```yaml",
              *[cal._yaml_line(k, v) for k, v in report["thresholds"].items()], "```", "",
              "## Τι ΔΕΝ μοντελοποιείται", "",
              "- Basis, spread, slippage — στα altcoins συνήθως μεγαλύτερα· η ευαισθησία ×1,5 είναι το υποκατάστατο.",
              "- ADL και ρευστοποίηση: στα altcoins πιθανότερα (§5 R11, R13)· γι' αυτό `MAX_NOTIONAL_PER_ALT_USD` < `MAX_NOTIONAL_PER_SYMBOL_USD`.",
              "- Delisting και αλλαγή collateral ratio (R16, R28): ελέγχονται σε κάθε κύκλο, όχι εδώ.",
              "- Layer A όπως στο `CARRY_CALIBRATION.md` (ίδιο ιστορικό APR, ίδια περιορισμένη κάλυψη)."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--from-data", type=Path, help="replay a saved carry_alt_data_*.json (offline)")
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "carry.yaml")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "carry")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load(args.config.read_text())

    if args.from_data:
        data = json.loads(args.from_data.read_text())
    else:
        data = fetch(args.top, args.days)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (args.out / f"carry_alt_data_{stamp}.json").write_text(json.dumps(data))
    if data["symbols"] and not data["layer_a"].get("points"):
        print("ERROR: no layer A (Easy Earn USDT) APR history; unknown is not zero.")
        return 1

    report = calibrate_alts(data, cfg)
    report["selection"] = data.get("selection")
    report["data_source"] = data.get("source")
    report["fetched_at_ms"] = data.get("fetched_at_ms")
    (args.out / "carry_alt_calibration.json").write_text(json.dumps(report, indent=2))
    md = markdown(report, data)
    (args.out / "CARRY_CALIBRATION_ALTS.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
