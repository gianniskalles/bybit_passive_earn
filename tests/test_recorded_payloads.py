"""Lock the Bybit parsing on recorded responses (tests/data/*.json).

Every replayable capture in tests/data is run through the REAL
BybitEarnTool and collect_inputs.  Today only the SYNTHETIC file exists;
real testnet captures (scripts/testnet.py capture) are picked up
automatically and must pass the same checks — that is what closes the
fields that are still unconfirmed (precision, maxStakeAmount, the position
list key, order status values, APR-history fields)."""

import json
from pathlib import Path

import pytest

import run_yield_cycle as ryc
from helpers import load_cfg
from replay import DATA_DIR, load_payload, replay_tool

CAPTURES = sorted(p.name for p in DATA_DIR.glob("*.json")
                  if "responses" in json.loads(p.read_text()))
ROUNDTRIPS = sorted(p.name for p in DATA_DIR.glob("testnet_roundtrip_*.json"))


def test_there_is_at_least_one_capture():
    assert CAPTURES


@pytest.mark.parametrize("name", CAPTURES)
def test_capture_parses_into_a_complete_usdt_product(isolated_paths, name):
    cfg = load_cfg(isolated_paths, SIMULATED_IDLE_BALANCE=None)
    data = ryc.collect_inputs(replay_tool(load_payload(name)), cfg)
    assert data["data_errors"] == {}
    assert data["pending_unmatched"] == []
    usdt = [p for p in data["scan"] if p["coin"] == "USDT"]
    assert usdt, f"no USDT product survived filtering: {data['filtered']}"
    for p in usdt:
        for field in ("precision", "min_stake_amount", "max_stake_amount", "estimate_apr",
                      "apr_ma_24h", "redemption_eta_hours"):
            assert p[field] is not None, f"{field} unknown in {name}"
    assert all(isinstance(pos["amount"], float) for pos in data["positions"])


@pytest.mark.parametrize("name", ROUNDTRIPS)
def test_testnet_roundtrip_succeeded(name):
    report = json.loads((DATA_DIR / name).read_text())
    steps = {s["execution"]["action"]: s for s in report["steps"]}
    assert set(steps) == {"STAKE", "REDEEM"}
    for step in steps.values():
        assert step["execution"]["executed"] is True
        assert step["execution"]["response"]["orderId"]
        assert str(step["final_order"]["status"]).lower() == "success"
        assert step["final_order"]["orderLinkId"] == step["execution"]["order_link_id"]
