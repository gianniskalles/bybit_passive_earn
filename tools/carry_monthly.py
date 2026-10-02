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
  3. sends a short Telegram report.

It never changes the config: a threshold changes only by a decision of
Giannis, in git (decision 13.1).

  python tools/carry_monthly.py                        # fetch + report + Telegram
  python tools/carry_monthly.py --from-data FILE --no-telegram   # offline
Exit code: 0 report written (and sent, unless --no-telegram), 1 otherwise.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
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


def shipped_params(cfg: Dict):
    return dataclasses.replace(cc.to_params(cfg), max_entry_basis_bps=None, max_spread_bps=None,
                               max_favorable_basis_bps=None)


def replay_shipped(data: Dict, cfg: Dict) -> Dict[str, Dict]:
    p = shipped_params(cfg)
    return {sym: bt.simulate([tuple(x) for x in d["funding"]], data["layer_a"]["points"], p,
                             symbol=sym).summary()
            for sym, d in sorted(data["symbols"].items())}


def _pct(x: Optional[float]) -> str:
    return "?" if x is None else f"{x * 100:+.2f}%"


def telegram_text(month: str, report: Dict, shipped: Dict[str, Dict], out: Path) -> str:
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
    lines.append(f"Earn (layer A) mean APR: {_pct(report['full_period_lagged']['layer_a_apr'])}")
    lines.append(f"report: {out / 'CARRY_CALIBRATION.md'} — the config is NOT changed")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-data", type=Path, help="replay a saved carry_data_*.json (offline)")
    ap.add_argument("--out", type=Path, default=None,
                    help="report directory (default <hermes home>/reports/carry/<YYYY-MM>)")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(settings.carry_config_file().read_text())
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    out = args.out or settings.hermes_home() / "reports" / "carry" / month
    calib_args = ["--symbols", ",".join(cfg["SYMBOLS"]), "--out", str(out)]
    if args.from_data:
        calib_args += ["--from-data", str(args.from_data)]
    if carry_calibrate.main(calib_args) != 0:
        text = f"🚨 Carry — monthly measurement {month} FAILED (missing data); see the timer log"
        report = None
    else:
        report = json.loads((out / "carry_calibration.json").read_text())
        data_file = args.from_data or sorted(out.glob("carry_data_*.json"))[-1]
        shipped = replay_shipped(json.loads(Path(data_file).read_text()), cfg)
        report["shipped_thresholds"] = shipped
        (out / "carry_calibration.json").write_text(json.dumps(report, indent=2))
        text = telegram_text(month, report, shipped, out)
    print(text)
    if args.no_telegram:
        return 0 if report else 1
    from notify import Notifier
    sent = Notifier(settings.load_env(), cfg).event(text)
    return 0 if report and sent else 1


if __name__ == "__main__":
    sys.exit(main())
