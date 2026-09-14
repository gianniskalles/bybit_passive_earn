#!/usr/bin/env python3
"""Regression test runner for the yield rotation prompt.

For each fixture, compose the prompt with the fixture's inputs (the
"wrapper view": verified risk_state + filtered scan), call hermes chat,
parse JSON, validate against the expected behaviour, repeat N times.
Report pass/fail per fixture and overall pass rate.

Usage:
  run_regression.py [--runs 5] [--fixtures 01_baseline,...] [--prompt PATH]
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Make tests/ importable
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from tests.fixtures import ALL_FIXTURES, get_fixture  # noqa: E402

PROMPT_PATH_DEFAULT = Path("/opt/hermes/yield_rotation/prompt_v5.md")
# hermes CLI binary path. /usr/local/bin/hermes is the gateway CLI
# (hermes george, hermes seo). The agent is at /opt/hermes/.venv/bin/hermes.
HERMES_BIN = "/opt/hermes/.venv/bin/hermes"
DEFAULT_RUNS = 5
DEFAULT_TIMEOUT = 180  # seconds, per the spec


def strip_otel(stdout: str) -> str:
    """Remove OpenTelemetry tracing banner from stdout."""
    # Look for the first line that starts with '{' (the JSON object)
    for line in stdout.splitlines():
        s = line.strip()
        if s.startswith("{"):
            return stdout[stdout.index(line):]
    return stdout


def extract_json(stdout: str) -> dict | None:
    """Extract JSON object from stdout, stripping any prose/fences.

    Strategy: find the FIRST top-level balanced JSON object. Skip
    the OpenTelemetry banner, which itself may contain empty {}
    or quoted JSON snippets. The agent's real output is always
    one balanced object at the end.
    """
    s = strip_otel(stdout)
    s = re.sub(r"^```(?:json)?\s*", "", s.strip())
    s = re.sub(r"\s*```$", "", s)
    # Walk the string, find the first { that starts a balanced object.
    # Reject trivial {} so we don't grab an OTEL placeholder.
    pos = 0
    best = None
    while True:
        start = s.find("{", pos)
        if start < 0:
            break
        depth = 0
        end = -1
        in_string = False
        escape = False
        for i in range(start, len(s)):
            c = s[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            break
        candidate = s[start:end]
        if candidate.strip() == "{}":
            # Empty object from OTEL banner; skip and keep looking.
            pos = end
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Truncated or malformed; skip and keep looking.
            pos = end
            continue
    return None


def compose_prompt(prompt_template: str, fixture: dict) -> str:
    """Append the fixture's wrapper view to the prompt template.

    Wrapper view: the inputs the agent actually receives. Risk_state is
    already verified, snapshot freshness already applied, config nulls
    already flagged with CONFIG_INCOMPLETE.
    """
    risk_state = fixture["risk_state"]
    wrapper_alerts = []
    if not risk_state.get("verified", True):
        wrapper_alerts.append(f"RISK_STATE_UNVERIFIED ({risk_state.get('failure_reason', 'unknown')})")

    config = fixture["config"]
    config_alerts = []
    for k in ("ENTRY_APR", "EXIT_APR", "MIN_APR_EDGE"):
        if config.get(k) is None:
            config_alerts.append(f"CONFIG_INCOMPLETE (missing {k})")
    wrapper_alerts.extend(config_alerts)

    # If the fixture specifies that the wrapper dropped products due to
    # stale APR-history data, add a STALE_HISTORY alert so the agent can echo it.
    if fixture.get("wrapper_drops") == "STALE_HISTORY":
        wrapper_alerts.append("STALE_HISTORY (apr_history_age_seconds > 4h)")

    inputs_block = {
        "wrapper_alerts": wrapper_alerts,
        "config": config,
        "risk_state": risk_state,
        "positions": fixture["positions"],
        "scan": fixture["scan"],
    }
    return (
        f"{prompt_template}\n\n---\n\n"
        f"**Cycle inputs (wrapper view):**\n\n"
        f"```json\n{json.dumps(inputs_block, indent=2, ensure_ascii=False)}\n```\n"
    )


def call_hermes(prompt: str, timeout: int) -> tuple[int, str, str, float]:
    """Call hermes chat with the prompt, return (exit, stdout, stderr, elapsed)."""
    start = time.time()
    try:
        proc = subprocess.run(
            [HERMES_BIN, "chat", "--query-file", "/dev/stdin", "-Q",
             "--toolsets=", "--reasoning", "medium"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.time() - start
        return proc.returncode, proc.stdout, proc.stderr, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return -1, "", f"TIMEOUT after {timeout}s", elapsed


def _decision_matches(exp: dict, got: dict) -> list:
    """Return list of failures (empty if match)."""
    fails = []
    for k, v in exp.items():
        if k == "any_coin":
            # Global actions (REDEEM_ALL, NO_NEW_POSITIONS) are
            # valid without a coin field.
            if v and not got.get("coin"):
                action = got.get("action", "")
                if action not in ("REDEEM_ALL", "NO_NEW_POSITIONS", "ALERT_ONLY"):
                    fails.append(f"decision must have a coin field; got {got!r}")
            continue
        if k == "product_id_any":
            if v and got.get("product_id") not in v and got.get("from_product_id") not in v:
                fails.append(f"decision product_id/from_product_id must be in {v!r}; got {got!r}")
            continue
        if got.get(k) != v:
            fails.append(f"decision.{k}: expected {v!r}, got {got.get(k)!r}")
    return fails


def validate(parsed: dict, expected: dict) -> tuple[bool, list]:
    """Check parsed output against expected; return (ok, list of failures)."""
    fails = []
    # 1. decisions match
    exp_decisions = expected.get("decisions", [])
    got_decisions = parsed.get("decisions", [])
    if len(exp_decisions) != len(got_decisions):
        fails.append(f"decisions count: expected {len(exp_decisions)}, "
                     f"got {len(got_decisions)}")
    else:
        for i, (exp, got) in enumerate(zip(exp_decisions, got_decisions)):
            fails.extend(_decision_matches(exp, got))
    # 2. must_hold_reason_contains
    holds_text = " ".join(parsed.get("holds", []))
    for needle in expected.get("must_hold_reason_contains", []):
        if needle not in holds_text:
            fails.append(f"holds missing {needle!r}; got: {holds_text!r}")
    # 3. risk_state echo
    if "risk_state_echo" in expected:
        if parsed.get("risk_state") != expected["risk_state_echo"]:
            fails.append(f"risk_state: expected {expected['risk_state_echo']!r}, "
                         f"got {parsed.get('risk_state')!r}")
    # 4. must_alert_contain
    alerts_text = " ".join(parsed.get("alerts", []))
    for needle in expected.get("must_alert_contain", []):
        if needle not in alerts_text:
            fails.append(f"alerts missing {needle!r}; got: {alerts_text!r}")
    return (len(fails) == 0, fails)


def run_fixture(fixture: dict, prompt: str, runs: int, timeout: int) -> dict:
    """Run a single fixture N times, return pass/fail summary."""
    full_prompt = compose_prompt(prompt, fixture)
    passes = 0
    failures = []
    for run_idx in range(1, runs + 1):
        exit_code, stdout, stderr, elapsed = call_hermes(full_prompt, timeout)
        parsed = extract_json(stdout)
        if parsed is None:
            failures.append({"run": run_idx, "elapsed": elapsed,
                             "error": "JSON parse failed",
                             "stdout_head": stdout[:300],
                             "stderr_head": stderr[:200]})
            continue
        ok, fails = validate(parsed, fixture["expected"])
        if ok:
            passes += 1
        else:
            failures.append({"run": run_idx, "elapsed": elapsed,
                             "fails": fails, "parsed": parsed})
    return {
        "name": fixture["name"],
        "description": fixture["description"],
        "passes": passes,
        "total": runs,
        "failures": failures,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--prompt", type=Path, default=PROMPT_PATH_DEFAULT)
    ap.add_argument("--fixtures", default=None,
                    help="comma-separated fixture names; default = all")
    ap.add_argument("--out", type=Path,
                    default=Path("/tmp/smoke/regression_results.json"))
    args = ap.parse_args()

    if not args.prompt.exists():
        print(f"FATAL: prompt file not found: {args.prompt}", file=sys.stderr)
        return 2

    prompt = args.prompt.read_text()

    if args.fixtures:
        names = [n.strip() for n in args.fixtures.split(",")]
        fixtures = [get_fixture(n) for n in names]
    else:
        fixtures = ALL_FIXTURES

    print(f"Running {len(fixtures)} fixtures × {args.runs} runs each "
          f"= {len(fixtures) * args.runs} cycles\n", file=sys.stderr)

    results = []
    total_passes = 0
    total_runs = 0
    for fx in fixtures:
        print(f"=== {fx['name']} ===", file=sys.stderr)
        r = run_fixture(fx, prompt, args.runs, args.timeout)
        status = "PASS" if r["passes"] == r["total"] else "FAIL"
        print(f"  {status}: {r['passes']}/{r['total']}", file=sys.stderr)
        if r["failures"]:
            for f in r["failures"][:2]:
                print(f"    run {f['run']}: {f.get('fails') or f.get('error')}",
                      file=sys.stderr)
        results.append(r)
        total_passes += r["passes"]
        total_runs += r["total"]

    summary = {
        "prompt": str(args.prompt),
        "runs_per_fixture": args.runs,
        "total_cycles": total_runs,
        "total_passes": total_passes,
        "pass_rate": round(total_passes / total_runs, 3) if total_runs else 0,
        "fixtures": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n=== SUMMARY: {total_passes}/{total_runs} cycles passed "
          f"({summary['pass_rate']*100:.0f}%) ===", file=sys.stderr)
    print(f"Detailed results -> {args.out}", file=sys.stderr)
    return 0 if total_passes == total_runs else 1


if __name__ == "__main__":
    sys.exit(main())
