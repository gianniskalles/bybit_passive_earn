"""T0.1 / T0.2 — the repo runs on a clean machine; env loading is unified.

Importing any module must not touch /opt, must not mutate sys.path with
absolute VPS paths, and must not write to os.environ.  Env precedence is:
process env -> profile .env (/opt/hermes/.env) -> from the shared
/opt/data/.env ONLY TELEGRAM_BOT_TOKEN.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_IMPORT_PROBE = r"""
import importlib, json, os, site, sys
# The interpreter's own files (stdlib, site-packages) may legitimately live
# under /opt — e.g. /opt/hostedtoolcache on GitHub runners. Only the
# project's own behaviour is under test.
own = tuple(os.path.realpath(p) for p in
            {sys.prefix, sys.base_prefix, sys.exec_prefix, *site.getsitepackages()})
touched = []
def hook(event, args):
    if event in ("open", "os.mkdir", "os.listdir", "os.scandir", "os.remove",
                 "os.rename", "os.chmod") and args:
        p = args[0]
        if isinstance(p, bytes):
            p = p.decode(errors="replace")
        if not isinstance(p, (str, os.PathLike)):
            return
        p = str(p)
        if p.startswith("/opt") and not os.path.realpath(p).startswith(own):
            touched.append([event, p])
sys.addaudithook(hook)
env_before = dict(os.environ)
path_before = list(sys.path)
importlib.import_module(sys.argv[1])
print(json.dumps({
    "touched": touched,
    "opt_on_sys_path": [p for p in sys.path if p not in path_before and str(p).startswith("/opt")],
    "env_added": sorted(set(os.environ) - set(env_before)),
}))
"""

MODULES = ["run_yield_cycle", "heartbeat", "bybit_earn_tool", "executor", "signing",
           "risk_state", "settings", "notify", "summary", "telegram_bot"]


def _clean_env(**extra):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": str(REPO_ROOT), "LANG": "C.UTF-8"}
    env.update(extra)
    return env


@pytest.mark.parametrize("module", MODULES)
def test_import_has_no_side_effects(module, tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, module],
        cwd=tmp_path, env=_clean_env(), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"import {module} failed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["touched"] == [], f"import {module} touched /opt: {result['touched']}"
    assert result["opt_on_sys_path"] == [], f"import {module} put /opt on sys.path"
    assert result["env_added"] == [], f"import {module} wrote os.environ: {result['env_added']}"


def test_import_does_not_load_env_from_cwd(tmp_path):
    (tmp_path / ".env").write_text("BYBIT_API_KEY=from-cwd\nBYBIT_API_SECRET=from-cwd\n")
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, "bybit_earn_tool"],
        cwd=tmp_path, env=_clean_env(), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "BYBIT_API_KEY" not in result["env_added"]


# --------------------------------------------------------------------------- #
# T0.2 — unified load_env                                                     #
# --------------------------------------------------------------------------- #

def _write(path: Path, **kv):
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))


def test_env_precedence_process_over_profile_over_shared(isolated_paths, monkeypatch):
    import settings
    _write(isolated_paths["YIELD_ENV_FILE"], BYBIT_API_KEY="profile", TELEGRAM_BOT_TOKEN="profile-tok")
    _write(isolated_paths["YIELD_SHARED_ENV_FILE"], TELEGRAM_BOT_TOKEN="shared-tok")
    env = settings.load_env()
    assert env["BYBIT_API_KEY"] == "profile"
    assert env["TELEGRAM_BOT_TOKEN"] == "profile-tok"

    monkeypatch.setenv("BYBIT_API_KEY", "process")
    assert settings.load_env()["BYBIT_API_KEY"] == "process"


def test_shared_env_supplies_only_telegram_token(isolated_paths):
    import settings
    _write(isolated_paths["YIELD_SHARED_ENV_FILE"], TELEGRAM_BOT_TOKEN="shared-tok", OTHER="x")
    env = settings.load_env()
    assert env["TELEGRAM_BOT_TOKEN"] == "shared-tok"
    assert "OTHER" not in env


def test_shared_env_cannot_override_keys(isolated_paths):
    import settings
    _write(isolated_paths["YIELD_ENV_FILE"], BYBIT_API_KEY="profile-key", BYBIT_API_SECRET="profile-secret")
    _write(isolated_paths["YIELD_SHARED_ENV_FILE"],
           BYBIT_API_KEY="", BYBIT_API_SECRET="shared-secret", HERMES_RISK_HMAC_KEY="shared-hmac")
    env = settings.load_env()
    assert env["BYBIT_API_KEY"] == "profile-key"
    assert env["BYBIT_API_SECRET"] == "profile-secret"
    assert "HERMES_RISK_HMAC_KEY" not in env

    import bybit_earn_tool
    tool = bybit_earn_tool.BybitEarnTool()
    assert tool.api_key == "profile-key"
    assert tool.api_secret == "profile-secret"


def test_load_env_tolerates_missing_files_and_does_not_mutate_environ(isolated_paths):
    import settings
    before = dict(os.environ)
    env = settings.load_env()
    assert dict(os.environ) == before
    assert "BYBIT_API_KEY" not in env


def test_load_env_file_parses_quotes_and_comments(tmp_path):
    import settings
    p = tmp_path / "x.env"
    p.write_text('# comment\n\nA="quoted"\nB=\'single\'\nC = spaced \nnot a pair\n')
    assert settings.load_env_file(p) == {"A": "quoted", "B": "single", "C": "spaced"}


def test_heartbeat_signs_with_profile_key_not_shared(isolated_paths, monkeypatch):
    """The shared /opt/data/.env must not be able to substitute the HMAC key."""
    from signing import verify
    _write(isolated_paths["YIELD_ENV_FILE"], HERMES_RISK_HMAC_KEY="profile-hmac")
    _write(isolated_paths["YIELD_SHARED_ENV_FILE"], HERMES_RISK_HMAC_KEY="shared-hmac")
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")

    import importlib
    import heartbeat
    importlib.reload(heartbeat)
    assert heartbeat.main() == 0

    rs = json.loads(isolated_paths["YIELD_STATE_FILE"].read_text())
    payload = {k: v for k, v in rs.items() if k != "sig"}
    assert verify("profile-hmac", payload, rs["sig"])


def test_wrapper_reads_hmac_key_from_profile_env(isolated_paths):
    """run_yield_cycle used to rely on bybit_earn_tool having copied
    /opt/hermes/.env into os.environ at import time.  It must read the key
    through the unified loader instead."""
    import risk_state
    import run_yield_cycle
    _write(isolated_paths["YIELD_ENV_FILE"], HERMES_RISK_HMAC_KEY="profile-hmac")
    risk_state.write(isolated_paths["YIELD_STATE_FILE"], "profile-hmac", "NORMAL", "t",
                     risk_state.SOURCE_OPERATOR)
    state, meta, alerts = run_yield_cycle.resolve_risk_state()
    assert (state, meta["code"], alerts) == ("NORMAL", "OK", [])


def test_risk_state_comes_from_the_repo_only():
    """No fallback to /opt/hermes/tools: the repo's risk_state.py is the one."""
    import risk_state
    import run_yield_cycle
    import settings
    assert Path(risk_state.__file__).resolve().parent == REPO_ROOT
    assert not hasattr(run_yield_cycle, "_risk_state_module")
    assert not hasattr(settings, "risk_state_dir")
