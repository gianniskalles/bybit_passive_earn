"""Shared pytest fixtures.

Every test runs hermetically: all paths point into tmp_path, the real
/opt/hermes/.env and /opt/data/.env are never read, credentials from the
developer's shell are stripped, and outbound network is refused.
"""

import socket
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Env vars that must never leak from the developer's shell into a test.
_SCRUBBED_ENV = (
    "HERMES_RISK_HMAC_KEY",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "BYBIT_TESTNET",
    "TELEGRAM_BOT_TOKEN",
    "ALERT_TELEGRAM_CHAT_ID",
    "YIELD_LOG_DIR",
    "YIELD_SKIP_API_CHECK",
    "YIELD_SKIP_CONFIG_DCHECK",
    "YIELD_ROTATION_PROMPT",
)


class _NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Rule 3: no network calls in tests. Any connect attempt fails loudly."""

    def _refuse(*args, **kwargs):
        raise _NetworkBlocked(f"network access attempted in test: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Point every configurable path into tmp_path and scrub credentials."""
    for key in _SCRUBBED_ENV:
        monkeypatch.delenv(key, raising=False)

    # Kept under a subdirectory so tests can freely create their own files
    # directly in tmp_path.
    base = tmp_path / "_isolated"
    hermes_home = base / "hermes"
    hermes_home.mkdir(parents=True)
    log_dir = base / "logs"
    log_dir.mkdir()

    cfg = yaml.safe_load((REPO_ROOT / "config" / "yield_rotation.yaml").read_text())
    cfg["LOG_DIR"] = str(log_dir)
    config_file = base / "yield_rotation.yaml"
    config_file.write_text(yaml.safe_dump(cfg))

    paths = {
        "YIELD_HERMES_HOME": hermes_home,
        "YIELD_CONFIG_FILE": config_file,
        "YIELD_STATE_FILE": base / "risk_state.json",
        "YIELD_ENV_FILE": base / "profile.env",
        "YIELD_SHARED_ENV_FILE": base / "shared.env",
        "YIELD_SESSION_DIR": base / "sessions",
        "YIELD_NOTIFY_STATE": base / "notify_state.json",
    }
    for key, value in paths.items():
        monkeypatch.setenv(key, str(value))

    paths["LOG_DIR"] = log_dir
    paths["ROOT"] = base
    return paths
