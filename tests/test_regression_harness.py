"""Phase 4 — the LLM regression uses the production path (T4.1/T4.2) and
its scenarios are sound. No LLM: a scripted agent stands in."""

import json
import os
from pathlib import Path

import pytest

import run_regression as rr
import run_yield_cycle as ryc
from helpers import decision_json
from regression_fixtures import scenarios
from replay import load_payload, replay_tool


@pytest.fixture
def reg(isolated_paths, monkeypatch, tmp_path):
    for k in ("YIELD_STATE_FILE", "YIELD_SESSION_DIR", "HERMES_RISK_HMAC_KEY"):
        monkeypatch.setenv(k, os.environ.get(k, ""))  # restored by monkeypatch
    rr.isolate(tmp_path)
    return rr.regression_config(tmp_path)


def scripted(expect_stake):
    def agent(prompt, cfg, cycle_id):
        ds = [{"action": "STAKE", "coin": "USDT", "product_id": pid, "reason": "x 0.012"}
              for pid in expect_stake] or [{"action": "HOLD", "coin": "USDT",
                                            "product_id": "1", "reason": "x 0.0005"}]
        return decision_json(cycle_id, ds), "s"
    return agent


def test_regression_config_is_production_config(reg):
    prod = ryc.load_config(Path(ryc.settings.config_file()))
    for k in ("ENTRY_APR", "EXIT_APR", "MIN_APR_EDGE", "RESOLVED_MODEL", "REQUESTED_REASONING",
              "PROMPT_VERSION", "MAX_PER_PRODUCT_USD", "MAX_REDEMPTION_ETA_HOURS", "COIN_WHITELIST"):
        assert reg[k] == prod[k]
    assert reg["DRY_RUN"] is True


def test_regression_uses_production_agent_and_cycle():
    import inspect
    src = inspect.getsource(rr)
    assert "ryc.call_agent" in src and "ryc.run_cycle" in src
    for name in ("def extract_json", "def compose_prompt", "def call_hermes", "subprocess"):
        assert name not in src


@pytest.mark.parametrize("sc", scenarios(), ids=lambda s: s["name"])
def test_scenario_passes_with_expected_answer(reg, sc):
    result = rr.run_once(sc, reg, scripted(sc["expect"]["stake"]))
    assert result["rc"] == 0, result["record"]["alerts"]
    assert rr.evaluate(sc, result) == []
    assert result["record"]["risk_state"] == sc["risk_state"]
    # The recorded payload went through the real client and filters:
    assert [p["product_id"] for p in json.loads(result["prompt"].rsplit("```json\n", 1)[1]
                                               .split("\n```")[0])["scan"]] == ["1"]


@pytest.mark.parametrize("sc", scenarios(), ids=lambda s: s["name"])
def test_scenario_fails_with_wrong_answer(reg, sc):
    wrong = [] if sc["expect"]["stake"] else ["1"]
    assert rr.evaluate(sc, rr.run_once(sc, reg, scripted(wrong)))


def test_prompt_is_the_production_prompt(reg, monkeypatch):
    built = []
    real = ryc.compose_prompt
    monkeypatch.setattr(ryc, "compose_prompt", lambda *a, **k: built.append(real(*a, **k)) or built[-1])
    sc = scenarios()[0]
    result = rr.run_once(sc, reg, scripted(["1"]))
    assert result["prompt"] == built[-1]
    body = (ryc.settings.ROOT / f"prompt_{reg['PROMPT_VERSION']}.md").read_text()
    assert body in result["prompt"]


def test_unparseable_output_fails(reg):
    sc = scenarios()[0]
    result = rr.run_once(sc, reg, lambda p, c, cid: ("I think you should stake.", "s"))
    assert rr.evaluate(sc, result)


def test_replay_runs_real_client_parsing():
    tool = replay_tool(load_payload("SYNTHETIC_usdt_flexible.json"))
    assert [p["productId"] for p in tool.get_earn_products()] == ["1", "2"]
    assert tool.get_earn_positions(coin="USDT") == []
    assert tool.get_earn_orders() == []
    hist = tool.get_earn_apr_history(product_id="1")
    assert len(hist) == 24
