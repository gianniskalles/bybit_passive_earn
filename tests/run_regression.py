#!/usr/bin/env python3
"""LLM regression — runs the PRODUCTION cycle against the real model.

Each scenario (tests/regression_fixtures.py) replays recorded Bybit
responses through the real BybitEarnTool and calls run_yield_cycle.run_cycle
with the production call_agent: same prompt file, same compose_prompt, same
`hermes chat` command line (build_agent_command), same extract_json and
validation, thresholds from the real config/yield_rotation.yaml.  Nothing
here re-implements the production path, so the prompt the model sees is
byte for byte the prompt production would send for the same data.

Only decision quality is measured (T4.4). Deterministic behaviour is
covered by the wrapper unit tests (`pytest`).

Runs only where the agent CLI exists (the VPS):

  /opt/hermes/venvs/yield_rotation/bin/python tests/run_regression.py [--runs 5] \
      [--fixtures 01_stake_when_rate_qualifies,...] [--out results.json]

The run is isolated: its own risk_state file, HMAC key, log dir and
session dir under a temporary directory. DRY_RUN is forced on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import risk_state  # noqa: E402
import run_yield_cycle as ryc  # noqa: E402
import settings  # noqa: E402
from regression_fixtures import get, scenarios  # noqa: E402
from replay import replay_tool  # noqa: E402

DEFAULT_RUNS = 5
REGRESSION_KEY = "regression-only-key"


def regression_config(workdir: Path) -> Dict[str, Any]:
    """The production config, with only the isolation knobs changed."""
    cfg = ryc.load_config(settings.config_file())
    cfg["LOG_DIR"] = str(workdir / "logs")
    cfg["DRY_RUN"] = True
    # Balances come from the scenario's recorded wallet, not a simulation.
    cfg["SIMULATED_IDLE_BALANCE"] = None
    return cfg


def isolate(workdir: Path) -> None:
    """Point state, sessions and the HMAC key at `workdir` (process env wins
    over the .env files in settings.load_env)."""
    os.environ["YIELD_STATE_FILE"] = str(workdir / "risk_state.json")
    os.environ["YIELD_SESSION_DIR"] = str(workdir / "sessions")
    os.environ["HERMES_RISK_HMAC_KEY"] = REGRESSION_KEY


def run_once(scenario: Dict[str, Any], cfg: Dict[str, Any], agent: Callable) -> Dict[str, Any]:
    """One production cycle for `scenario`. Returns the record plus what the
    agent saw and said."""
    risk_state.write(Path(os.environ["YIELD_STATE_FILE"]), REGRESSION_KEY,
                     scenario["risk_state"], "regression", risk_state.SOURCE_OPERATOR)
    seen: Dict[str, Any] = {}

    def capturing(prompt, cfg_, cycle_id):
        seen["prompt"] = prompt
        raw, session_id = agent(prompt, cfg_, cycle_id)
        seen["raw"] = raw
        return raw, session_id

    rec, rc = ryc.run_cycle(cfg, tool=replay_tool(scenario["payload"]), agent=capturing)
    agent_decisions: Optional[List[Dict]] = None
    if "raw" in seen:
        try:
            agent_decisions = ryc.extract_json(seen["raw"], rec["cycle_id"])["decisions"]
        except ValueError:
            agent_decisions = None
    return {"record": rec, "rc": rc, "prompt": seen.get("prompt"), "raw": seen.get("raw"),
            "agent_decisions": agent_decisions}


def evaluate(scenario: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    """Return the list of failures (empty = pass)."""
    decisions = result["agent_decisions"]
    if decisions is None:
        return [f"no parseable agent output; alerts={result['record']['alerts']}"]
    fails = []
    stakes = sorted(str(d.get("product_id")) for d in decisions
                    if isinstance(d, dict) and d.get("action") == "STAKE")
    want = sorted(scenario["expect"]["stake"])
    if stakes != want:
        fails.append(f"STAKE product ids: expected {want}, got {stakes}")
    return fails


def run_scenario(scenario, cfg, agent, runs: int) -> Dict[str, Any]:
    passes, failures = 0, []
    for i in range(1, runs + 1):
        result = run_once(scenario, cfg, agent)
        fails = evaluate(scenario, result)
        if fails:
            failures.append({"run": i, "fails": fails,
                             "agent_decisions": result["agent_decisions"],
                             "raw_head": (result["raw"] or "")[:400]})
        else:
            passes += 1
    return {"name": scenario["name"], "description": scenario["description"],
            "passes": passes, "total": runs, "failures": failures}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--fixtures", default=None, help="comma-separated names; default = all")
    ap.add_argument("--out", type=Path, default=None, help="JSON report path")
    args = ap.parse_args(argv)

    selected = ([get(n.strip()) for n in args.fixtures.split(",")]
                if args.fixtures else scenarios())
    with tempfile.TemporaryDirectory(prefix="yr-regression-") as tmp:
        workdir = Path(tmp)
        isolate(workdir)
        cfg = regression_config(workdir)
        print(f"model={cfg['RESOLVED_MODEL']} prompt=prompt_{cfg['PROMPT_VERSION']}.md "
              f"cmd={' '.join(ryc.build_agent_command(cfg))}", file=sys.stderr)
        results = []
        for sc in selected:
            r = run_scenario(sc, cfg, ryc.call_agent, args.runs)
            print(f"{'PASS' if r['passes'] == r['total'] else 'FAIL'} "
                  f"{r['passes']}/{r['total']}  {sc['name']}", file=sys.stderr)
            for f in r["failures"][:2]:
                print(f"    run {f['run']}: {f['fails']}", file=sys.stderr)
            results.append(r)

    total = sum(r["total"] for r in results)
    passed = sum(r["passes"] for r in results)
    report = {"model": cfg["RESOLVED_MODEL"], "prompt_version": cfg["PROMPT_VERSION"],
              "runs_per_fixture": args.runs, "total": total, "passed": passed,
              "fixtures": results}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"=== {passed}/{total} passed ===", file=sys.stderr)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
