"""T0.1 / T0.2 — the repo runs on a clean machine; env loading is unified.

Importing any module must not touch /opt, must not mutate sys.path with
absolute VPS paths, and must not write to os.environ.  Env precedence is:
process env -> profile .env (/opt/hermes/.env) -> from the shared
/opt/data/.env ONLY TELEGRAM_BOT_TOKEN.
"""

import json
import shutil
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_IMPORT_PROBE = r"""
import importlib, json, os, site, sys
module, forbidden = sys.argv[1], os.path.realpath(sys.argv[2])
# The interpreter's own files (stdlib, site-packages) may legitimately live
# under /opt — e.g. /opt/hostedtoolcache on GitHub runners.
own = tuple(os.path.realpath(p) for p in
            {sys.prefix, sys.base_prefix, sys.exec_prefix, *site.getsitepackages()})
own += (OWN_REPO,)
touched = []
def hook(event, args):
    if event in ("open", "os.mkdir", "os.listdir", "os.scandir", "os.remove",
                 "os.rename", "os.chmod") and args:
        p = args[0]
        if isinstance(p, bytes):
            p = p.decode(errors="replace")
        if not isinstance(p, (str, os.PathLike)):
            return
        real = os.path.realpath(str(p))
        if (real == forbidden or real.startswith(forbidden + os.sep)) and not any(
                real == o or real.startswith(o + os.sep) for o in own):
            touched.append([event, str(p)])
sys.addaudithook(hook)
env_before = dict(os.environ)
path_before = list(sys.path)
importlib.import_module(module)
print(json.dumps({
    "touched": touched,
    "opt_on_sys_path": [p for p in sys.path if p not in path_before
                        and os.path.realpath(p).startswith(forbidden)],
    "env_added": sorted(set(os.environ) - set(env_before)),
}))
"""

MODULES = ["run_yield_cycle", "heartbeat", "bybit_earn_tool", "executor", "signing",
           "risk_state", "settings", "notify", "summary", "telegram_bot",
           "carry.decide", "carry.backtest", "carry.client"]


def _clean_env(repo=REPO_ROOT, **extra):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": str(repo), "LANG": "C.UTF-8"}
    env.update(extra)
    return env


def _probe(module, cwd, repo=REPO_ROOT, forbidden="/opt"):
    """Import `module` from `repo` in a clean interpreter and report every
    access under `forbidden` — except the interpreter's own files and the
    repo's own files (the repo itself lives under /opt on the VPS)."""
    code = _IMPORT_PROBE.replace("OWN_REPO", repr(os.path.realpath(repo)))
    proc = subprocess.run(
        [sys.executable, "-c", code, module, str(forbidden)],
        cwd=cwd, env=_clean_env(repo), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"import {module} failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", MODULES)
def test_import_has_no_side_effects(module, tmp_path):
    result = _probe(module, tmp_path)
    assert result["touched"] == [], f"import {module} touched /opt: {result['touched']}"
    assert result["opt_on_sys_path"] == [], f"import {module} put /opt on sys.path"
    assert result["env_added"] == [], f"import {module} wrote os.environ: {result['env_added']}"


def _repo_copy_under_opt(tmp_path):
    """The VPS layout: the repo at <root>/opt/hermes/yield_rotation."""
    fake_opt = tmp_path / "opt"
    repo = fake_opt / "hermes" / "yield_rotation"
    shutil.copytree(REPO_ROOT, repo, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", "*.pyc"))
    return fake_opt, repo


@pytest.mark.parametrize("module", MODULES)
def test_probe_allows_repo_own_files_when_repo_lives_under_opt(module, tmp_path):
    fake_opt, repo = _repo_copy_under_opt(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    result = _probe(module, cwd, repo=repo, forbidden=fake_opt)
    assert result["touched"] == [], result["touched"]
    assert result["opt_on_sys_path"] == []


def test_probe_still_catches_access_next_to_the_repo(tmp_path):
    """Excluding the repo must not exclude its neighbours (e.g. /opt/hermes/.env)."""
    fake_opt, repo = _repo_copy_under_opt(tmp_path)
    secret = fake_opt / "hermes" / ".env"
    secret.write_text("HERMES_RISK_HMAC_KEY=x\n")
    (repo / "reads_env_on_import.py").write_text(f"open({str(secret)!r}).read()\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    result = _probe("reads_env_on_import", cwd, repo=repo, forbidden=fake_opt)
    assert [e[1] for e in result["touched"]] == [str(secret)]


def test_import_does_not_load_env_from_cwd(tmp_path):
    (tmp_path / ".env").write_text("BYBIT_API_KEY=from-cwd\nBYBIT_API_SECRET=from-cwd\n")
    result = _probe("bybit_earn_tool", tmp_path)
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
