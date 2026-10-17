"""The heartbeat renews BEFORE the risk state expires (16/10).

On the VPS, 24 h showed RISK_STATE_STALE x22, each time two Telegram
messages (NO_NEW_POSITIONS, then NORMAL 5 minutes later): the heartbeat
renewed only after the 30-minute expiry, and the 5-minute cycle often landed
in the gap. Now:
  - the heartbeat renews once the age passes half the limit, under the same
    safety conditions (no blocking code, API reachable, valid signature,
    scanner alive) — a healthy cycle never sees a stale record;
  - a risk-state change is announced only after it held 2 cycles in a row;
  - if the heartbeat stops, nothing else renews: the state still expires
    within 30 minutes.
The simulation runs the real carry cycle (paper account) and the real
heartbeat against a fake clock; no network.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

import heartbeat
import risk_state
import run_carry_cycle as rcc
import settings
from carry_builders import cfg as base_cfg
from carry_regimes import E8, H, FakeClient, Regime
from helpers import KEY
from notify import Notifier

MIN = 60_000
T0 = 1_792_000_800_000 - (1_792_000_800_000 % E8)          # a settlement instant, Oct 2026


class Clock:
    def __init__(self, t_ms):
        self.ms = t_ms

    def time(self):
        return self.ms / 1000


@pytest.fixture
def sim(isolated_paths, tmp_path, monkeypatch):
    log_dir = tmp_path / "carry_logs"
    log_dir.mkdir()
    cfg = base_cfg(LOG_DIR=str(log_dir), DEADMAN_URL=None)
    path = tmp_path / "carry.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(path))
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "1")
    clock = Clock(T0)
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(rcc, "_now_iso", lambda: datetime.fromtimestamp(
        clock.ms / 1000, timezone.utc).isoformat())
    sent = []
    monkeypatch.setattr(heartbeat, "send_telegram_alert",
                        lambda tok, chat, text: sent.append(("heartbeat", text)) or True)
    notifier = Notifier({"TELEGRAM_BOT_TOKEN": "t", "ALERT_TELEGRAM_CHAT_ID": "1"}, cfg,
                        sender=lambda tok, chat, text: sent.append(("cycle", text)) or True,
                        state_file=tmp_path / "notify_cycle.json", clock=clock.time)
    regime = Regime([(30, 0.00005)], start_ms=T0 - 10 * 24 * H)
    state_file = settings.carry_risk_state_file()
    # Steady state after a deploy: NORMAL, renewed by the heartbeat.
    risk_state.write(state_file, KEY, "NORMAL", "deploy", risk_state.SOURCE_RENEW, ts_ms=T0,
                     profile=risk_state.CARRY_PROFILE)

    class Sim:
        def __init__(self):
            self.clock, self.sent, self.state_file = clock, sent, state_file
            self.records = []

        def cycle(self):
            rec, rc = rcc.run_cycle(cfg, {"HERMES_RISK_HMAC_KEY": KEY}, FakeClient(),
                                    now_ms=clock.ms, snapshot_fn=regime.snapshot_fn(),
                                    notifier=notifier, sleep=lambda s: None,
                                    paper_start=(0.0, 100.0))
            assert rc == 0, rec["alerts"]
            self.records.append((clock.ms, rec))
            return rec

        def heartbeat(self):
            assert heartbeat.main(["--system", "carry"]) == 0

        def run(self, minutes, hb_every=3, cycle_every=5, hb_offset=0, cycle_offset=0,
                heartbeat_on=True):
            start = clock.ms
            for m in range(minutes):
                clock.ms = start + m * MIN
                if heartbeat_on and (m - hb_offset) % hb_every == 0:
                    self.heartbeat()
                if (m - cycle_offset) % cycle_every == 0:
                    self.cycle()
            clock.ms = start + minutes * MIN

        def stale(self, since=None):
            return [(t, a) for t, r in self.records for a in r["alerts"]
                    if a.startswith("RISK_STATE_STALE") and (since is None or t >= since)]

    return Sim()


def test_24h_heartbeat_every_3_cycle_every_5_no_stale_no_message(sim):
    sim.run(24 * 60, hb_every=3, cycle_every=5)
    assert len(sim.records) == 288
    assert sim.stale() == []
    assert all(r["risk_state"] == "NORMAL" for _, r in sim.records)
    assert sim.sent == []
    ages = [r["risk_state_meta"]["age_ms"] for _, r in sim.records]
    assert max(ages) < risk_state.MAX_AGE_MS


def test_24h_with_the_shipped_timers_no_stale_no_message(sim):
    """yield-carry-cycle *:0/5, yield-carry-heartbeat *:3/5."""
    sim.run(24 * 60, hb_every=5, hb_offset=3, cycle_every=5)
    assert sim.stale() == [] and sim.sent == []


def test_the_simulation_reproduces_the_vps_bug(sim, monkeypatch):
    """Renewing only after expiry (the old rule) shows stale cycles in the
    same 24 hours — the simulation does catch the bug."""
    monkeypatch.setattr(heartbeat, "RENEW_AFTER_MS", risk_state.MAX_AGE_MS + 1)
    sim.run(24 * 60, hb_every=3, cycle_every=5)
    assert len(sim.stale()) > 0


def test_state_expires_within_30_minutes_when_the_heartbeat_stops(sim):
    sim.run(2 * 60)                                    # healthy
    assert sim.stale() == []
    stopped = sim.clock.ms
    last_write = risk_state.verify(sim.state_file, KEY,
                                   profile=risk_state.CARRY_PROFILE).ts
    assert stopped - last_write <= risk_state.MAX_AGE_MS
    sim.run(60, heartbeat_on=False)                    # the heartbeat is gone
    deadline = stopped + risk_state.MAX_AGE_MS
    after = [(t, r) for t, r in sim.records if t > deadline]
    assert after and all(any(a.startswith("RISK_STATE_STALE") for a in r["alerts"])
                         for _, r in after)
    assert all(r["risk_state"] == "NO_NEW_POSITIONS" for _, r in after)
    # stale exactly from last renewal + 30 min, never before
    first_stale = min(t for t, _ in sim.stale())
    assert last_write + risk_state.MAX_AGE_MS < first_stale <= deadline + 5 * MIN
    # and it is announced: the change held 2 cycles
    assert any("RISK STATE NO_NEW_POSITIONS" in text for _, text in sim.sent)


# --- the heartbeat's decision ------------------------------------------------------- #

def _decide_at(sim, age_min, cycle_age_min=None, alerts=()):
    risk_state.write(sim.state_file, KEY, "NORMAL", "t", risk_state.SOURCE_RENEW,
                     ts_ms=sim.clock.ms - age_min * MIN, profile=risk_state.CARRY_PROFILE)
    if cycle_age_min is not None:
        log_dir = Path(yaml.safe_load(settings.carry_config_file().read_text())["LOG_DIR"])
        for f in log_dir.glob("*.jsonl"):
            f.unlink()
        ts = datetime.fromtimestamp((sim.clock.ms - cycle_age_min * MIN) / 1000, timezone.utc)
        rcc.write_record(log_dir, {"ts": ts.isoformat(), "alerts": list(alerts)}, sim.clock.ms)
    before = sim.state_file.read_bytes()
    rc, condition, _ = heartbeat._decide(settings.load_env(), heartbeat.SYSTEMS["carry"])
    return sim.state_file.read_bytes() != before, condition


def test_young_state_is_left_alone(sim):
    assert _decide_at(sim, age_min=10, cycle_age_min=2) == (False, None)


def test_half_aged_state_is_renewed_when_the_scanner_is_alive(sim):
    wrote, condition = _decide_at(sim, age_min=16, cycle_age_min=2)
    assert wrote and condition is None
    v = risk_state.verify(sim.state_file, KEY, profile=risk_state.CARRY_PROFILE)
    assert v.state == "NORMAL" and v.age_ms == 0 and v.source == risk_state.SOURCE_RENEW


def test_half_aged_state_is_not_renewed_without_a_live_scanner(sim):
    assert _decide_at(sim, age_min=16, cycle_age_min=40) == (False, None)   # silent: expires


def test_half_aged_state_is_not_renewed_after_a_blocking_cycle(sim):
    wrote, condition = _decide_at(sim, age_min=16, cycle_age_min=2,
                                  alerts=("ORPHAN_LEG: perp closed",))
    assert not wrote and condition.startswith("cycle_blocked")


def test_stale_state_without_scanner_still_alerts(sim):
    wrote, condition = _decide_at(sim, age_min=31, cycle_age_min=40)
    assert not wrote and condition == "scanner_dead"


def test_operator_non_normal_is_never_renewed(sim):
    risk_state.write(sim.state_file, KEY, "UNWIND", "op", risk_state.SOURCE_OPERATOR,
                     ts_ms=sim.clock.ms - 20 * MIN, profile=risk_state.CARRY_PROFILE)
    before = sim.state_file.read_bytes()
    heartbeat._decide(settings.load_env(), heartbeat.SYSTEMS["carry"])
    assert sim.state_file.read_bytes() == before


# --- notifications: a change must hold 2 cycles -------------------------------------- #

def _notifier(tmp_path, sent):
    return Notifier({"TELEGRAM_BOT_TOKEN": "t", "ALERT_TELEGRAM_CHAT_ID": "1"}, {},
                    sender=lambda tok, chat, text: sent.append(text) or True,
                    state_file=tmp_path / "n.json", clock=lambda: 1000.0)


def test_one_cycle_blip_sends_nothing(tmp_path):
    sent = []
    n = _notifier(tmp_path, sent)
    for cond in (None, "NO_NEW_POSITIONS", None, None, "NO_NEW_POSITIONS", None):
        n.observe("risk", cond, "down", resolved_text="up", confirm=2)
    assert sent == []


def test_change_held_two_cycles_is_sent_once_and_recovery_too(tmp_path):
    sent = []
    n = _notifier(tmp_path, sent)
    for cond in ("NO_NEW_POSITIONS", "NO_NEW_POSITIONS", "NO_NEW_POSITIONS",
                 None, "NO_NEW_POSITIONS", None, None):
        n.observe("risk", cond, "down", resolved_text="up", confirm=2)
    assert sent == ["down", "up"]


def test_without_confirm_nothing_changes(tmp_path):
    sent = []
    n = _notifier(tmp_path, sent)
    n.observe("cycle", "ORPHAN_LEG", "orphan")
    assert sent == ["orphan"]                          # blocking codes: at once


def test_both_cycles_debounce_the_risk_state_channel():
    for f in ("run_carry_cycle.py", "run_yield_cycle.py"):
        text = (Path(__file__).resolve().parent.parent / f).read_text()
        assert "confirm=STATE_CONFIRM_CYCLES" in text and "STATE_CONFIRM_CYCLES = 2" in text, f
