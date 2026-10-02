"""Testnet state is separate from mainnet state (decision of 2/10, before
the testnet). With BYBIT_TESTNET set — the same switch as the Bybit
client — every carry state file lives in <state>/carry-testnet and the
default config is config/carry.testnet.yaml (its own LOG_DIR); a testnet
cycle with a mainnet config is refused. Mainnet paths are unchanged.
"""

from pathlib import Path

import pytest
import yaml

import run_carry_cycle as rcc
import settings
from carry import book as book_store
from carry.plan import SymbolBook
from carry_regimes import E8, H, FakeClient, Regime
from helpers import KEY

REPO = Path(__file__).resolve().parent.parent
FILES = ("carry_risk_state_file", "carry_adopt_file", "carry_book_file", "carry_paper_file",
         "carry_hold_file", "carry_cycle_file")


def _paths():
    return {f: getattr(settings, f)() for f in FILES}


def test_mainnet_paths_are_unchanged(isolated_paths):
    state = Path(isolated_paths["YIELD_HERMES_HOME"]) / "state"
    for f, p in _paths().items():
        assert p.parent == state, f
    assert settings.carry_config_file() == REPO / "config" / "carry.yaml"


@pytest.mark.parametrize("where", ["process", "profile"])
def test_testnet_paths_are_separate(isolated_paths, monkeypatch, where):
    mainnet = _paths()
    if where == "process":
        monkeypatch.setenv("BYBIT_TESTNET", "1")
    else:                                       # what the Bybit client also reads
        Path(isolated_paths["YIELD_ENV_FILE"]).write_text("BYBIT_TESTNET=true\n")
    testnet = _paths()
    state = Path(isolated_paths["YIELD_HERMES_HOME"]) / "state" / "carry-testnet"
    for f in FILES:
        assert testnet[f].parent == state, f
        assert testnet[f] != mainnet[f], f
    assert settings.carry_config_file() == REPO / "config" / "carry.testnet.yaml"
    cfg = yaml.safe_load(settings.carry_config_file().read_text())
    main_cfg = yaml.safe_load((REPO / "config" / "carry.yaml").read_text())
    assert cfg["TESTNET_ONLY"] is True and cfg["LOG_DIR"] != main_cfg["LOG_DIR"]


def test_explicit_overrides_still_win(isolated_paths, monkeypatch, tmp_path):
    monkeypatch.setenv("BYBIT_TESTNET", "1")
    monkeypatch.setenv("YIELD_CARRY_BOOK_FILE", str(tmp_path / "b.json"))
    assert settings.carry_book_file() == tmp_path / "b.json"


def test_testnet_cycle_never_touches_the_mainnet_files(isolated_paths, monkeypatch, tmp_path):
    main_book = settings.carry_book_file()
    book_store.save(main_book, KEY, {"ETHUSDT": SymbolBook(status="OPEN", perp_qty=0.03,
                                                           spot_qty=0.03)}, 1)
    before = main_book.read_bytes()
    monkeypatch.setenv("BYBIT_TESTNET", "1")
    cfg = yaml.safe_load(settings.carry_config_file().read_text())
    cfg["LOG_DIR"] = str(tmp_path / "testnet_logs")

    class TestnetClient(FakeClient):
        testnet = True

    import time
    now = int(time.time() * 1000)
    regime = Regime([(40, 0.0003)], start_ms=now - 20 * 24 * H - (now % E8))
    rec, rc = rcc.run_cycle(cfg, {"HERMES_RISK_HMAC_KEY": KEY}, TestnetClient(),
                            snapshot_fn=regime.snapshot_fn(), paper_start=(0.0, 100.0),
                            sleep=lambda s: None)
    assert rc == 0, rec["alerts"]
    assert main_book.read_bytes() == before
    assert not any(a.startswith(("BOOK_MISMATCH", "CRITICAL")) for a in rec["alerts"])
    assert settings.carry_book_file().exists() and settings.carry_book_file() != main_book
    assert settings.carry_paper_file().parent.name == "carry-testnet"
    mainnet_state = Path(isolated_paths["YIELD_HERMES_HOME"]) / "state"
    assert sorted(p.name for p in mainnet_state.iterdir() if p.is_file()) == ["carry_book.json"]


def test_testnet_with_a_mainnet_config_is_refused(isolated_paths, monkeypatch):
    seen = {}

    def fake_run(cfg, env, client, **kw):
        seen.update(cfg=cfg, error=kw.get("config_error"), testnet=client.testnet)
        return {"cycle_id": "x", "alerts": []}, 3

    monkeypatch.setattr(rcc, "run_cycle", fake_run)
    monkeypatch.setenv("BYBIT_TESTNET", "1")
    monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(REPO / "config" / "carry.yaml"))
    rcc.main()
    assert seen["cfg"] is None and "not TESTNET_ONLY" in seen["error"]
    assert seen["testnet"] is True
    lock = Path(isolated_paths["YIELD_HERMES_HOME"]) / "state" / "carry-testnet" / "carry_cycle.lock"
    assert lock.exists()


def test_mainnet_never_loads_the_testnet_config(isolated_paths, monkeypatch):
    seen = {}
    monkeypatch.setattr(rcc, "run_cycle", lambda cfg, env, client, **kw: (
        seen.update(cfg=cfg, error=kw.get("config_error")) or ({"cycle_id": "x", "alerts": []}, 3)))
    monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(REPO / "config" / "carry.testnet.yaml"))
    rcc.main()
    assert seen["cfg"] is None and seen["error"]
