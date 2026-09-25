"""Phase 3 — data integrity: real fields only, unknown = null, strict
config, no silent cycle death."""

import json
import time
from pathlib import Path

import pytest

import heartbeat
import run_yield_cycle as ryc
from helpers import (KEY, FakeAgent, FakeBybit, apr_history, decision_json, load_cfg,
                     position, product, read_log, reply_with, stake_executions,
                     write_state)

STAKE_1 = {"action": "STAKE", "coin": "USDT", "product_id": "1", "reason": "apr 0.012 >= 0.001"}
HOLD = {"action": "HOLD", "coin": "USDT", "product_id": "1", "reason": "nothing"}
HOUR_MS = 3600 * 1000


@pytest.fixture
def env(isolated_paths, monkeypatch):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    write_state(isolated_paths["YIELD_STATE_FILE"], "NORMAL")
    return isolated_paths


def run(env, tool=None, agent=None, **over):
    cfg = load_cfg(env, **over)
    return ryc.run_cycle(cfg, tool=tool or FakeBybit(), agent=agent or FakeAgent(reply_with(HOLD)))


def scan_of(env, tool, **over):
    return ryc.collect_inputs(tool, load_cfg(env, **over))


# --- T3.1 ------------------------------------------------------------------ #

def test_redemption_eta_from_bybit(env):
    data = scan_of(env, FakeBybit(products=[product("1", redeem_minutes="2880")]))
    assert data["scan"] == [] or data["scan"][0]["redemption_eta_hours"] == 48.0
    info = data["product_info"]["1"]
    assert info["redemption_eta_hours"] == 48.0


def test_redemption_eta_missing_is_null(env):
    data = scan_of(env, FakeBybit(products=[product("1", redeem_minutes=None)]))
    assert data["product_info"]["1"]["redemption_eta_hours"] is None


@pytest.mark.parametrize("minutes,staked", [("0", True), ("60", True), ("2880", False), (None, False)])
def test_stake_requires_known_liquid_product(env, minutes, staked):
    tool = FakeBybit(products=[product("1", redeem_minutes=minutes)])
    rec, _ = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)))
    assert bool([s for s in stake_executions(rec) if s.get("would_call")]) is staked


def test_positions_carry_product_liquidity(env):
    tool = FakeBybit(products=[product("1", redeem_minutes="2880")],
                     positions=[position("1", amount="5")])
    data = scan_of(env, tool)
    [p] = data["positions"]
    assert p["product_status"] == "Available" and p["redemption_eta_hours"] == 48.0


# --- T3.2 ------------------------------------------------------------------ #

def _hist(points, apr_of=lambda i: "1%"):
    now = int(time.time() * 1000)
    return [{"timestamp": str(now - h * HOUR_MS), "apr": apr_of(i)} for i, h in enumerate(points)]


def test_apr_history_order_independent(env):
    # hourly points 0.5h..30.5h old; the 24h window holds 24 of them
    ages = [0.5 + i for i in range(31)]
    asc = _hist(list(reversed(ages)), lambda i: "1%" if i >= 7 else "9%")  # old points = 9%
    desc = list(reversed(asc))
    for hist in (asc, desc):
        [p] = scan_of(env, FakeBybit(history=hist))["scan"]
        assert p["apr_ma_24h"] == pytest.approx(0.01)


def test_apr_ma_uses_time_window_not_last_24_records(env):
    # 48 half-hourly points: the last 24 records only span 12 h
    ages = [0.25 + 0.5 * i for i in range(48)]
    hist = _hist(ages, lambda i: "1%" if ages[i] <= 12 else "3%")
    [p] = scan_of(env, FakeBybit(history=hist))["scan"]
    assert p["apr_ma_24h"] == pytest.approx((24 * 0.01 + 24 * 0.03) / 48)


def test_apr_ma_null_with_fewer_than_6_points(env):
    data = scan_of(env, FakeBybit(history=_hist([0.5, 1.5, 2.5, 3.5, 4.5])))
    assert data["scan"] == []
    assert any(f["reason"].startswith("NO_APR_MA_24H") for f in data["filtered"])


def test_apr_history_is_requested_per_product(env):
    seen = []

    class Tool(FakeBybit):
        def get_earn_apr_history(self, coin=None, product_id=None, **kw):
            seen.append((coin, product_id))
            return apr_history()

    scan_of(env, Tool(products=[product("1"), product("2")]))
    assert seen == [(None, "1"), (None, "2")]


# --- T3.3 / T3.4 ------------------------------------------------------------ #

FAKE_FIELDS = {"apr_ma_7d", "apr_p25_180d", "apr_p75_180d", "tier_cap_amount",
               "marginal_apr_for_size"}


def test_no_hardcoded_fields_in_scan(env):
    [p] = scan_of(env, FakeBybit())["scan"]
    assert not FAKE_FIELDS & set(p)


def test_prompt_does_not_mention_fake_fields():
    text = (Path(__file__).resolve().parent.parent / "prompt_v6.md").read_text()
    for field in FAKE_FIELDS:
        assert field not in text


def test_tiered_product_is_not_staked(env):
    data = scan_of(env, FakeBybit(products=[product("1", hasTieredApr=True)]))
    assert data["scan"] == []
    assert any(f["reason"].startswith("TIERED_APR") for f in data["filtered"])


def test_positions_are_numeric_snake_case(env):
    [p] = scan_of(env, FakeBybit(positions=[position("1", amount="5.5")]))["positions"]
    assert set(p) >= {"product_id", "coin", "amount", "status"}
    assert isinstance(p["amount"], float) and p["amount"] == 5.5


# --- T3.5 ------------------------------------------------------------------ #

def test_agent_timeout_is_distinct_and_non_blocking(env, monkeypatch):
    import subprocess

    def slow(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=1, output="partial")

    monkeypatch.setattr(ryc.subprocess, "run", slow)
    rec, rc = run(env, agent=ryc.call_agent)
    assert "AGENT_TIMEOUT" in [a.split(":")[0] for a in rec["alerts"]]
    assert not any(a.startswith("AGENT_PARSE_ERROR") for a in rec["alerts"])
    assert heartbeat.check_last_cycle_ok(Path(env["LOG_DIR"]))[0] is True


def test_network_problem_does_not_freeze_heartbeat(env):
    run(env, tool=FakeBybit(fail={"get_earn_products", "get_wallet_balance"}))
    assert heartbeat.check_last_cycle_ok(Path(env["LOG_DIR"]))[0] is True


def test_blocking_codes_include_cycle_crash():
    assert "CYCLE_CRASH" in heartbeat.BLOCKING_CODES
    assert "DATA_UNAVAILABLE" not in heartbeat.BLOCKING_CODES
    assert "AGENT_TIMEOUT" not in heartbeat.BLOCKING_CODES


# --- T3.6 ------------------------------------------------------------------ #

REQUIRED = ["ACCOUNT_TYPE", "COIN_WHITELIST", "CYCLE_INTERVAL_MINUTES", "DRY_RUN", "ENTRY_APR",
            "EXIT_APR", "LOG_DIR", "MAX_PER_PRODUCT_USD", "MAX_REDEMPTION_ETA_HOURS",
            "MIN_APR_EDGE", "MIN_MOVE_USD", "PROMPT_VERSION", "REQUESTED_REASONING",
            "RESERVE_USD", "RESOLVED_MODEL", "MAX_SCAN_AGE_SECONDS", "MAX_APR_HISTORY_GAP_HOURS"]


@pytest.mark.parametrize("field", REQUIRED)
def test_missing_config_field_hard_fails_with_record(env, field):
    cfg = load_cfg(env)
    cfg.pop(field)
    agent = FakeAgent(reply_with(HOLD))
    rec, rc = ryc.run_cycle(cfg, tool=FakeBybit(), agent=agent)
    assert rc == 3 and agent.called is False
    assert any(a.startswith("CONFIG_INCOMPLETE") and field in a for a in rec["alerts"])
    logged = read_log(env["LOG_DIR"]) if field != "LOG_DIR" else read_log(
        Path(env["YIELD_HERMES_HOME"]) / "logs" / "yield_rotation")
    assert logged[-1]["cycle_id"] == rec["cycle_id"]


@pytest.mark.parametrize("field,value", [
    ("MAX_PER_PRODUCT_USD", "5"), ("MAX_PER_PRODUCT_USD", 0), ("MAX_PER_PRODUCT_USD", True),
    ("ENTRY_APR", -0.1), ("ENTRY_APR", 5), ("DRY_RUN", "true"), ("COIN_WHITELIST", []),
    ("COIN_WHITELIST", "USDT"), ("RESERVE_USD", -1), ("CYCLE_INTERVAL_MINUTES", 0),
    ("PROMPT_VERSION", "../etc"), ("SIMULATED_IDLE_BALANCE", "100"),
])
def test_bad_config_value_hard_fails(env, field, value):
    rec, rc = run(env, **{field: value})
    assert rc == 3
    assert any(a.startswith("CONFIG_INCOMPLETE") and field in a for a in rec["alerts"])


def test_simulated_balance_with_live_hard_fails(env):
    rec, rc = run(env, DRY_RUN=False, SIMULATED_IDLE_BALANCE=100)
    assert rc == 3 and any("SIMULATED_IDLE_BALANCE" in a for a in rec["alerts"])


def test_shipped_config_is_valid():
    cfg = __import__("yaml").safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "yield_rotation.yaml").read_text())
    assert ryc.validate_config(cfg) == []
    assert cfg["DRY_RUN"] is True


# --- T3.7 ------------------------------------------------------------------ #

@pytest.mark.parametrize("target", ["resolve_risk_state", "collect_inputs", "compose_prompt",
                                    "validate_decision_record", "apply_risk_gate", "build_plan",
                                    "unavailable_redeems"])
def test_unexpected_exception_writes_cycle_crash(env, monkeypatch, target):
    def boom(*a, **kw):
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(ryc, target, boom)
    rec, rc = run(env, agent=FakeAgent(reply_with(HOLD)))
    assert rc != 0
    assert any(a.startswith("CYCLE_CRASH") for a in rec["alerts"])
    last = read_log(env["LOG_DIR"])[-1]
    assert last["cycle_id"] == rec["cycle_id"] and "ZeroDivisionError" in last["crash"]
    assert heartbeat.check_last_cycle_ok(Path(env["LOG_DIR"]))[0] is False


def test_executor_exception_writes_cycle_crash(env, monkeypatch):
    monkeypatch.setattr(ryc.Executor, "execute", lambda self, o: 1 / 0)
    rec, rc = run(env, agent=FakeAgent(reply_with(STAKE_1)))
    assert any(a.startswith("CYCLE_CRASH") for a in rec["alerts"])


def test_main_with_unreadable_config_writes_record(env, monkeypatch, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("DRY_RUN: [unclosed\n")
    monkeypatch.setattr("sys.argv", ["run_yield_cycle.py", "--config", str(bad)])
    with pytest.raises(SystemExit) as e:
        ryc.main()
    assert e.value.code == 3
    log_dir = Path(env["YIELD_HERMES_HOME"]) / "logs" / "yield_rotation"
    assert any(a.startswith("CONFIG_INCOMPLETE") for a in read_log(log_dir)[-1]["alerts"])
