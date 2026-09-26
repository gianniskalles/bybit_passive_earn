"""Phase 5 — operations: Telegram dedup (T5.2), operator commands (T5.3),
session cleanup (T5.4), deploy units (T5.1)."""

import configparser
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import heartbeat
import risk_state
import run_yield_cycle as ryc
import summary
from helpers import KEY, FakeAgent, FakeBybit, load_cfg, reply_with, write_state
from notify import REPEAT_S, Notifier
from telegram_bot import CONFIRM_TTL_S, CommandBot

REPO = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Outbox:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, token, chat, text):
        self.sent.append(text)
        return self.ok


def notifier(tmp_path, clock=None, outbox=None, token="tok"):
    return Notifier({"TELEGRAM_BOT_TOKEN": token} if token else {}, {"ALERT_TELEGRAM_CHAT_ID": "42"},
                    sender=outbox, state_file=tmp_path / "n.json", clock=clock or Clock())


# --- T5.2: dedup ----------------------------------------------------------- #

def test_same_condition_is_sent_once_then_every_6h(tmp_path):
    clock, box = Clock(), Outbox()
    for _ in range(50):
        notifier(tmp_path, clock, box).observe("hb", "scanner_dead", "stuck")
        clock.t += 300  # a heartbeat every 5 minutes
    assert len(box.sent) == 1 + (50 * 300) // REPEAT_S


def test_change_and_recovery_are_sent(tmp_path):
    clock, box = Clock(), Outbox()
    n = lambda: notifier(tmp_path, clock, box)  # noqa: E731
    n().observe("hb", "a", "A")
    n().observe("hb", "b", "B")
    n().observe("hb", None, resolved_text="OK again")
    n().observe("hb", None)
    assert box.sent == ["A", "B", "OK again"]


def test_failed_send_is_retried(tmp_path):
    clock, box = Clock(), Outbox(ok=False)
    notifier(tmp_path, clock, box).observe("hb", "a", "A")
    box.ok = True
    notifier(tmp_path, clock, box).observe("hb", "a", "A")
    assert box.sent == ["A", "A"]


def test_no_token_sends_nothing(tmp_path):
    box = Outbox()
    assert notifier(tmp_path, outbox=box, token=None).observe("hb", "a", "A") is False
    assert box.sent == []


def test_heartbeat_abstain_is_not_repeated_every_run(isolated_paths, monkeypatch):
    """K19: a stuck state used to alert on every heartbeat run."""
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    box = Outbox()
    monkeypatch.setattr(heartbeat, "send_telegram_alert", box)
    Path(isolated_paths["YIELD_STATE_FILE"]).write_text('{"state":"UNWIND","ts":17')
    for _ in range(10):
        assert heartbeat.main() == 0
    assert len(box.sent) == 1 and "UNREADABLE" in box.sent[0]
    risk_state.write(isolated_paths["YIELD_STATE_FILE"], KEY, "NORMAL", "fixed",
                     risk_state.SOURCE_OPERATOR)
    heartbeat.main()
    assert len(box.sent) == 2 and "back to normal" in box.sent[1]


def test_notify_cycle_dedups_and_reports_orders(isolated_paths, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    write_state(isolated_paths["YIELD_STATE_FILE"], "NORMAL")
    box = Outbox()
    n = notifier(tmp_path, outbox=box)
    bad = FakeAgent(lambda cid: "no json here")
    for _ in range(3):
        rec, _ = ryc.run_cycle(load_cfg(isolated_paths), tool=FakeBybit(), agent=bad)
        ryc.notify_cycle(rec, n)
    assert len([m for m in box.sent if "AGENT_PARSE_ERROR" in m]) == 1

    stake = {"action": "STAKE", "coin": "USDT", "product_id": "1", "reason": "x"}
    tool = FakeBybit()
    rec, _ = ryc.run_cycle(load_cfg(isolated_paths, DRY_RUN=False, SIMULATED_IDLE_BALANCE=None),
                           tool=tool, agent=FakeAgent(reply_with(stake)))
    ryc.notify_cycle(rec, n)
    assert any(m.startswith("✅ ORDER STAKE 5 USDT product 1 orderId oid-1") for m in box.sent)
    assert any("no blocking codes" in m for m in box.sent)


def test_daily_summary(isolated_paths, monkeypatch):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    write_state(isolated_paths["YIELD_STATE_FILE"], "NORMAL")
    for reply in (reply_with({"action": "HOLD", "coin": "USDT", "product_id": "1", "reason": "x"}),
                  lambda cid: "garbage"):
        ryc.run_cycle(load_cfg(isolated_paths), tool=FakeBybit(), agent=FakeAgent(reply))
    text = summary.build_summary(Path(isolated_paths["LOG_DIR"]),
                                 Path(isolated_paths["YIELD_STATE_FILE"]), KEY)
    assert "cycles: 2" in text and "NORMAL 2" in text
    assert "AGENT_PARSE_ERROR×1" in text
    assert "risk_state now: NORMAL (OK, source=operator" in text


# --- T5.3: operator commands ----------------------------------------------- #

def msg(text, chat=42, sender=42):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"id": sender}, "text": text}}


@pytest.fixture
def bot():
    writes, clock = [], Clock()
    b = CommandBot("42", lambda s, r: writes.append((s, r)), lambda: "status-text",
                   clock=clock, new_code=lambda: "123456")
    b.writes, b.clock_ = writes, clock
    return b


def test_unwind_needs_confirmation(bot):
    chat, reply = bot.handle(msg("/unwind"))
    assert chat == "42" and "/confirm 123456" in reply and bot.writes == []
    chat, reply = bot.handle(msg("/confirm 123456"))
    assert bot.writes and bot.writes[0][0] == "UNWIND" and "✅" in reply


def test_resume_writes_normal(bot):
    bot.handle(msg("/resume"))
    bot.handle(msg("/confirm 123456"))
    assert [w[0] for w in bot.writes] == ["NORMAL"]


@pytest.mark.parametrize("chat,sender", [(7, 7), (42, 7), (7, 42)])
def test_other_chats_are_ignored(bot, chat, sender):
    assert bot.handle(msg("/unwind", chat, sender)) is None
    assert bot.handle(msg("/confirm 123456", chat, sender)) is None
    assert bot.writes == [] and bot.pending is None


def test_wrong_code_or_expired_does_not_write(bot):
    bot.handle(msg("/unwind"))
    assert "Wrong code" in bot.handle(msg("/confirm 000000"))[1]
    assert "Nothing to confirm" in bot.handle(msg("/confirm 123456"))[1]  # single use
    bot.handle(msg("/unwind"))
    bot.clock_.t += CONFIRM_TTL_S + 1
    assert "expired" in bot.handle(msg("/confirm 123456"))[1]
    assert bot.writes == []


def test_cancel(bot):
    bot.handle(msg("/unwind"))
    bot.handle(msg("/cancel"))
    assert "Nothing to confirm" in bot.handle(msg("/confirm 123456"))[1]


def test_operator_write_is_respected_by_heartbeat(isolated_paths, monkeypatch):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")
    path = Path(isolated_paths["YIELD_STATE_FILE"])
    b = CommandBot("42", lambda s, r: risk_state.write(path, KEY, s, r, risk_state.SOURCE_OPERATOR),
                   lambda: "", new_code=lambda: "1")
    b.handle(msg("/unwind"))
    b.handle(msg("/confirm 1"))
    before = path.read_bytes()
    heartbeat.main()
    assert path.read_bytes() == before
    assert json.loads(before)["source"] == "operator"


# --- T5.4 ------------------------------------------------------------------- #

def test_old_session_files_are_deleted(tmp_path):
    old, new, other = tmp_path / "a.raw", tmp_path / "b.raw", tmp_path / "c.txt"
    for f in (old, new, other):
        f.write_text("x")
    week = 7 * 86400
    os.utime(old, (time.time() - week - 60,) * 2)
    os.utime(other, (time.time() - week - 60,) * 2)
    assert ryc.cleanup_sessions(tmp_path) == 1
    assert not old.exists() and new.exists() and other.exists()


# --- T5.1: deploy ----------------------------------------------------------- #

UNITS = sorted((REPO / "deploy").glob("*.service"))
TIMERS = sorted((REPO / "deploy").glob("*.timer"))


def _ini(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(path)
    return cp


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_service_units(unit):
    svc = _ini(unit)["Service"]
    assert svc["User"] == "hermes" and svc["Group"] == "hermes"
    exe, script = svc["ExecStart"].split()
    assert exe == "/opt/hermes/.venv/bin/python"
    assert script.startswith("/opt/hermes/yield_rotation/")
    assert (REPO / script.removeprefix("/opt/hermes/yield_rotation/")).is_file()
    assert "DRY_RUN" not in unit.read_text()
    assert svc["NoNewPrivileges"] == "yes"


def test_timer_cadence():
    cal = {t.stem: _ini(t)["Timer"]["OnCalendar"] for t in TIMERS}
    assert cal == {"yield-cycle": "*:0/10", "yield-heartbeat": "*:2/5",
                   "yield-summary": "*-*-* 06:55:00 UTC"}
    for t in TIMERS:
        assert (REPO / "deploy" / f"{t.stem}.service").is_file()


def test_install_script_syntax():
    script = REPO / "deploy" / "install.sh"
    assert os.access(script, os.X_OK)
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text()
    assert "set -euo pipefail" in text
    assert ".env" not in text.split("\n", 3)[-1].replace("Never touches .env", "")


# --- DEPLOY.md guard rails --------------------------------------------------- #

DEPLOY = (REPO / "DEPLOY.md").read_text()
DEPLOY_SH = (REPO / "deploy" / "deploy.sh").read_text()


def test_deploy_is_one_command_and_a_log():
    cmd = DEPLOY[DEPLOY.index("## Η εντολή"):DEPLOY.index("## Τι κάνει")]
    assert "sudo -u hermes git pull --ff-only origin main" in cmd
    assert "sudo deploy/deploy.sh" in cmd
    assert "/opt/hermes/logs/deploy/deploy_" in cmd


def test_deploy_names_the_services_never_to_touch():
    for svc in ("hermes-gateway", "hermes-litellm", "hermes-george", "hermes-seo_agent"):
        assert svc in DEPLOY and svc in DEPLOY_SH


def test_deploy_sh_order_regression_before_systemd():
    assert DEPLOY_SH.index("run_step 6") < DEPLOY_SH.index("run_step 7")
    assert "run_regression.py" in DEPLOY_SH[DEPLOY_SH.index("step6_regression()"):
                                            DEPLOY_SH.index("step7_systemd()")]


def test_deploy_stops_before_testnet():
    testnet = DEPLOY[DEPLOY.index("## Μετά — Testnet"):DEPLOY.index("## Μετά — Επταήμερο")]
    assert "ΣΤΑΜΑΤΑ" in testnet and "έγκριση" in testnet
    assert "testnet" not in DEPLOY_SH.replace("Testnet is NOT part of this script", "") \
        .replace("anything against testnet", "")


def test_deploy_snippets_define_their_variables():
    for block in DEPLOY.split("```bash")[1:]:
        code = block.split("```")[0]
        if "$PY" in code or "$REPO" in code:
            assert "PY=/opt/hermes/.venv/bin/python" in code, code[:120]
