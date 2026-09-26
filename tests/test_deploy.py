"""deploy/deploy.sh and deploy/preflight.py — the one-command deploy.

preflight.py holds every check (tested directly here). deploy.sh only
orchestrates; it is run here with stub `systemctl`, `crontab`, `git`,
`systemd-analyze`, `hermes` and `python` on PATH, which log every call, so
the tests can prove: PASS/FAIL per step, stop at the first FAIL, a full log
file, idempotence, and that no unit outside yield-* is ever touched and
testnet is never run.
"""

import json
import re
import os
import stat
import sys as _sys
import subprocess
import sys
import time
from pathlib import Path

import pytest

import risk_state
import settings
from helpers import KEY, FakeAgent, FakeBybit, load_cfg, reply_with, write_state

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "deploy"))
import preflight  # noqa: E402

SMOKE = "smoke-test-only-do-not-use-in-prod"


def _write_env(path: Path, **kv):
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))


# =========================================================================== #
# preflight.py                                                                 #
# =========================================================================== #

# --- keys: fingerprints only ---------------------------------------------- #

def test_keys_replaces_smoke_test_hmac_and_prints_only_fingerprints(isolated_paths, capsys):
    prof = Path(isolated_paths["YIELD_ENV_FILE"])
    _write_env(prof, HERMES_RISK_HMAC_KEY=SMOKE, BYBIT_API_KEY="bk-123456",
               BYBIT_API_SECRET="bs-abcdef", OTHER="keep-me")
    _write_env(Path(isolated_paths["YIELD_SHARED_ENV_FILE"]), TELEGRAM_BOT_TOKEN="tg-999")
    assert preflight.main(["keys"]) == 0
    out = capsys.readouterr().out
    env = settings.load_env_file(prof)
    new = env["HERMES_RISK_HMAC_KEY"]
    assert new != SMOKE and len(new) == 64
    assert env["OTHER"] == "keep-me" and env["BYBIT_API_KEY"] == "bk-123456"
    for secret in (new, SMOKE, "bk-123456", "bs-abcdef", "tg-999"):
        assert secret not in out
    assert preflight.fingerprint(new) in out
    assert stat.S_IMODE(prof.stat().st_mode) == 0o600
    assert list(prof.parent.glob(prof.name + ".bak.*")), "backup before changing"


def test_keys_is_idempotent(isolated_paths, capsys):
    prof = Path(isolated_paths["YIELD_ENV_FILE"])
    _write_env(prof, HERMES_RISK_HMAC_KEY="a" * 64, BYBIT_API_KEY="k", BYBIT_API_SECRET="s",
               TELEGRAM_BOT_TOKEN="t")
    before = prof.read_text()
    assert preflight.main(["keys"]) == 0
    assert prof.read_text() == before
    assert not list(prof.parent.glob(prof.name + ".bak.*")), "no change -> no backup"


def test_keys_copies_bybit_keys_from_shared_env(isolated_paths, capsys):
    prof = Path(isolated_paths["YIELD_ENV_FILE"])
    _write_env(prof, HERMES_RISK_HMAC_KEY="a" * 64)
    _write_env(Path(isolated_paths["YIELD_SHARED_ENV_FILE"]), BYBIT_API_KEY="k-shared",
               BYBIT_API_SECRET="s-shared", TELEGRAM_BOT_TOKEN="t")
    assert preflight.main(["keys"]) == 0
    env = settings.load_env_file(prof)
    assert env["BYBIT_API_KEY"] == "k-shared" and env["BYBIT_API_SECRET"] == "s-shared"
    assert "k-shared" not in capsys.readouterr().out


@pytest.mark.parametrize("profile,shared,why", [
    ({"HERMES_RISK_HMAC_KEY": "a" * 64}, {"TELEGRAM_BOT_TOKEN": "t"}, "BYBIT_API_KEY"),
    ({"HERMES_RISK_HMAC_KEY": "a" * 64, "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}, {},
     "TELEGRAM_BOT_TOKEN"),
    ({"HERMES_RISK_HMAC_KEY": "a" * 64, "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
      "BYBIT_TESTNET": "1"}, {"TELEGRAM_BOT_TOKEN": "t"}, "BYBIT_TESTNET"),
])
def test_keys_fails(isolated_paths, capsys, profile, shared, why):
    _write_env(Path(isolated_paths["YIELD_ENV_FILE"]), **profile)
    _write_env(Path(isolated_paths["YIELD_SHARED_ENV_FILE"]), **shared)
    assert preflight.main(["keys"]) == 1
    assert why in capsys.readouterr().out


def test_keys_refuses_testnet_from_process_env(isolated_paths, monkeypatch, capsys):
    _write_env(Path(isolated_paths["YIELD_ENV_FILE"]), HERMES_RISK_HMAC_KEY="a" * 64,
               BYBIT_API_KEY="k", BYBIT_API_SECRET="s", TELEGRAM_BOT_TOKEN="t")
    monkeypatch.setenv("BYBIT_TESTNET", "true")
    assert preflight.main(["keys"]) == 1


# --- risk state ------------------------------------------------------------ #

def _key_in_profile(isolated_paths, key=KEY):
    _write_env(Path(isolated_paths["YIELD_ENV_FILE"]), HERMES_RISK_HMAC_KEY=key)


def test_reset_state_moves_legacy_file_aside(isolated_paths):
    _key_in_profile(isolated_paths)
    state = Path(isolated_paths["YIELD_STATE_FILE"])
    state.write_text(json.dumps({"profile": "hermes-yield-rotation", "state": "NORMAL",
                                 "ts": 1, "reason": "old", "sig": "x"}))
    assert preflight.main(["reset-state-if-invalid"]) == 0
    assert not state.exists()
    assert len(list(state.parent.glob(state.name + ".pre-v6.*"))) == 1


def test_reset_state_keeps_a_valid_record(isolated_paths):
    _key_in_profile(isolated_paths)
    state = Path(isolated_paths["YIELD_STATE_FILE"])
    write_state(state, "NORMAL", source=risk_state.SOURCE_RENEW, age_s=3 * 3600)
    before = state.read_bytes()
    assert preflight.main(["reset-state-if-invalid"]) == 0
    assert state.read_bytes() == before


def test_expect_normal(isolated_paths, capsys):
    _key_in_profile(isolated_paths)
    state = Path(isolated_paths["YIELD_STATE_FILE"])
    write_state(state, "NO_NEW_POSITIONS", source=risk_state.SOURCE_OPERATOR)
    assert preflight.main(["expect-normal"]) == 1
    assert "operator" in capsys.readouterr().out
    write_state(state, "NORMAL", source=risk_state.SOURCE_RENEW)
    assert preflight.main(["expect-normal"]) == 0


# --- the manual cycle ------------------------------------------------------ #

def _cycle(isolated_paths, monkeypatch, agent_reply=None, **cfg):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    import run_yield_cycle as ryc
    hold = {"action": "HOLD", "coin": "USDT", "product_id": "1", "reason": "x"}
    ryc.run_cycle(load_cfg(isolated_paths, **cfg), tool=cfg.pop("_tool", FakeBybit()),
                  agent=FakeAgent(agent_reply or reply_with(hold)))


def test_check_cycle_passes_on_clean_record(isolated_paths, monkeypatch):
    write_state(isolated_paths["YIELD_STATE_FILE"], "NORMAL")
    _cycle(isolated_paths, monkeypatch)
    assert preflight.main(["check-cycle"]) == 0


@pytest.mark.parametrize("case", ["blocking", "data", "no_eligible"])
def test_check_cycle_fails(isolated_paths, monkeypatch, capsys, case):
    write_state(isolated_paths["YIELD_STATE_FILE"], "NORMAL")
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    import run_yield_cycle as ryc
    from helpers import apr_history
    tool, agent = FakeBybit(), FakeAgent(lambda cid: "garbage")
    if case == "data":
        tool = FakeBybit(fail={"get_wallet_balance"})
        agent = FakeAgent(reply_with({"action": "HOLD", "coin": "USDT", "product_id": "1",
                                      "reason": "x"}))
    if case == "no_eligible":
        tool = FakeBybit(history=apr_history(newest_age_s=5 * 3600))
    ryc.run_cycle(load_cfg(isolated_paths, SIMULATED_IDLE_BALANCE=None), tool=tool, agent=agent)
    assert preflight.main(["check-cycle"]) == 1
    out = capsys.readouterr().out
    assert {"blocking": "AGENT_PARSE_ERROR", "data": "balance",
            "no_eligible": "STALE_HISTORY"}[case] in out


# --- regression report ----------------------------------------------------- #

def test_regression_report_path_depends_on_prompt_and_model(isolated_paths, capsys):
    preflight.main(["regression-report"])
    path = capsys.readouterr().out.strip()
    assert "regression_v6_" in path and path.endswith(".json")
    body = (REPO / "prompt_v6.md").read_bytes()
    import hashlib
    assert hashlib.sha256(body).hexdigest()[:12] in path


def test_regression_ok(isolated_paths, tmp_path):
    report = tmp_path / "r.json"
    assert preflight.main(["regression-ok", str(report)]) == 1  # missing
    report.write_text(json.dumps({"model": "google/gemini-2.5-flash", "prompt_version": "v6",
                                  "total": 25, "passed": 24}))
    assert preflight.main(["regression-ok", str(report)]) == 1
    report.write_text(json.dumps({"model": "google/gemini-2.5-flash", "prompt_version": "v6",
                                  "total": 25, "passed": 25}))
    assert preflight.main(["regression-ok", str(report)]) == 0
    report.write_text(json.dumps({"model": "other/model", "prompt_version": "v6",
                                  "total": 25, "passed": 25}))
    assert preflight.main(["regression-ok", str(report)]) == 1


# --- telegram -------------------------------------------------------------- #

def test_telegram_test(isolated_paths, monkeypatch):
    sent = []
    monkeypatch.setattr(preflight, "send_telegram", lambda t, c, x: sent.append(x) or True)
    assert preflight.main(["telegram-test"]) == 1  # no token
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert preflight.main(["telegram-test"]) == 0 and len(sent) == 1


# =========================================================================== #
# deploy.sh with stubbed system commands                                       #
# =========================================================================== #

STUB = r'''#!/usr/bin/env bash
# Logs "<name> <args>" and replies from $STUB_DIR/<name>.<first-arg>.{out,rc}
name=$(basename "$0")
printf '%s' "$0" >> "$STUB_DIR/paths.log"; printf ' %q' "$@" >> "$STUB_DIR/paths.log"; printf '\n' >> "$STUB_DIR/paths.log"
printf '%s' "$name" >> "$STUB_DIR/calls.log"
printf ' %q' "$@" >> "$STUB_DIR/calls.log"
printf '\n' >> "$STUB_DIR/calls.log"
key="$name"
case "$name" in
  python)
    if [[ ${1:-} == -m ]]; then key="python.$2"
    elif [[ ${1:-} == */preflight.py ]]; then key="python.preflight.$2"
    else key="python.$(basename "${1:-x}" .py)"; fi ;;
  systemctl) key="systemctl.$1" ;;
  *) key="$name.${1:-}" ;;
esac
# Keys listed in $STUB_PASSTHROUGH run for real (e.g. a real preflight check).
if [[ " ${STUB_PASSTHROUGH:-} " == *" $key "* ]]; then exec "$REAL_PYTHON" "$@"; fi
[[ -f "$STUB_DIR/$key.out" ]] && cat "$STUB_DIR/$key.out"
rcfile="$STUB_DIR/$key.rc"
rc=0
if [[ -f $rcfile ]]; then
  rc=$(head -n 1 "$rcfile")
  # A multi-line .rc file is a queue: one exit code per call, the last one sticks.
  if (( $(wc -l < "$rcfile") > 1 )); then tail -n +2 "$rcfile" > "$rcfile.tmp" && mv "$rcfile.tmp" "$rcfile"; fi
fi
exit "$rc"
'''


@pytest.fixture
def sandbox(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("systemctl", "crontab", "git", "systemd-analyze", "python", "hermes"):
        p = stubs / name
        p.write_text(STUB)
        p.chmod(0o755)
    home = tmp_path / "hermes"
    # The Hermes CLI venv: only the `hermes` binary. No python here, so any
    # use of it by deploy.sh would fail loudly.
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / ".venv" / "bin" / "hermes").symlink_to(stubs / "hermes")
    # The project's own venv.
    venv = home / "venvs" / "yield_rotation"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(stubs / "python")
    install = tmp_path / "install.sh"
    install.write_text(f'#!/usr/bin/env bash\necho "install.sh $*" >> "{stubs}/calls.log"\n')
    install.chmod(0o755)
    (stubs / "hermes.chat.out").write_text("  --query-file FILE\n  --toolsets LIST\n  -Q\n"
                                           "  -m MODEL\n  --reasoning LEVEL\n")
    (stubs / "systemctl.is-active.out").write_text("active\n")
    (stubs / "systemctl.is-enabled.out").write_text("enabled\n")
    # regression report: absent before the run, passing after it
    (stubs / "python.preflight.regression-ok.rc").write_text("1\n0\n")
    (stubs / "python.preflight.regression-report.out").write_text(str(tmp_path / "reg.json") + "\n")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("YIELD_", "BYBIT"))},
           "PATH": f"{stubs}:{os.environ['PATH']}", "STUB_DIR": str(stubs),
           "YIELD_REPO": str(REPO), "YIELD_HERMES_HOME": str(home), "YIELD_VENV": str(venv),
           "REAL_PYTHON": sys.executable,
           "YIELD_RUN_AS": subprocess.run(["id", "-un"], capture_output=True,
                                          text=True).stdout.strip(),
           "YIELD_INSTALL": str(install), "YIELD_DEPLOY_LOG_DIR": str(tmp_path / "logs"),
           "YIELD_DEPLOY_ALLOW_NONROOT": "1"}

    class Box:
        dir = stubs
        logs = tmp_path / "logs"
        hermes_home = home
        project_venv = venv
        root = tmp_path

        def paths(self):
            f = stubs / "paths.log"
            return f.read_text().splitlines() if f.exists() else []

        def reply(self, key, out="", rc=0):
            (stubs / f"{key}.out").write_text(out)
            (stubs / f"{key}.rc").write_text(str(rc))

        env_extra: dict = {}

        def run(self):
            return subprocess.run(["bash", str(REPO / "deploy" / "deploy.sh")],
                                  env={**env, **self.env_extra},
                                  capture_output=True, text=True, timeout=60)

        def calls(self):
            f = stubs / "calls.log"
            return f.read_text().splitlines() if f.exists() else []

        def systemctl_mutations(self):
            verbs = ("enable", "disable", "start", "stop", "restart", "mask", "unmask",
                     "reload", "kill", "reset-failed", "daemon-reload")
            out = []
            for c in self.calls():
                parts = c.split()
                if parts[0] == "systemctl" and len(parts) > 1 and parts[1] in verbs:
                    out.append(parts[1:])
            return out

    return Box()


def _assert_only_yield_units(box):
    for verb, *args in box.systemctl_mutations():
        units = [a for a in args if not a.startswith("-")]
        assert units and all(u.startswith("yield-") for u in units), (verb, args)


def test_all_steps_pass(sandbox):
    r = sandbox.run()
    assert r.returncode == 0, r.stdout + r.stderr
    for n in range(1, 8):
        assert f"PASS step {n}/7" in r.stdout
    assert "FAIL" not in r.stdout
    assert "ALL 7 STEPS PASSED" in r.stdout
    [log] = list(sandbox.logs.glob("deploy_*.log"))
    assert log.read_text() == r.stdout
    calls = sandbox.calls()
    assert any(c.startswith("python.") or "run_regression.py" in c for c in calls)
    assert any("install.sh" in c for c in calls)
    _assert_only_yield_units(sandbox)


def test_stops_at_first_fail_with_raw_output(sandbox):
    sandbox.reply("python.pytest", out="E   assert 1 == 2\n1 failed, 3 passed", rc=1)
    r = sandbox.run()
    assert r.returncode != 0
    assert "PASS step 1/7" in r.stdout and "FAIL step 2/7" in r.stdout
    assert "E   assert 1 == 2" in r.stdout
    assert "step 3/7" not in r.stdout
    assert not any("install.sh" in c for c in sandbox.calls())
    assert "DEPLOY STOPPED at step 2/7" in r.stdout
    [log] = list(sandbox.logs.glob("deploy_*.log"))
    assert "FAIL step 2/7" in log.read_text()


def test_regression_runs_before_install_and_is_skipped_when_already_passed(sandbox):
    r = sandbox.run()
    calls = sandbox.calls()
    reg = next(i for i, c in enumerate(calls) if "run_regression" in c)
    inst = next(i for i, c in enumerate(calls) if "install.sh" in c)
    assert reg < inst
    (sandbox.dir / "python.preflight.regression-ok.rc").write_text("0\n")
    (sandbox.dir / "calls.log").unlink()
    r = sandbox.run()
    assert r.returncode == 0
    assert not any("run_regression" in c for c in sandbox.calls())
    assert "already passed" in r.stdout


def test_failed_regression_never_installs_timers(sandbox):
    sandbox.reply("python.run_regression", out="FAIL 3/5  02_hold_when_rate_below_entry", rc=1)
    r = sandbox.run()
    assert "FAIL step 6/7" in r.stdout
    assert not any("install.sh" in c for c in sandbox.calls())
    assert not any(m[0] in ("enable", "start") for m in sandbox.systemctl_mutations())


def test_foreign_unit_matching_pattern_fails_step1_and_is_not_touched(sandbox):
    listing = ("hermes-gateway.service enabled\nhermes-litellm.service enabled\n"
               "hermes-george.service enabled\nhermes-seo_agent.service enabled\n"
               "old-heartbeat.timer enabled\n")
    sandbox.reply("systemctl.list-unit-files", out=listing)
    r = sandbox.run()
    assert "FAIL step 1/7" in r.stdout and "old-heartbeat.timer" in r.stdout
    assert sandbox.systemctl_mutations() == []


def test_legacy_yield_unit_is_disabled_and_ours_are_kept(sandbox):
    listing = "yield-rotation.timer enabled\nyield-cycle.timer enabled\nhermes-gateway.service enabled\n"
    sandbox.reply("systemctl.list-unit-files", out=listing)
    r = sandbox.run()
    assert r.returncode == 0, r.stdout
    muts = sandbox.systemctl_mutations()
    assert ["disable", "--now", "yield-rotation.timer"] in muts
    assert not any("yield-cycle.timer" in m for m in muts)
    assert not any("hermes-gateway.service" in " ".join(m) for m in muts)
    _assert_only_yield_units(sandbox)


def test_cron_entry_fails_step1(sandbox):
    sandbox.reply("crontab.-l", out="*/10 * * * * /opt/hermes/.venv/bin/python run_yield_cycle.py\n")
    r = sandbox.run()
    assert "FAIL step 1/7" in r.stdout and "run_yield_cycle.py" in r.stdout


def test_missing_agent_flag_fails_step2(sandbox):
    (sandbox.dir / "hermes.chat.out").write_text("  --query-file FILE\n  -m MODEL\n")
    r = sandbox.run()
    assert "FAIL step 2/7" in r.stdout and "--toolsets" in r.stdout


def test_dirty_checkout_fails_step2(sandbox):
    sandbox.reply("git.-C", out=" M run_yield_cycle.py\n")
    r = sandbox.run()
    assert "FAIL step 2/7" in r.stdout


def test_idempotent(sandbox):
    assert sandbox.run().returncode == 0
    assert sandbox.run().returncode == 0
    _assert_only_yield_units(sandbox)


def test_deploy_script_never_runs_testnet():
    text = (REPO / "deploy" / "deploy.sh").read_text()
    assert "testnet.py" not in text and "BYBIT_TESTNET=1" not in text


def test_requires_root_without_escape_hatch(sandbox, tmp_path):
    """Must never run a real deploy: sandbox paths, and skipped under root
    (where the refusal cannot be observed anyway)."""
    if os.geteuid() == 0:
        pytest.skip("running as root: the non-root refusal cannot be observed")
    env = {"PATH": f"{sandbox.dir}:{os.environ['PATH']}", "STUB_DIR": str(sandbox.dir),
           "YIELD_HERMES_HOME": str(tmp_path / "nothing"),
           "YIELD_DEPLOY_LOG_DIR": str(tmp_path / "nolog")}
    r = subprocess.run(["bash", str(REPO / "deploy" / "deploy.sh")], env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "root" in (r.stdout + r.stderr)
    assert sandbox.calls() == [] and not (tmp_path / "nolog").exists()


@pytest.mark.parametrize("cmd", ["list-unit-files", "list-units"])
def test_unreadable_systemd_fails_step1(sandbox, cmd):
    sandbox.reply(f"systemctl.{cmd}", out="Failed to connect to bus: Host is down", rc=1)
    r = sandbox.run()
    assert "FAIL step 1/7" in r.stdout and "Host is down" in r.stdout


def test_no_crontab_is_fine_but_crontab_error_fails(sandbox):
    sandbox.reply("crontab.-l", out="no crontab for hermes", rc=1)
    assert "PASS step 1/7" in sandbox.run().stdout
    sandbox.reply("crontab.-l", out="crontab: cannot open /var/spool: Permission denied", rc=1)
    r = sandbox.run()
    assert "FAIL step 1/7" in r.stdout and "Permission denied" in r.stdout


def test_git_runs_as_the_repo_owner():
    """As root, git refuses a hermes-owned repo ("dubious ownership")."""
    for line in (REPO / "deploy" / "deploy.sh").read_text().splitlines():
        code = line.split("#", 1)[0]
        if re.search(r"\bgit -C\b", code):
            assert "as_hermes git" in code, line


def test_shell_syntax():
    subprocess.run(["bash", "-n", str(REPO / "deploy" / "deploy.sh")], check=True)


def test_cron_not_installed_means_no_cron_jobs(sandbox, monkeypatch):
    sandbox.env_extra = {"YIELD_CRONTAB": "/nonexistent/crontab"}
    r = sandbox.run()
    assert "PASS step 1/7" in r.stdout and "cron is not installed" in r.stdout


def test_keys_writes_nothing_when_another_key_is_missing(isolated_paths, capsys):
    prof = Path(isolated_paths["YIELD_ENV_FILE"])
    _write_env(prof, HERMES_RISK_HMAC_KEY=SMOKE)
    assert preflight.main(["keys"]) == 1
    out = capsys.readouterr().out
    assert settings.load_env_file(prof)["HERMES_RISK_HMAC_KEY"] == SMOKE
    assert "NOT WRITTEN" in out and "GENERATED" not in out



# =========================================================================== #
# DRY_RUN is enforced, not just checked                                        #
# =========================================================================== #

@pytest.mark.parametrize("value,rc", [(True, 0), (False, 1), ("true", 1), (None, 1)])
def test_preflight_dry_run_on(isolated_paths, value, rc, capsys):
    import yaml
    cfg_file = Path(isolated_paths["YIELD_CONFIG_FILE"])
    cfg = yaml.safe_load(cfg_file.read_text())
    if value is None:
        cfg.pop("DRY_RUN")
    else:
        cfg["DRY_RUN"] = value
    cfg_file.write_text(yaml.safe_dump(cfg))
    assert preflight.main(["dry-run-on"]) == rc
    assert str(cfg_file) in capsys.readouterr().out


def _config_with(tmp_path, **over):
    import yaml
    cfg = yaml.safe_load((REPO / "config" / "yield_rotation.yaml").read_text())
    cfg.update(over)
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_live_config_fails_step5_before_anything_runs(sandbox):
    """Config says DRY_RUN: false -> FAIL at step 5 with zero executions:
    no risk_state change, no cycle, no heartbeat, no regression, no install."""
    cfg = _config_with(sandbox.root, DRY_RUN=False, SIMULATED_IDLE_BALANCE=None)
    sandbox.env_extra = {"YIELD_CONFIG_FILE": str(cfg),
                         "STUB_PASSTHROUGH": "python.preflight.dry-run-on"}
    r = sandbox.run()
    assert "PASS step 4/7" in r.stdout and "FAIL step 5/7" in r.stdout, r.stdout
    assert "DRY_RUN" in r.stdout
    calls = sandbox.calls()
    step5_on = [c for c in calls if c.startswith("python") and any(
        x in c for x in ("heartbeat", "run_yield_cycle", "reset-state", "check-cycle",
                         "run_regression"))]
    assert step5_on == []
    assert not any("install.sh" in c for c in calls)
    assert sandbox.systemctl_mutations() == []


def test_dry_run_config_passes_the_real_check(sandbox):
    cfg = _config_with(sandbox.root, DRY_RUN=True)
    sandbox.env_extra = {"YIELD_CONFIG_FILE": str(cfg),
                         "STUB_PASSTHROUGH": "python.preflight.dry-run-on"}
    r = sandbox.run()
    assert r.returncode == 0, r.stdout
    assert "DRY_RUN: true (verified" in r.stdout


def test_manual_cycle_runs_with_dry_run_flag(sandbox):
    sandbox.run()
    cycles = [c for c in sandbox.calls() if "run_yield_cycle" in c]
    assert cycles and all("--dry-run" in c for c in cycles)


def test_dry_run_checked_before_step7_install(sandbox):
    # step 5 check passes, step 7 check fails -> nothing installed
    (sandbox.dir / "python.preflight.dry-run-on.rc").write_text("0\n1\n")
    r = sandbox.run()
    assert "FAIL step 7/7" in r.stdout
    assert not any("install.sh" in c for c in sandbox.calls())


def test_final_message_only_after_the_final_check(sandbox):
    # steps 5 and 7 pass, the final re-check fails
    (sandbox.dir / "python.preflight.dry-run-on.rc").write_text("0\n0\n1\n")
    r = sandbox.run()
    assert r.returncode != 0
    assert "ALL 7 STEPS PASSED" not in r.stdout
    assert "DRY_RUN: true (verified" not in r.stdout


# =========================================================================== #
# The project has its own venv; the Hermes CLI venv is never modified         #
# =========================================================================== #

def test_project_uses_its_own_venv(sandbox):
    assert sandbox.run().returncode == 0
    hermes_venv = str(sandbox.hermes_home / ".venv")
    for line in sandbox.paths():
        exe = line.split()[0]
        if exe.startswith(hermes_venv):
            assert exe.endswith("/bin/hermes"), f"hermes venv used for: {line}"
    pips = [l for l in sandbox.paths() if " -m pip" in l.replace("\\ ", " ")]
    assert pips and all(l.startswith(str(sandbox.project_venv)) for l in pips)
    assert any(l.startswith(hermes_venv + "/bin/hermes") for l in sandbox.paths())


def test_missing_project_venv_is_created(sandbox):
    (sandbox.project_venv / "bin" / "python").unlink()
    base = sandbox.dir / "python3"
    base.write_text(STUB)
    base.chmod(0o755)
    sandbox.env_extra = {"YIELD_BASE_PYTHON": str(base)}
    r = sandbox.run()
    assert any(l.startswith(str(base)) and "-m venv" in l and str(sandbox.project_venv) in l
               for l in sandbox.paths())
    assert "FAIL step 2/7" in r.stdout  # the stub created nothing


def test_nothing_installs_into_the_hermes_cli_venv():
    for f in ["deploy/deploy.sh", "deploy/install.sh", ".github/workflows/tests.yml",
              "DEPLOY.md", "HANDOFF.md", "README.md", *map(str, (REPO / "deploy").glob("*.service"))]:
        text = (REPO / f).read_text()
        assert "/opt/hermes/.venv/bin/pip" not in text, f
        assert "/opt/hermes/.venv/bin/python" not in text, f
        assert "HERMES_HOME/.venv/bin/python" not in text, f
