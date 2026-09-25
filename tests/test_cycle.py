"""Phase 1 — wrapper (run_yield_cycle) safety tests.  No LLM, no network.

Each test drives run_yield_cycle.run_cycle() end to end with FakeBybit and
FakeAgent and inspects the decision record it writes.
"""

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

import risk_state
import run_yield_cycle as ryc
from executor import Executor
from helpers import (KEY, FakeAgent, FakeBybit, decision_json, load_cfg,
                     position, product, read_log, redeem_executions,
                     reply_with, stake_executions, write_state)

REPO = Path(__file__).resolve().parent.parent
BLOCKING = ("CONFIG_INCOMPLETE", "CRITICAL", "AGENT_PARSE_ERROR",
            "DECISION_VALIDATION_FAILED")

STAKE_1 = {"action": "STAKE", "coin": "USDT", "product_id": "1", "reason": "apr 0.012 >= 0.001"}
HOLD = {"action": "HOLD", "coin": "USDT", "product_id": "1", "reason": "nothing to do"}


@pytest.fixture
def env(isolated_paths, monkeypatch):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    return isolated_paths


def run(env, tool=None, agent=None, **cfg_overrides):
    cfg = load_cfg(env, **cfg_overrides)
    tool = tool or FakeBybit()
    agent = agent or FakeAgent(reply_with(HOLD))
    rec, rc = ryc.run_cycle(cfg, tool=tool, agent=agent)
    return rec, rc


def blocking_alerts(rec):
    return [a for a in rec["alerts"] if any(a == c or a.startswith(c + ":") for c in BLOCKING)]


# --------------------------------------------------------------------------- #
# T1.1 — deterministic risk-state gate                                        #
# --------------------------------------------------------------------------- #

def test_stake_allowed_under_normal(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    rec, rc = run(env, agent=FakeAgent(reply_with(STAKE_1)))
    assert rc == 0
    stakes = stake_executions(rec)
    assert len(stakes) == 1 and stakes[0]["amount"] == "5"
    assert stakes[0]["executed"] is False  # DRY_RUN


def test_stake_blocked_under_no_new_positions(env):
    write_state(env["YIELD_STATE_FILE"], "NO_NEW_POSITIONS")
    tool = FakeBybit()
    rec, rc = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), DRY_RUN=True)
    assert stake_executions(rec) == []
    assert all(d["action"] != "STAKE" for d in rec["decisions"])
    assert any(a.startswith("RISK_GATE_DROPPED_STAKE") for a in rec["alerts"])
    assert blocking_alerts(rec) == []
    assert tool.calls == []


def test_unwind_redeems_without_llm(env):
    write_state(env["YIELD_STATE_FILE"], "UNWIND")
    agent = FakeAgent(reply_with(STAKE_1))
    tool = FakeBybit(positions=[position("1", amount="5")])
    rec, rc = run(env, tool=tool, agent=agent)
    assert agent.called is False
    assert rec["agent_called"] is False
    assert [d["action"] for d in rec["decisions"]] == ["REDEEM_ALL"]
    redeems = redeem_executions(rec)
    assert [(r["product_id"], r["amount"]) for r in redeems] == [("1", "5")]
    assert stake_executions(rec) == []


def test_executor_refuses_stake_without_permission():
    tool = FakeBybit()
    ex = Executor(bybit_tool=tool, dry_run=False, allow_new_positions=False)
    [rec] = ex.execute([{"action": "STAKE", "coin": "USDT", "product_id": "1",
                         "amount": "5", "origin": "agent", "reason": "x"}])
    assert rec["executed"] is False and "RISK_GATE" in rec["reason"]
    assert tool.calls == []


# --------------------------------------------------------------------------- #
# T1.2 — staleness only ever makes the system more conservative               #
# --------------------------------------------------------------------------- #

def test_stale_unwind_still_redeems(env):
    write_state(env["YIELD_STATE_FILE"], "UNWIND", age_s=2 * 3600)
    agent = FakeAgent(reply_with(HOLD))
    rec, rc = run(env, tool=FakeBybit(positions=[position("1", amount="5")]), agent=agent)
    assert rc == 0
    assert rec["risk_state"] == "UNWIND"
    assert agent.called is False
    assert [(r["product_id"], r["amount"]) for r in redeem_executions(rec)] == [("1", "5")]
    assert "RISK_STATE_STALE" in rec["alerts"]


def test_stale_normal_becomes_no_new_positions(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL", age_s=2 * 3600)
    rec, rc = run(env, agent=FakeAgent(reply_with(STAKE_1)))
    assert rc == 0
    assert rec["risk_state"] == "NO_NEW_POSITIONS"
    assert stake_executions(rec) == []


def test_invalid_signature_allows_redeem_not_stake(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL", key="some-other-key")
    redeem = {"action": "REDEEM", "coin": "USDT", "product_id": "1", "reason": "apr 0 < EXIT_APR 0.001"}
    tool = FakeBybit(positions=[position("1", amount="3")])
    rec, rc = run(env, tool=tool, agent=FakeAgent(reply_with(redeem, STAKE_1)))
    assert rc == 0
    assert rec["risk_state"] == "NO_NEW_POSITIONS"
    assert "RISK_STATE_BAD_SIGNATURE" in rec["alerts"]
    assert [(r["product_id"], r["amount"]) for r in redeem_executions(rec)] == [("1", "3")]
    assert stake_executions(rec) == []


def test_missing_state_continues_cycle(env):
    agent = FakeAgent(reply_with(HOLD))
    rec, rc = run(env, agent=agent)
    assert rc == 0 and agent.called
    assert rec["risk_state"] == "NO_NEW_POSITIONS"
    assert "RISK_STATE_MISSING" in rec["alerts"]
    assert read_log(env["LOG_DIR"])[-1]["cycle_id"] == rec["cycle_id"]


def test_risk_state_meta_is_structured(env):
    written = write_state(env["YIELD_STATE_FILE"], "NORMAL", source=risk_state.SOURCE_BOOTSTRAP)
    rec, _ = run(env)
    meta = rec["risk_state_meta"]
    assert meta["code"] == "OK" and meta["signature_valid"] is True and meta["fresh"] is True
    assert meta["ts"] == written["ts"] and meta["source"] == "heartbeat_bootstrap"


# --------------------------------------------------------------------------- #
# T1.5 — the wrapper computes every amount                                     #
# --------------------------------------------------------------------------- #

def test_amount_never_exceeds_cap(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    rec, _ = run(env, agent=FakeAgent(reply_with(STAKE_1)),
                 SIMULATED_IDLE_BALANCE=100, MAX_PER_PRODUCT_USD=5)
    [stake] = stake_executions(rec)
    assert Decimal(stake["amount"]) <= 5


def test_cap_counts_existing_position(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(positions=[position("1", amount="3")])
    rec, _ = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), MAX_PER_PRODUCT_USD=5)
    [stake] = stake_executions(rec)
    assert stake["amount"] == "2"


def test_amount_uses_min_of_limits_and_rounds_down(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(products=[product("1", remaining="7.567", precision="2")])
    rec, _ = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)),
                 SIMULATED_IDLE_BALANCE=10.5, RESERVE_USD=1, MAX_PER_PRODUCT_USD=50)
    [stake] = stake_executions(rec)
    assert stake["amount"] == "7.56"


def test_amount_below_floor_is_skipped(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(products=[product("1", min_stake="10")])
    rec, rc = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), MAX_PER_PRODUCT_USD=5)
    assert rc == 0
    assert [e for e in stake_executions(rec) if e.get("would_call")] == []
    assert any("below" in e["reason"] for e in stake_executions(rec))


def test_unknown_precision_skips_stake(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(products=[product("1", precision=None)])
    rec, _ = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)))
    assert [e for e in stake_executions(rec) if e.get("would_call")] == []


@pytest.mark.parametrize("slip", [{"amount_usd": "5"}, {"amount_usd": 80}, {"amount": 5},
                                  {"amount_usd": None}, {"product_id": 1}])
def test_llm_type_slip_does_not_crash(env, slip):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    stake = {**STAKE_1, **slip}
    rec, rc = run(env, agent=FakeAgent(reply_with(stake)), MAX_PER_PRODUCT_USD=5)
    assert rc == 0
    assert read_log(env["LOG_DIR"])[-1]["cycle_id"] == rec["cycle_id"]
    assert all(Decimal(s["amount"]) <= 5 for s in stake_executions(rec) if s.get("amount"))


def test_duplicate_stake_for_same_product_executes_once(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    rec, _ = run(env, agent=FakeAgent(reply_with(STAKE_1, STAKE_1)))
    assert len([s for s in stake_executions(rec) if s.get("would_call")]) == 1


# --------------------------------------------------------------------------- #
# T1.6 — REDEEM                                                               #
# --------------------------------------------------------------------------- #

def test_redeem_unavailable_product(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(products=[product("1", status="NotAvailable")],
                     positions=[position("1", amount="5")])
    rec, rc = run(env, tool=tool, agent=FakeAgent(reply_with(HOLD)))
    assert rc == 0
    assert blocking_alerts(rec) == []
    assert [(r["product_id"], r["amount"], r["origin"]) for r in redeem_executions(rec)] \
        == [("1", "5", "wrapper")]


def test_agent_redeem_checked_against_positions_not_scan(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(products=[product("1"), product("2", status="NotAvailable")],
                     positions=[position("2", amount="4")])
    redeem = {"action": "REDEEM", "coin": "USDT", "product_id": "2", "reason": "status NotAvailable"}
    rec, rc = run(env, tool=tool, agent=FakeAgent(reply_with(redeem)))
    assert blocking_alerts(rec) == []
    assert [(r["product_id"], r["amount"]) for r in redeem_executions(rec)] == [("2", "4")]


def test_redeem_of_unheld_product_rejected(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    redeem = {"action": "REDEEM", "coin": "USDT", "product_id": "9", "reason": "x"}
    rec, _ = run(env, agent=FakeAgent(reply_with(redeem)))
    assert any(a.startswith("CRITICAL") for a in rec["alerts"])
    assert redeem_executions(rec) == []


def test_redeem_uses_product_id_field_only(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    redeem = {"action": "REDEEM", "coin": "USDT", "from_product_id": "1", "reason": "x"}
    rec, _ = run(env, tool=FakeBybit(positions=[position("1")]), agent=FakeAgent(reply_with(redeem)))
    assert any(a.startswith("DECISION_VALIDATION_FAILED") for a in rec["alerts"])


def test_rotate_is_not_accepted(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    rotate = {"action": "ROTATE", "coin": "USDT", "product_id": "1", "reason": "x"}
    rec, _ = run(env, agent=FakeAgent(reply_with(rotate)))
    assert any(a.startswith("DECISION_VALIDATION_FAILED") for a in rec["alerts"])
    assert rec["executions"] == [] or all(not e.get("would_call") for e in rec["executions"])


# --------------------------------------------------------------------------- #
# T1.7 — prompt selection                                                     #
# --------------------------------------------------------------------------- #

def test_production_uses_configured_prompt(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    agent = FakeAgent(reply_with(HOLD))
    rec, _ = run(env, agent=agent, PROMPT_VERSION="v5")
    body = (REPO / "prompt_v5.md").read_bytes()
    assert rec["prompt_file"] == "prompt_v5.md"
    assert rec["prompt_sha256"] == hashlib.sha256(body).hexdigest()
    assert body.decode() in agent.prompts[0]


def test_missing_prompt_file_hard_fails(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    agent = FakeAgent(reply_with(HOLD))
    rec, rc = run(env, agent=agent, PROMPT_VERSION="v99")
    assert rc != 0
    assert agent.called is False
    assert any(a.startswith("CONFIG_INCOMPLETE") for a in rec["alerts"])
    assert read_log(env["LOG_DIR"])[-1]["cycle_id"] == rec["cycle_id"]


def test_shipped_config_uses_v6_and_v4_is_archived():
    cfg = yaml.safe_load((REPO / "config" / "yield_rotation.yaml").read_text())
    assert cfg["PROMPT_VERSION"] == "v6"
    assert (REPO / "prompt_v6.md").exists()
    assert not (REPO / "prompt_v4.md").exists()
    assert (REPO / "archive" / "prompt_v4.md").exists()


def test_v6_prompt_does_not_ask_llm_for_amounts():
    text = (REPO / "prompt_v6.md").read_text()
    assert "amount_usd" not in text
    assert "from_product_id" not in text
    assert "ROTATE" not in text


# --------------------------------------------------------------------------- #
# T1.8 — JSON extraction                                                      #
# --------------------------------------------------------------------------- #

EXAMPLE = json.dumps({"cycle_id": "EXAMPLE", "risk_state": "NORMAL", "decisions": [
    {"action": "STAKE", "coin": "USDT", "product_id": "1", "reason": "example"}]})


def test_prompt_example_not_executed(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    agent = FakeAgent(lambda cid: EXAMPLE + "\n\n" + decision_json(cid, [HOLD]))
    rec, _ = run(env, agent=agent)
    assert stake_executions(rec) == []
    assert [d["action"] for d in rec["decisions"]] == ["HOLD"]


def test_output_for_other_cycle_is_parse_error(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    agent = FakeAgent(lambda cid: decision_json("20200101_000000_abcdef", [STAKE_1]))
    rec, _ = run(env, agent=agent)
    assert any(a.startswith("AGENT_PARSE_ERROR") for a in rec["alerts"])
    assert stake_executions(rec) == []


def test_extract_json_takes_last_object_for_cycle():
    raw = (decision_json("c1", [STAKE_1]) + "\nthinking...\n" + decision_json("c1", [HOLD])
           + '\n{"unrelated": true}')
    assert ryc.extract_json(raw, "c1")["decisions"] == [HOLD]
    with pytest.raises(ValueError):
        ryc.extract_json(decision_json("c2", [HOLD]), "c1")


def test_prompt_examples_use_impossible_ids():
    text = (REPO / "prompt_v6.md").read_text()
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    assert blocks, "prompt must contain an example"
    for b in blocks:
        obj = json.loads(b)
        assert obj["cycle_id"] == "EXAMPLE"
        for d in obj.get("decisions", []):
            if "product_id" in d:
                assert d["product_id"] == "EXAMPLE"


# --------------------------------------------------------------------------- #
# T1.9 — model check                                                          #
# --------------------------------------------------------------------------- #

def test_fallback_word_is_harmless(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    hold = {**HOLD, "reason": "no fallback needed; apr 0.012 steady"}
    rec, rc = run(env, agent=FakeAgent(lambda cid: "fallback\n" + decision_json(cid, [hold])))
    assert rc == 0
    assert rec["alerts"] == []
    assert rec["model_requested_on_cli"] == "google/gemini-2.5-flash"


# --------------------------------------------------------------------------- #
# T1.10 — agent isolation                                                     #
# --------------------------------------------------------------------------- #

class _Proc:
    returncode = 0
    stdout = "{}"
    stderr = ""


def test_agent_command_is_exact(env, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _Proc()

    monkeypatch.setattr(ryc.subprocess, "run", fake_run)
    cfg = load_cfg(env)
    prompt = "SECRET-PROMPT-BODY " * 10
    ryc.call_agent(prompt, cfg, "cid")
    assert seen["cmd"] == [
        str(Path(env["YIELD_HERMES_HOME"]) / ".venv" / "bin" / "hermes"), "chat",
        "--query-file", "/dev/stdin", "-Q", "--toolsets=",
        "-m", "google/gemini-2.5-flash", "--reasoning", "medium",
    ]
    assert seen["kw"]["input"] == prompt
    assert all("SECRET-PROMPT-BODY" not in part for part in seen["cmd"])
    assert "HERMES_RISK_HMAC_KEY" not in seen["kw"]["env"]


def test_regression_uses_same_command(env, monkeypatch):
    import run_regression
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _Proc()

    monkeypatch.setattr(run_regression.subprocess, "run", fake_run)
    cfg = load_cfg(env)
    run_regression.call_hermes("p", cfg, timeout=5)
    assert seen["cmd"] == ryc.build_agent_command(cfg)
    assert seen["kw"]["input"] == "p"


# =========================================================================== #
# Phase 2 — execution path                                                     #
# =========================================================================== #

from helpers import order  # noqa: E402

LIVE = dict(DRY_RUN=False, SIMULATED_IDLE_BALANCE=None)


def placed_orders(rec):
    return [e for e in rec["executions"] if e.get("would_call")]


# --- T2.4: unreadable data fails closed ------------------------------------ #

def test_positions_error_with_existing_position_means_zero_stake(env):
    """Positions unreadable while a position exists: the cap cannot subtract
    it, so staking would go over MAX_PER_PRODUCT_USD. Fail closed."""
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(positions=[position("1", amount="5")], fail={"get_earn_positions"})
    agent = FakeAgent(reply_with(STAKE_1))
    rec, rc = run(env, tool=tool, agent=agent, MAX_PER_PRODUCT_USD=10)
    assert stake_executions(rec) == [] or not placed_orders(rec)
    assert all(e["action"] != "STAKE" or not e.get("would_call") for e in rec["executions"])
    assert any(a.startswith("DATA_UNAVAILABLE: positions") for a in rec["alerts"])
    assert blocking_alerts(rec) == []  # a network blip must not freeze the heartbeat


def test_positions_error_live_places_nothing(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(positions=[position("1", amount="5")], fail={"get_earn_positions"})
    rec, rc = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), **LIVE)
    assert tool.placed == []


@pytest.mark.parametrize("failing", ["get_wallet_balance", "get_earn_orders"])
def test_balance_or_orders_error_blocks_stake(env, failing):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(fail={failing})
    rec, rc = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), **LIVE)
    assert tool.placed == []
    assert any(a.startswith("DATA_UNAVAILABLE") for a in rec["alerts"])
    assert blocking_alerts(rec) == []


def test_products_error_is_not_config_incomplete(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    rec, rc = run(env, tool=FakeBybit(fail={"get_earn_products"}))
    assert any(a.startswith("DATA_UNAVAILABLE: products") for a in rec["alerts"])
    assert blocking_alerts(rec) == []


def test_balance_error_still_allows_redeem(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(products=[product("1", status="NotAvailable")],
                     positions=[position("1", amount="5")], fail={"get_wallet_balance"})
    rec, _ = run(env, tool=tool, **LIVE)
    assert [(c[1], c[2], c[3]) for c in tool.calls] == [("Redeem", "1", "5")]


# --- T2.1 / T2.5: place-order request -------------------------------------- #

def test_live_stake_uses_place_order(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit()
    rec, _ = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), **LIVE)
    [req] = tool.placed
    assert req["method"] == "POST" and req["path"] == "/v5/earn/place-order"
    body = req["body"]
    assert body["category"] == "FlexibleSaving" and body["orderType"] == "Stake"
    assert body["accountType"] == "UNIFIED" and body["coin"] == "USDT"
    assert body["productId"] == "1" and body["amount"] == "5"
    [ex] = placed_orders(rec)
    assert ex["executed"] is True and ex["response"]["orderId"] == "oid-1"
    assert ex["order_link_id"] == body["orderLinkId"]


def test_dry_run_would_call_equals_live_request(env):
    """T2.5: the dry-run record is exactly the request live mode sends."""
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    from executor import Executor
    orders_ = [{"action": "STAKE", "coin": "USDT", "product_id": "1", "amount": "5",
                "origin": "agent", "reason": "x"}]
    [dry] = Executor(dry_run=True, allow_new_positions=True, account_type="UNIFIED",
                     cycle_id="c1").execute(orders_)
    tool = FakeBybit()
    [live] = Executor(bybit_tool=tool, dry_run=False, allow_new_positions=True,
                      account_type="UNIFIED", cycle_id="c1").execute(orders_)
    assert dry["executed"] is False
    assert dry["would_call"] == live["would_call"] == tool.placed[0]


# --- T2.2: pending orders -------------------------------------------------- #

def test_pending_redeem_is_not_resent(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(products=[product("1", status="NotAvailable")],
                     positions=[position("1", amount="5")],
                     orders=[order("Redeem", "1", status="Pending")])
    rec, _ = run(env, tool=tool, **LIVE)
    assert tool.placed == []
    assert any("pending" in e["reason"].lower() for e in rec["executions"])
    assert rec["orders"][0]["status"] == "Pending"


def test_pending_order_blocks_new_stake_in_that_coin(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(orders=[order("Stake", "1", status="Pending")])
    run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), **LIVE)
    assert tool.placed == []


def test_second_cycle_does_not_resend_after_live_order(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(products=[product("1", status="NotAvailable")],
                     positions=[position("1", amount="5")])
    run(env, tool=tool, **LIVE)
    assert len(tool.placed) == 1
    rec2, _ = run(env, tool=tool, **LIVE)
    assert len(tool.placed) == 1  # the redeem is still Pending at Bybit
    assert rec2["orders"][-1]["status"] == "Pending"


def test_finished_orders_do_not_block(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(orders=[order("Stake", "1", status="Success"),
                             order("Redeem", "1", status="Fail", link="x")])
    run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), **LIVE)
    assert len(tool.placed) == 1


def test_unknown_order_status_counts_as_pending(env):
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    tool = FakeBybit(orders=[order("Stake", "1", status="Processing")])
    run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), **LIVE)
    assert tool.placed == []


@pytest.mark.parametrize("bad", [{"amount": None}, {"amount": "abc"}, {"amount": "-1"},
                                 {"productId": None}])
def test_position_with_unreadable_fields_fails_closed(env, bad):
    """A position whose amount (or product) cannot be read would count as 0
    against the cap — same over-limit risk as an API failure."""
    write_state(env["YIELD_STATE_FILE"], "NORMAL")
    pos = {**position("1", amount="5"), **bad}
    pos = {k: v for k, v in pos.items() if v is not None}
    tool = FakeBybit(positions=[pos])
    rec, _ = run(env, tool=tool, agent=FakeAgent(reply_with(STAKE_1)), **LIVE)
    assert tool.placed == []
    assert any(a.startswith("DATA_UNAVAILABLE: positions") for a in rec["alerts"])
