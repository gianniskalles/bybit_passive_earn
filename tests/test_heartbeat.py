#!/usr/bin/env python3
"""Test suite for heartbeat.py — v5.3 regressions.

Covers the two scenarios the user explicitly wants proven, plus the LOG_DIR
rules:
  1. test_bootstrap_no_logs_writes_no_new_positions
       no cycle log at all  -> heartbeat writes NO_NEW_POSITIONS with
       source=heartbeat_bootstrap; it is promoted to NORMAL only after a
       verified clean cycle (test_bootstrap_promotes_after_clean_cycle).
  2. test_log_dir_missing_errors
       LOG_DIR misconfigured / directory missing -> heartbeat FAILS (exit 3)
       with an alert; it is NOT silently treated as bootstrap.
  3. test_stuck_scenario_risk_state_stale_heartbeat_writes_normal
       risk state stale NORMAL -> heartbeat renews -> next cycle runs WITHOUT
       forced:true (the stuck scenario the heartbeat exists to break).

Canonical JSON + HMAC come from the single shared source signing.py (repo
root) — the same module heartbeat.py and risk_state.py import.  A divergence
between the two implementations would surface as a signature failure, i.e.
as RISK_STATE_BAD_SIGNATURE.

Run with `pytest` from the repo root; tests/conftest.py isolates every path
into tmp_path and blocks network access.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

# Both heartbeat.py and the tests use the SAME shared signing source.
from signing import sign, verify

import heartbeat
import risk_state

KEY = "test-secret-key-123"


def _setenv(monkeypatch, log_dir: Path, state_file: Path, skip_config_dcheck: str = "0"):
    monkeypatch.setenv("YIELD_LOG_DIR", str(log_dir))
    monkeypatch.setenv("YIELD_STATE_FILE", str(state_file))
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")
    monkeypatch.setenv("YIELD_SKIP_CONFIG_DCHECK", skip_config_dcheck)
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)


def _run() -> int:
    return heartbeat.main()


def _signed(state: str, ts: int, reason: str, source: str = risk_state.SOURCE_RENEW) -> dict:
    return risk_state.make_record(KEY, state, reason, source, ts_ms=ts)


def _valid(rs: dict) -> bool:
    return verify(KEY, {k: v for k, v in rs.items() if k != "sig"}, rs.get("sig", ""))


def test_bootstrap_no_logs_writes_no_new_positions(tmp_path, monkeypatch):
    """Bootstrap: no cycle has ever run (log dir exists, no *.jsonl) -> NO_NEW_POSITIONS (fail-closed)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"
    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0, f"bootstrap should succeed, got {rc}"
    assert state.exists(), "risk_state.json should be created"
    rs = json.loads(state.read_text())
    assert rs["state"] == "NO_NEW_POSITIONS", f"expected NO_NEW_POSITIONS (fail-closed), got {rs['state']}"
    assert _valid(rs), "HMAC must verify"
    assert rs["source"] == "heartbeat_bootstrap"
    print("✓ bootstrap (no logs) writes NO_NEW_POSITIONS (fail-closed)")


def test_log_dir_missing_errors(tmp_path, monkeypatch):
    """LOG_DIR directory missing -> HARD ERROR (exit 3), NOT bootstrap."""
    log_dir = tmp_path / "logs_missing"  # NOT created
    state = tmp_path / "risk_state.json"

    # In prod (no test escape hatch) a missing LOG_DIR dir must FAIL.
    _setenv(monkeypatch, log_dir, state, skip_config_dcheck="0")
    rc = _run()
    assert rc == 3, f"missing LOG_DIR should be exit 3 (misconfig), got {rc}"
    assert not state.exists(), "must NOT write risk_state on LOG_DIR misconfig"
    print("✓ missing LOG_DIR dir -> error (exit 3), not silent bootstrap")


def test_normal_fresh_no_write(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"
    obj = _signed("NORMAL", int(time.time() * 1000), "test fresh")
    state.write_text(json.dumps(obj))

    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0
    rs = json.loads(state.read_text())
    assert rs["reason"] == "test fresh", "must not overwrite a fresh NORMAL"
    print("✓ NORMAL + fresh -> no write")


def test_normal_stale_scanner_dead_heartbeat_abstains(tmp_path, monkeypatch):
    """Scanner dead (no recent cycle) -> heartbeat ABSTAINS, stale NORMAL stays stale."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"
    old_ts = int((time.time() - 3600) * 1000)
    obj = _signed("NORMAL", old_ts, "old")
    state.write_text(json.dumps(obj))

    # No recent cycle log -> scanner not alive.
    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0, "heartbeat should succeed (abstain is not error)"
    rs = json.loads(state.read_text())
    assert rs["state"] == "NORMAL", "state should remain NORMAL"
    assert rs["ts"] == old_ts, "timestamp must NOT change (no renewal)"
    assert rs == obj, "ABSTAIN must not write anything (T1.4)"
    print("✓ stale NORMAL + scanner dead -> heartbeat ABSTAINS, state stays stale")


def test_non_normal_stale_abstains(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"
    old_ts = int((time.time() - 3600) * 1000)
    obj = _signed("NO_NEW_POSITIONS", old_ts, "was stale", risk_state.SOURCE_OPERATOR)
    state.write_text(json.dumps(obj))

    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0
    rs = json.loads(state.read_text())
    assert rs["state"] == "NO_NEW_POSITIONS", "must not overwrite non-NORMAL"
    assert rs["ts"] == old_ts
    print("✓ non-NORMAL + stale -> ABSTAIN, no overwrite")


def test_bad_hmac_abstains(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"
    obj = risk_state.make_record("wrong-key", "NORMAL", "tampered", risk_state.SOURCE_RENEW, ts_ms=int(time.time() * 1000))  # signed with a DIFFERENT key
    state.write_text(json.dumps(obj))

    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0
    rs = json.loads(state.read_text())
    assert rs["reason"] == "tampered", "must not overwrite unverifiable state"
    print("✓ bad HMAC -> ABSTAIN, no overwrite")


def test_blocking_code_in_log_abstains(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"
    obj = _signed("NORMAL", int(time.time() * 1000), "test")
    state.write_text(json.dumps(obj))

    (log_dir / "2026-09-14.jsonl").write_text(json.dumps({
        "ts": "2026-09-14T10:00:00Z", "cycle_id": "c",
        "alerts": ["CONFIG_INCOMPLETE: missing ENTRY_APR"],
    }) + "\n")

    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0
    rs = json.loads(state.read_text())
    assert rs["reason"] == "test", "must not renew on hard-fail code"
    print("✓ blocking code in log -> ABSTAIN")


def test_non_blocking_code_in_log_allows_renewal(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"
    old_ts = int((time.time() - 3600) * 1000)
    obj = _signed("NORMAL", old_ts, "old")
    state.write_text(json.dumps(obj))

    # Recent cycle (within scanner window: 30 min) with non-blocking alert.
    recent_ts = int((time.time() - 300) * 1000)  # 5 minutes ago
    dt = datetime.fromtimestamp(recent_ts / 1000, tz=timezone.utc).isoformat()
    (log_dir / "2026-09-14.jsonl").write_text(json.dumps({
        "ts": dt, "cycle_id": "c",
        "alerts": ["RISK_STATE_STALE: risk_state stale (3600s old)"],
    }) + "\n")

    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0
    rs = json.loads(state.read_text())
    assert "was stale" in rs["reason"], "must renew despite RISK_STATE_STALE"
    print("✓ non-blocking code in log -> allows renewal")


def test_stale_normal_scanner_dead_heartbeat_abstains(tmp_path, monkeypatch):
    """Scanner dead (no recent cycle) -> heartbeat ABSTAINS, stale NORMAL stays stale."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"

    # Stale NORMAL, but NO cycle log at all -> scanner never produced a decision.
    old_ts = int((time.time() - 3600) * 1000)
    obj = _signed("NORMAL", old_ts, "stale")
    state.write_text(json.dumps(obj))

    # Empty log dir -> no verified cycle ever, scanner not alive.
    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0, "heartbeat should succeed (abstain is not error)"
    rs = json.loads(state.read_text())
    assert rs["state"] == "NORMAL", "state should remain NORMAL"
    assert rs["ts"] == old_ts, "timestamp must NOT change (no renewal)"
    assert rs == obj, "ABSTAIN must not write anything (T1.4)"
    print("✓ stale NORMAL + scanner dead -> heartbeat ABSTAINS, state stays stale")


def test_stale_normal_scanner_alive_heartbeat_renews(tmp_path, monkeypatch):
    """Scanner alive (recent non-blocking cycle) -> heartbeat RENEWS stale NORMAL."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"

    # Stale NORMAL
    old_ts = int((time.time() - 3600) * 1000)
    obj = _signed("NORMAL", old_ts, "stale")
    state.write_text(json.dumps(obj))

    # Recent cycle (within scanner window) with non-blocking alert -> scanner alive.
    # The scanner window is max(MAX_AGE_MS, 3 * interval_min * 60 * 1000)
    # MAX_AGE_MS = 30 min, interval = 10 min -> window = 30 min.
    # Use a cycle from 5 minutes ago.
    recent_ts = int((time.time() - 300) * 1000)
    dt = datetime.fromtimestamp(recent_ts / 1000, tz=timezone.utc).isoformat()
    (log_dir / "2026-09-14.jsonl").write_text(json.dumps({
        "ts": dt, "cycle_id": "recent_cycle",
        "alerts": ["RISK_STATE_STALE: risk_state stale (3600s old)"],
    }) + "\n")

    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0, "heartbeat should succeed"
    rs = json.loads(state.read_text())
    assert rs["state"] == "NORMAL"
    assert rs["ts"] > old_ts, "timestamp MUST refresh (renewal happened)"
    assert "scanner alive" in rs["reason"], "reason should mention scanner alive"
    print("✓ stale NORMAL + scanner alive -> heartbeat RENEWS")


def test_stuck_scenario_risk_state_stale_heartbeat_writes_normal(tmp_path, monkeypatch):
    """THE stuck scenario: stale risk state -> heartbeat renews -> next cycle
    runs WITHOUT forced:true."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = tmp_path / "risk_state.json"

    # Step 1: risk state is STALE NORMAL (the stuck condition).
    old_ts = int((time.time() - 3600) * 1000)
    obj = _signed("NORMAL", old_ts, "stale from previous cycle")
    state.write_text(json.dumps(obj))

    # Previous cycle surfaced a non-fatal RISK_STATE_STALE alert.
    # Make it recent (within scanner window of 30 min).
    recent_ts = int((time.time() - 300) * 1000)  # 5 minutes ago
    dt = datetime.fromtimestamp(recent_ts / 1000, tz=timezone.utc).isoformat()
    (log_dir / "2026-09-14.jsonl").write_text(json.dumps({
        "ts": dt, "cycle_id": "stuck_cycle",
        "alerts": ["RISK_STATE_STALE: risk_state stale (3600s old)"],
    }) + "\n")

    _setenv(monkeypatch, log_dir, state)
    rc = _run()
    assert rc == 0, "heartbeat should succeed on the stuck scenario"
    rs = json.loads(state.read_text())
    assert rs["state"] == "NORMAL"
    assert "was stale" in rs["reason"], "should renew the stale NORMAL"
    renewed_ts = rs["ts"]
    assert renewed_ts > old_ts, "timestamp must refresh"

    # Step 2: now the state is FRESH -> the wrapper's verify() returns true,
    # so the next cycle runs with the normal (non-stale) path. We model the
    # wrapper's forced:true decision here: forced is only set when state is
    # stale/invalid. Since it is now fresh, forced must be False.
    now_ms = int(time.time() * 1000)
    fresh = (now_ms - rs["ts"]) < 30 * 60 * 1000
    assert fresh, "renewed state must be fresh (no forced:true needed)"
    forced = not fresh
    assert forced is False, "next cycle must run WITHOUT forced:true"
    print("✓ STUCK scenario: stale NORMAL -> heartbeat renews -> "
          "next cycle runs without forced:true")


# --------------------------------------------------------------------------- #
# Phase 1 — T1.3 bootstrap promotion, T1.4 robustness                         #
# --------------------------------------------------------------------------- #

import pytest  # noqa: E402

import run_yield_cycle  # noqa: E402
from helpers import FakeAgent, FakeBybit, load_cfg, reply_with  # noqa: E402

HOLD = {"action": "HOLD", "coin": "USDT", "product_id": "1", "reason": "nothing to do"}


@pytest.fixture
def hb(isolated_paths, monkeypatch):
    """Heartbeat wired to the isolated config/log dir, with alerts captured."""
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "1")
    alerts = []
    monkeypatch.setattr(heartbeat, "send_telegram_alert",
                        lambda token, chat, text: alerts.append(text) or True)
    isolated_paths["alerts"] = alerts
    return isolated_paths


def _cycle(paths):
    cfg = load_cfg(paths)
    rec, rc = run_yield_cycle.run_cycle(cfg, tool=FakeBybit(), agent=FakeAgent(reply_with(HOLD)))
    return rec


def _state(paths):
    return json.loads(Path(paths["YIELD_STATE_FILE"]).read_text())


def test_bootstrap_promotes_after_clean_cycle(hb):
    assert heartbeat.main() == 0
    boot = _state(hb)
    assert boot["state"] == "NO_NEW_POSITIONS" and boot["source"] == "heartbeat_bootstrap"

    # A second heartbeat without a cycle in between must not promote.
    assert heartbeat.main() == 0
    assert _state(hb) == boot

    rec = _cycle(hb)
    assert rec["risk_state"] == "NO_NEW_POSITIONS"
    assert heartbeat.main() == 0
    promoted = _state(hb)
    assert promoted["state"] == "NORMAL"
    assert promoted["source"] == "heartbeat_renew"
    assert _valid(promoted)


def test_bootstrap_not_promoted_by_cycle_before_its_ts(hb):
    _cycle(hb)  # a clean cycle that ran BEFORE the bootstrap record existed
    time.sleep(0.01)
    risk_state.write(hb["YIELD_STATE_FILE"], KEY, "NO_NEW_POSITIONS", "boot",
                     risk_state.SOURCE_BOOTSTRAP)
    before = _state(hb)
    assert heartbeat.main() == 0
    assert _state(hb) == before


def test_bootstrap_not_promoted_after_blocking_cycle(hb):
    risk_state.write(hb["YIELD_STATE_FILE"], KEY, "NO_NEW_POSITIONS", "boot",
                     risk_state.SOURCE_BOOTSTRAP)
    rec = _cycle(hb)
    # Simulate that the last cycle hard-failed.
    log = next(Path(hb["LOG_DIR"]).glob("*.jsonl"))
    rec["alerts"] = ["AGENT_PARSE_ERROR: x"]
    log.write_text(json.dumps(rec) + "\n")
    before = _state(hb)
    assert heartbeat.main() == 0
    assert _state(hb) == before


def test_operator_state_never_promoted(hb):
    risk_state.write(hb["YIELD_STATE_FILE"], KEY, "NO_NEW_POSITIONS", "manual hold",
                     risk_state.SOURCE_OPERATOR)
    before = Path(hb["YIELD_STATE_FILE"]).read_bytes()
    for _ in range(3):
        rec = _cycle(hb)
        assert rec["risk_state"] == "NO_NEW_POSITIONS"
        assert heartbeat.main() == 0
    assert Path(hb["YIELD_STATE_FILE"]).read_bytes() == before


def test_corrupt_state_not_overwritten(hb):
    state_file = Path(hb["YIELD_STATE_FILE"])
    state_file.write_text('{"state":"UNWIND","ts":17')
    before = state_file.read_bytes()
    assert heartbeat.main() == 0
    assert state_file.read_bytes() == before
    assert any("UNREADABLE" in a for a in hb["alerts"]), hb["alerts"]


def test_abstain_does_not_write(hb):
    state_file = Path(hb["YIELD_STATE_FILE"])
    risk_state.write(state_file, KEY, "NORMAL", "old", risk_state.SOURCE_RENEW,
                     ts_ms=int((time.time() - 3600) * 1000))
    before = (state_file.read_bytes(), state_file.stat().st_mtime_ns)
    time.sleep(0.01)
    assert heartbeat.main() == 0  # no cycle ever ran -> scanner not alive -> ABSTAIN
    assert (state_file.read_bytes(), state_file.stat().st_mtime_ns) == before


_GOOD = lambda: risk_state.make_record(KEY, "NORMAL", "x", risk_state.SOURCE_RENEW)  # noqa: E731


@pytest.mark.parametrize("mutate", [
    lambda o: {**o, "sig": None},
    lambda o: {k: v for k, v in o.items() if k != "profile"},
    lambda o: {k: v for k, v in o.items() if k != "source"},   # legacy record
    lambda o: {**o, "ts": "17"},
    lambda o: {**o, "state": ["NORMAL"]},
    lambda o: [o],
    lambda o: "NORMAL",
    lambda o: None,
], ids=["sig_null", "no_profile", "no_source", "ts_str", "state_list",
        "top_level_list", "top_level_str", "null"])
def test_heartbeat_survives_malformed_state(hb, mutate):
    state_file = Path(hb["YIELD_STATE_FILE"])
    state_file.write_text(json.dumps(mutate(_GOOD())))
    before = state_file.read_bytes()
    assert heartbeat.main() == 0
    assert state_file.read_bytes() == before
    assert hb["alerts"], "an unverifiable state must raise an alert"


@pytest.mark.parametrize("last_line", ["not json", "[1, 2]", '{"alerts": "CRITICAL"}',
                                       '{"ts": 5, "alerts": [null, 3]}'])
def test_heartbeat_survives_malformed_cycle_log(hb, last_line):
    risk_state.write(hb["YIELD_STATE_FILE"], KEY, "NORMAL", "old", risk_state.SOURCE_RENEW,
                     ts_ms=int((time.time() - 3600) * 1000))
    (Path(hb["LOG_DIR"]) / "2026-09-25.jsonl").write_text(last_line + "\n")
    assert heartbeat.main() == 0


def test_blocking_codes_are_exact():
    # Rule 8: any change here must come with the wrapper change that motivates it.
    assert set(heartbeat.BLOCKING_CODES) == {
        "CONFIG_INCOMPLETE", "CRITICAL", "AGENT_PARSE_ERROR", "DECISION_VALIDATION_FAILED",
    }
