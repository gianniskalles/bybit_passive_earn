#!/usr/bin/env python3
"""carry_monthly.py — the monthly re-measurement (decision 13.7), run by
yield-carry-calibrate.timer.

Public data only (no key). Fetches 180 days of funding for the SYMBOLS of
config/carry.yaml and the layer A APR history, then:
  1. runs the Phase 0B calibration (tools/carry_calibrate.py: grid, in/out of
     sample, §2 GO criteria) into <reports>/<YYYY-MM>/;
  2. replays the SHIPPED thresholds of config/carry.yaml on the same data
     (decide.py through carry/backtest.py, basis/spread off as in the
     calibration: funding history has no order book);
  3. measures the REFERENCE coins (30/10: LINK, HBAR, DOGE, NEAR) — for
     reference only, never in SYMBOLS: each gets the altcoin GO of
     tools/carry_alt_calibrate.py (eligibility of §13.10, then the §2
     criteria with the thresholds of config/carry.yaml), one line each in
     the report;
  4. sends a short Telegram report, and a SEPARATE message for every
     reference coin that is GO this month and was GO last month too (the
     previous month's carry_calibration.json next to this one).

It never changes the config: a threshold changes only by a decision of
Giannis, in git (decision 13.1); a reference coin enters SYMBOLS only by
his decision, with its GO in calibration/carry_alt_calibration.json (13.10).

  python tools/carry_monthly.py                        # fetch + report + Telegram
  python tools/carry_monthly.py --from-data FILE --no-telegram   # offline
  python tools/carry_monthly.py --from-data FILE --reference-data REF   # offline, with reference
Exit code: 0 report written (and sent, unless --no-telegram), 1 otherwise.
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import settings  # noqa: E402
from carry import backtest as bt  # noqa: E402
from carry import config as cc  # noqa: E402
from tools import carry_calibrate  # noqa: E402

# Measured every month for reference; never traded (not in SYMBOLS).
REFERENCE_SYMBOLS = ("LINKUSDT", "HBARUSDT", "DOGEUSDT", "NEARUSDT")


def shipped_params(cfg: Dict):
    return dataclasses.replace(cc.to_params(cfg), max_entry_basis_bps=None, max_spread_bps=None,
                               max_favorable_basis_bps=None)


def replay_shipped(data: Dict, cfg: Dict) -> Dict[str, Dict]:
    p = shipped_params(cfg)
    return {sym: bt.simulate([tuple(x) for x in d["funding"]], data["layer_a"]["points"], p,
                             symbol=sym).summary()
            for sym, d in sorted(data["symbols"].items())}


# --------------------------------------------------------------------------- #
# reference coins                                                               #
# --------------------------------------------------------------------------- #

def fetch_reference(symbols=REFERENCE_SYMBOLS, days: int = 180, client=None,
                    now_ms: Optional[int] = None) -> Dict:
    """Public data only. Each coin: the eligibility of §13.10 (perp, spot pair,
    UTA collateral, listed >= 6 months), then its funding history. A coin that
    cannot be read is reported as such, never dropped."""
    from bybit_earn_tool import BybitAPIError
    from tools import carry_alt_calibrate as alt
    if client is None:
        from carry.client import CarryPublicClient
        client = CarryPublicClient()
    end = int(time.time() * 1000) if now_ms is None else now_ms
    start = end - days * alt.DAY_MS
    try:
        collateral, collateral_err = client.get_collateral_ratios(), None
    except BybitAPIError as e:
        collateral, collateral_err = None, str(e)
    data: Dict = {"fetched_at_ms": end, "days": days, "symbols": {}, "excluded": {},
                  "unreadable": {}, "details": {}}
    for sym in symbols:
        try:
            why = alt._exclusion(client, sym, end, collateral, collateral_err, data["details"])
            if why:
                data["excluded"][sym] = why
                continue
            data["symbols"][sym] = {"funding": client.get_funding_history(sym, start, end)}
        except Exception as e:                                      # noqa: BLE001
            data["unreadable"][sym] = f"{type(e).__name__}: {e}"
    return data


def measure_reference(ref: Dict, layer_a_points, cfg: Dict) -> Dict[str, Dict]:
    """One result per reference coin: {go, status, reason, excess_apr,
    worst_30d_return}."""
    from tools import carry_alt_calibrate as alt
    out: Dict[str, Dict] = {}
    for sym, why in ref.get("excluded", {}).items():
        out[sym] = {"go": False, "status": "excluded", "reason": why}
    for sym, why in ref.get("unreadable", {}).items():
        out[sym] = {"go": False, "status": "unreadable", "reason": why}
    if ref.get("symbols"):
        per = alt.calibrate_alts({"symbols": ref["symbols"],
                                  "layer_a": {"points": layer_a_points}}, cfg)["per_symbol"]
        for sym, r in per.items():
            v = r["verdict"]
            out[sym] = {"go": bool(v["go"]), "status": "measured", "reason": v["reason"],
                        "excess_apr": v.get("excess_apr"),
                        "worst_30d_return": v.get("worst_30d_return")}
    return {sym: out[sym] for sym in sorted(out, key=lambda x: (
        list(REFERENCE_SYMBOLS).index(x) if x in REFERENCE_SYMBOLS else 99, x))}


def reference_line(sym: str, r: Dict) -> str:
    if r["status"] != "measured":
        return f"  {sym}: NO-GO — {r['status']}: {r['reason']}"
    return (f"  {sym}: {'GO' if r['go'] else 'NO-GO'} — excess over Earn "
            f"{_pct(r.get('excess_apr'))}, worst 30 days {_pct(r.get('worst_30d_return'))}"
            + ("" if r["go"] else f" ({r['reason']})"))


def previous_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def go_twice(month: str, reference: Dict[str, Dict], reports_dir: Path) -> List[str]:
    """Reference coins GO this month AND in last month's report."""
    try:
        prev = json.loads((reports_dir / previous_month(month) / "carry_calibration.json")
                          .read_text()).get("reference") or {}
    except (OSError, ValueError):
        return []
    return [s for s, r in reference.items() if r.get("go") and (prev.get(s) or {}).get("go")]


def go_twice_text(month: str, symbols: List[str]) -> str:
    return (f"🔔 Carry — reference coin(s) GO two months in a row ({previous_month(month)}, "
            f"{month}): {', '.join(symbols)}. For reference only: not traded. Into SYMBOLS only "
            f"by Giannis's decision, with its own GO in calibration/carry_alt_calibration.json "
            f"(13.10) and its own limit.")


def _pct(x: Optional[float]) -> str:
    return "?" if x is None else f"{x * 100:+.2f}%"


def telegram_text(month: str, report: Dict, shipped: Dict[str, Dict], out: Path,
                  reference: Optional[Dict[str, Dict]] = None) -> str:
    v = report["verdict"]
    lines = [f"📐 Carry — monthly measurement {month} ({report['period']['days']} days)",
             f"calibration grid: {'GO' if v['go'] else 'NO-GO'} — "
             f"{report['grid']['passing_go']}/{report['grid']['size']} sets pass §2; "
             f"best excess over Earn {_pct(v['excess_apr'])}, worst 30 days "
             f"{_pct(v['worst_30d_return'])}",
             "shipped thresholds (config/carry.yaml) on the same data:"]
    for sym, r in shipped.items():
        lines.append(f"  {sym}: entries {r['entries']}, exits {r['exits']}, in position "
                     f"{r['time_in_position'] * 100:.0f}% of the time, excess {_pct(r['excess_apr'])}, "
                     f"worst 30 days {_pct(r['worst_30d_return'])}")
    if reference is not None:
        lines.append("reference coins (not in SYMBOLS, never traded):")
        lines.extend(reference_line(sym, r) for sym, r in reference.items())
    lines.append(f"Earn (layer A) mean APR: {_pct(report['full_period_lagged']['layer_a_apr'])}")
    lines.append(f"report: {out / 'CARRY_CALIBRATION.md'} — the config is NOT changed")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-data", type=Path, help="replay a saved carry_data_*.json (offline)")
    ap.add_argument("--month", help="YYYY-MM of the report (default: now, UTC)")
    ap.add_argument("--out", type=Path, default=None,
                    help="report directory (default <hermes home>/reports/carry/<YYYY-MM>)")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--reference-data", type=Path,
                    help="a saved carry_reference_data_*.json (offline); with --from-data and "
                         "without it, the reference coins are not measured")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(settings.carry_config_file().read_text())
    month = args.month or datetime.now(timezone.utc).strftime("%Y-%m")
    out = args.out or settings.hermes_home() / "reports" / "carry" / month
    calib_args = ["--symbols", ",".join(cfg["SYMBOLS"]), "--out", str(out)]
    if args.from_data:
        calib_args += ["--from-data", str(args.from_data)]
    twice: List[str] = []
    if carry_calibrate.main(calib_args) != 0:
        text = f"🚨 Carry — monthly measurement {month} FAILED (missing data); see the timer log"
        report = None
    else:
        report = json.loads((out / "carry_calibration.json").read_text())
        data_file = args.from_data or sorted(out.glob("carry_data_*.json"))[-1]
        shipped = replay_shipped(json.loads(Path(data_file).read_text()), cfg)
        report["shipped_thresholds"] = shipped
        reference = None
        if args.reference_data:
            ref = json.loads(args.reference_data.read_text())
        elif args.from_data:
            ref = None                                       # offline: nothing fetched
        else:
            ref = fetch_reference()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (out / f"carry_reference_data_{stamp}.json").write_text(json.dumps(ref))
        if ref is not None:
            layer_a = json.loads(Path(data_file).read_text())["layer_a"]["points"]
            reference = measure_reference(ref, layer_a, cfg)
            report["reference"] = reference
        (out / "carry_calibration.json").write_text(json.dumps(report, indent=2))
        text = telegram_text(month, report, shipped, out, reference)
        twice = go_twice(month, reference, out.parent) if reference else []
    print(text)
    alert = go_twice_text(month, twice) if report and twice else None
    if alert:
        print(alert)
    if args.no_telegram:
        return 0 if report else 1
    from notify import Notifier
    notifier = Notifier(settings.load_env(), cfg)
    sent = notifier.event(text)
    if alert:
        sent = notifier.event(alert) and sent
    return 0 if report and sent else 1


if __name__ == "__main__":
    sys.exit(main())
