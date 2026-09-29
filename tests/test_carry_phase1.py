"""CARRY_PLAN Phase 1 — config schema, separate risk state, state semantics,
heartbeat per system. Written before the implementation."""

import json
import time
from pathlib import Path

import pytest
import yaml

import heartbeat
import risk_state
import settings
from carry import config as cc
from carry import state as cs
from carry.decide import Params
from helpers import KEY

REPO = Path(__file__).resolve().parent.parent
SHIPPED = REPO / "config" / "carry.yaml"
TESTNET = REPO / "config" / "carry.testnet.yaml"

# Values only live data can give (basis, spread, Phase 2) and the dead-man
# URL (before live, 13.11): null, so the cycle refuses to start until set.
UNSET_IN_SHIPPED = {"MAX_ENTRY_BASIS_BPS", "MAX_SPREAD_BPS", "DEADMAN_URL"}


def complete(**over):
    cfg = yaml.safe_load(SHIPPED.read_text())
    cfg.update(MAX_ENTRY_BASIS_BPS=10, MAX_SPREAD_BPS=5, TOTAL_CAPITAL_CAP_USD=1000,
               MAX_NOTIONAL_PER_SYMBOL_USD=450, USDT_BUFFER_USD=100,
               DEADMAN_URL="https://hc-ping.com/abc")
    cfg.update(over)
    return cfg


def keys_of(errors):
    return {e.split(":", 1)[0] for e in errors}


# --- config ----------------------------------------------------------------- #

def test_shipped_config_is_dry_run_and_lists_exactly_the_unset_decisions():
    cfg = yaml.safe_load(SHIPPED.read_text())
    assert cfg["DRY_RUN"] is True
    assert cfg.get("TESTNET_ONLY", False) is False
    errors = cc.validate(cfg, testnet=False)
    assert keys_of(errors) == UNSET_IN_SHIPPED
    assert all("unset" in e for e in errors)


def test_complete_config_is_valid():
    assert cc.validate(complete(), testnet=False) == []


def test_shipped_thresholds_are_decision_13_8():
    """No calibration set ever entered, so its choice meant nothing; the
    thresholds are Giannis's (decision 13.8)."""
    cfg = yaml.safe_load(SHIPPED.read_text())
    assert (cfg["MIN_HOLD_HOURS"], cfg["SMOOTHING_SETTLEMENTS"], cfg["EXIT_HORIZON_HOURS"],
            cfg["ENTRY_MIN_EXPECTED_APR"]) == (336, 9, 168, 0.05)
    assert "13.8" in cfg["CALIBRATION_SOURCE"]


DAY_MS = 86_400_000
EIGHT_H_MS = 8 * 3_600_000


def _shipped_params():
    """The shipped funding thresholds. Funding history has no order book, so
    the basis/spread checks are off here (as in the calibration); with a limit
    set and basis unknown, decide() never enters."""
    import dataclasses
    return dataclasses.replace(cc.to_params(complete()), max_entry_basis_bps=None,
                               max_spread_bps=None)


def test_steady_funding_of_001_pct_per_8h_enters():
    """0.01 %/8h (~11 % APR) for 30 days against layer A at the measured 1.73 %."""
    from carry import backtest as bt
    start = 1_700_000_000_000 - 1_700_000_000_000 % EIGHT_H_MS
    funding = [(start + i * EIGHT_H_MS, 0.0001) for i in range(30 * 3)]
    res = bt.simulate(funding, [(start, 0.0173)], _shipped_params())
    assert res.entries >= 1


def test_real_180_days_one_entry_per_symbol():
    """The saved VPS data (calibration/carry_data_*.json), locked as measured
    and accepted as correct behaviour (decision 13.12): one entry per symbol
    in late August 2026 (~10 % APR), excess over layer A >= 0, worst 30 days
    >= -0.5 %."""
    from carry import backtest as bt
    path = sorted((REPO / "calibration").glob("carry_data_*.json"))[-1]
    data = json.loads(path.read_text())
    for sym, d in data["symbols"].items():
        res = bt.simulate([tuple(x) for x in d["funding"]], data["layer_a"]["points"],
                          _shipped_params(), symbol=sym)
        assert res.settlements > 500, sym
        assert res.entries == 1, sym
        assert res.excess_apr >= 0, sym
        assert res.worst_30d_return >= -0.005, sym


def test_shipped_capital_is_decision_13_11():
    cfg = yaml.safe_load(SHIPPED.read_text())
    assert cfg["SYMBOLS"] == ["ETHUSDT"]
    assert (cfg["TOTAL_CAPITAL_CAP_USD"], cfg["USDT_BUFFER_USD"], cfg["MAX_NOTIONAL_PER_SYMBOL_USD"],
            cfg["MAX_NOTIONAL_PER_ALT_USD"]) == (100, 15, 80, 30)


def test_capital_rules():
    """Buffer >= 10 % of the cap; the limits of SYMBOLS add up to <= cap - buffer."""
    one = dict(SYMBOLS=["ETHUSDT"], TOTAL_CAPITAL_CAP_USD=100, USDT_BUFFER_USD=15)
    assert cc.validate(complete(**one, MAX_NOTIONAL_PER_SYMBOL_USD=85), testnet=False) == []
    assert "MAX_NOTIONAL_PER_SYMBOL_USD" in keys_of(cc.validate(
        complete(**one, MAX_NOTIONAL_PER_SYMBOL_USD=86), testnet=False))
    assert "USDT_BUFFER_USD" in keys_of(cc.validate(
        complete(**dict(one, USDT_BUFFER_USD=9), MAX_NOTIONAL_PER_SYMBOL_USD=50), testnet=False))
    two = dict(one, SYMBOLS=["BTCUSDT", "ETHUSDT"])
    assert "MAX_NOTIONAL_PER_SYMBOL_USD" in keys_of(cc.validate(
        complete(**two, MAX_NOTIONAL_PER_SYMBOL_USD=80), testnet=False))
    assert cc.validate(complete(**two, MAX_NOTIONAL_PER_SYMBOL_USD=42.5), testnet=False) == []


@pytest.mark.parametrize("field,value", [
    ("DRY_RUN", "true"), ("SYMBOLS", []), ("SYMBOLS", ["SOLUSDT"]), ("SYMBOLS", "BTCUSDT"),
    ("CYCLE_MINUTES", 0), ("SPOT_TAKER_FEE", -0.001), ("SMOOTHING_SETTLEMENTS", 0),
    ("SMOOTHING_SETTLEMENTS", 2.5), ("MAX_ROUND_TRIPS_PER_30D", -1), ("LEG_TIMEOUT_S", 0),
    ("REDEEM_TIMEOUT_HOURS", 0), ("REDEEM_TIMEOUT_HOURS", None), ("ADL_RANK_REDUCE", 6),
    ("MAX_NOTIONAL_PER_SYMBOL_USD", 5000), ("DEADMAN_URL", "http://insecure"),
    ("EARN_COIN", "USDC"), ("ACCOUNT_TYPE", "CONTRACT"),
])
def test_bad_value_is_rejected(field, value):
    assert field in keys_of(cc.validate(complete(**{field: value}), testnet=False))


def test_missing_field_is_rejected():
    cfg = complete()
    cfg.pop("REDEEM_TIMEOUT_HOURS")
    assert "REDEEM_TIMEOUT_HOURS" in keys_of(cc.validate(cfg, testnet=False))


def test_mmr_thresholds_must_be_ordered():
    errors = cc.validate(complete(MMR_WARN=0.5, MMR_REDUCE=0.4), testnet=False)
    assert "MMR_WARN" in keys_of(errors)


def test_hysteresis_is_enforced_in_config():
    errors = cc.validate(complete(ENTRY_MIN_PREDICTED_RATE=-0.001, EXIT_PREDICTED_FLOOR=0.0),
                         testnet=False)
    assert "ENTRY_MIN_PREDICTED_RATE" in keys_of(errors)


@pytest.mark.parametrize("field,value", [
    ("NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN", 10), ("ENTRY_EV_MULTIPLE", 0.5),
    ("ENTRY_MIN_EXPECTED_APR", -0.01), ("ENTRY_MIN_PREDICTED_RATE", 0.0),
    ("MIN_HOLD_HOURS", 4),
])
def test_production_floors_cannot_be_relaxed(field, value):
    """Decision 13.1: no threshold is loosened to make the strategy trade."""
    assert field in keys_of(cc.validate(complete(**{field: value}), testnet=False))


def test_testnet_config_refused_on_mainnet():
    cfg = yaml.safe_load(TESTNET.read_text())
    assert cfg["TESTNET_ONLY"] is True
    assert "TESTNET_ONLY" in keys_of(cc.validate(cfg, testnet=False))
    assert cc.validate(cfg, testnet=True) == []


def test_testnet_config_forces_an_entry_but_keeps_the_window():
    """The forced cycle must still respect the settlement window and dry-run
    defaults; only the entry thresholds are lowered."""
    cfg = yaml.safe_load(TESTNET.read_text())
    assert cfg["NO_FUNDING_ACTION_BEFORE_SETTLEMENT_MIN"] >= 15
    assert cfg["DRY_RUN"] is True
    assert cfg["ENTRY_EV_MULTIPLE"] == 0 and cfg["MIN_HOLD_HOURS"] == 0


def test_config_to_params():
    p = cc.to_params(complete())
    assert isinstance(p, Params)
    assert p.max_round_trips_30d == complete()["MAX_ROUND_TRIPS_PER_30D"]
    assert p.entry_ev_multiple == 1.0


def test_load_refuses_invalid(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(complete(DRY_RUN="yes")))
    with pytest.raises(cc.CarryConfigError) as e:
        cc.load(path, testnet=False)
    assert "DRY_RUN" in str(e.value)
    path.write_text(yaml.safe_dump(complete()))
    assert cc.load(path, testnet=False)["DRY_RUN"] is True


# --- separate risk state ------------------------------------------------------ #

def test_carry_state_file_is_separate(isolated_paths):
    assert settings.carry_risk_state_file() != settings.risk_state_file()
    assert settings.carry_risk_state_file().name == "carry_risk_state.json"


def test_profiles_cannot_be_swapped(tmp_path):
    y, c = tmp_path / "y.json", tmp_path / "c.json"
    risk_state.write(y, KEY, "NORMAL", "x", risk_state.SOURCE_OPERATOR)
    risk_state.write(c, KEY, "NORMAL", "x", risk_state.SOURCE_OPERATOR, profile=cs.PROFILE)
    assert risk_state.verify(c, KEY, profile=cs.PROFILE).ok
    assert risk_state.verify(y, KEY, profile=cs.PROFILE).code == risk_state.CODE_MALFORMED
    assert risk_state.verify(c, KEY).code == risk_state.CODE_MALFORMED


def _write_carry(path, state, age_s=0, source=risk_state.SOURCE_OPERATOR, key=KEY):
    return risk_state.write(path, key, state, "t", source,
                            ts_ms=int((time.time() - age_s) * 1000), profile=cs.PROFILE)


@pytest.mark.parametrize("state,age_s,expected", [
    ("NORMAL", 0, "NORMAL"), ("NORMAL", 7200, "NO_NEW_POSITIONS"),
    ("NO_NEW_POSITIONS", 7200, "NO_NEW_POSITIONS"), ("UNWIND", 7200, "UNWIND"),
    ("UNWIND", 0, "UNWIND"),
])
def test_staleness_only_more_conservative(isolated_paths, monkeypatch, state, age_s, expected):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    _write_carry(settings.carry_risk_state_file(), state, age_s)
    effective, meta, alerts = cs.resolve()
    assert effective == expected
    assert meta["signature_valid"] is True


@pytest.mark.parametrize("content", [None, "{", '{"state": "NORMAL"}'])
def test_invalid_or_missing_carry_state_is_no_new_positions(isolated_paths, monkeypatch, content):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    path = settings.carry_risk_state_file()
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    effective, meta, alerts = cs.resolve()
    assert effective == "NO_NEW_POSITIONS" and alerts


def test_yield_state_does_not_leak_into_carry(isolated_paths, monkeypatch):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    risk_state.write(settings.risk_state_file(), KEY, "NORMAL", "yield", risk_state.SOURCE_OPERATOR)
    effective, _, alerts = cs.resolve()
    assert effective == "NO_NEW_POSITIONS" and "RISK_STATE_MISSING" in alerts


# --- state semantics (§6 table + decision 13) ----------------------------------- #

ACTIONS = ("ENTER", "EXIT", "TRIM", "REBALANCE_TOWARD_NEUTRAL", "REBALANCE_AWAY_FROM_NEUTRAL",
           "EARN_REDEEM_FOR_ENTRY", "EARN_RETURN")


def test_permission_matrix_is_exact():
    allowed = {s: {a for a in ACTIONS if cs.is_allowed(s, a)} for s in cs.STATES}
    assert allowed == {
        "NORMAL": set(ACTIONS),
        "NO_NEW_POSITIONS": {"EXIT", "TRIM", "REBALANCE_TOWARD_NEUTRAL", "EARN_RETURN"},
        "UNWIND": {"EXIT", "TRIM", "REBALANCE_TOWARD_NEUTRAL", "EARN_RETURN"},
    }


def test_unknown_state_or_action_is_denied():
    assert cs.is_allowed("PANIC", "ENTER") is False
    assert cs.is_allowed("NORMAL", "SOMETHING_NEW") is False


def test_exits_are_always_allowed():
    for s in cs.STATES + ("PANIC",):
        assert cs.is_allowed(s, "EXIT")


# --- heartbeat per system ------------------------------------------------------ #

@pytest.fixture
def carry_hb(isolated_paths, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")
    log_dir = tmp_path / "carry_logs"
    log_dir.mkdir()
    cfg = complete(LOG_DIR=str(log_dir))
    cfg_path = tmp_path / "carry.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(cfg_path))
    return {"log_dir": log_dir, "state": settings.carry_risk_state_file()}


def _carry_cycle_record(log_dir, state_rec, alerts=()):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "cycle_id": "c",
           "alerts": list(alerts),
           "risk_state_meta": {"signature_valid": True, "ts": state_rec["ts"]}}
    (log_dir / time.strftime("%Y-%m-%d.jsonl", time.gmtime())).write_text(json.dumps(rec) + "\n")


def test_carry_heartbeat_bootstraps_its_own_file(carry_hb, isolated_paths):
    assert heartbeat.main(["--system", "carry"]) == 0
    v = risk_state.verify(carry_hb["state"], KEY, profile=cs.PROFILE)
    assert (v.state, v.source) == ("NO_NEW_POSITIONS", "heartbeat_bootstrap")
    assert not Path(isolated_paths["YIELD_STATE_FILE"]).exists()


def test_carry_heartbeat_promotes_after_clean_carry_cycle(carry_hb):
    heartbeat.main(["--system", "carry"])
    boot = json.loads(carry_hb["state"].read_text())
    time.sleep(1.1)
    _carry_cycle_record(carry_hb["log_dir"], boot)
    assert heartbeat.main(["--system", "carry"]) == 0
    v = risk_state.verify(carry_hb["state"], KEY, profile=cs.PROFILE)
    assert (v.state, v.source) == ("NORMAL", "heartbeat_renew")


@pytest.mark.parametrize("code", ["ORPHAN_LEG", "ADL_DETECTED", "MARGIN_EMERGENCY",
                                  "FOREIGN_ACTIVITY", "REGION_RESTRICTED", "CYCLE_CRASH"])
def test_carry_blocking_codes_stop_promotion(carry_hb, code):
    heartbeat.main(["--system", "carry"])
    boot = json.loads(carry_hb["state"].read_text())
    time.sleep(1.1)
    _carry_cycle_record(carry_hb["log_dir"], boot, alerts=[f"{code}: x"])
    heartbeat.main(["--system", "carry"])
    v = risk_state.verify(carry_hb["state"], KEY, profile=cs.PROFILE)
    assert v.state == "NO_NEW_POSITIONS"


def test_carry_blocking_codes_are_exact():
    assert set(heartbeat.SYSTEMS["carry"].blocking_codes) == {
        "CONFIG_INCOMPLETE", "CRITICAL", "CYCLE_CRASH", "ORPHAN_LEG", "ADL_DETECTED",
        "LIQUIDATION_DETECTED", "MARGIN_EMERGENCY", "FOREIGN_ACTIVITY", "REGION_RESTRICTED",
        "EARN_REDEEM_STUCK", "UNTRACKED_POSITION", "USDT_BORROW_LIMIT"}
    assert heartbeat.SYSTEMS["yield"].blocking_codes == heartbeat.BLOCKING_CODES


def test_operator_carry_state_is_never_touched(carry_hb):
    _write_carry(carry_hb["state"], "UNWIND", age_s=7200)
    before = carry_hb["state"].read_bytes()
    heartbeat.main(["--system", "carry"])
    assert carry_hb["state"].read_bytes() == before


def test_yield_heartbeat_unchanged_default(isolated_paths, monkeypatch):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")
    assert heartbeat.main([]) == 0
    assert risk_state.verify(settings.risk_state_file(), KEY).source == "heartbeat_bootstrap"
    assert not settings.carry_risk_state_file().exists()


def test_risk_state_cli_can_target_carry(isolated_paths, monkeypatch):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    assert risk_state.main(["--system", "carry", "write", "UNWIND", "stop"]) == 0
    v = risk_state.verify(settings.carry_risk_state_file(), KEY, profile=cs.PROFILE)
    assert (v.state, v.source) == ("UNWIND", "operator")
    assert not Path(isolated_paths["YIELD_STATE_FILE"]).exists()
