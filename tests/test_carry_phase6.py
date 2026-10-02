"""CARRY_PLAN Phase 6 — units, deploy.sh, Telegram, daily summary, monthly
measurement. Decisions of 2/10 for this phase:

  - deploy.sh never resets the carry book and never changes the HMAC key
    while the carry holds positions (the book is signed with that key; a new
    key means BOOK_UNREADABLE and a hold);
  - deploying the carry disables the yield rotation's timers — never both;
  - the daily summary has one line per symbol: the smoothed funding now
    against the entry threshold ("ETH: 2,6% — χρειάζεται ...");
  - (13.7) a monthly re-measurement with a Telegram report.
No network: the exchange is a fake or a recorded snapshot.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

import heartbeat
import risk_state
import run_carry_cycle as rcc
import settings
import summary
import telegram_bot
from carry import book as book_store
from carry import exposure as cx
from carry import risk
from carry.paper import PaperExchange, PaperState
from carry.plan import SymbolBook
from carry_builders import account, cfg as base_cfg, market, short, snapshot
from carry_regimes import E8, H, FakeClient, Regime
from helpers import KEY
from telegram_bot import CommandBot
from test_deploy import CARRY_TIMERS, YIELD_TIMERS, _write_env, preflight, sandbox  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
LONG_KEY = "k" * 64


# =========================================================================== #
# carry/exposure.py                                                             #
# =========================================================================== #

def _book(path, key=LONG_KEY, **sb):
    book_store.save(path, key, {"ETHUSDT": SymbolBook(**sb)} if sb else {}, 1)


def _paper(path, **state):
    path.parent.mkdir(parents=True, exist_ok=True)
    PaperExchange(PaperState(**state), {}).save(path)


def test_exposure_not_configured_without_key_book_or_paper(tmp_path):
    e = cx.check(None, base_cfg(), tmp_path / "book.json", LONG_KEY, tmp_path / "paper.json")
    assert e.verdict == cx.NOT_CONFIGURED and e.flat


def test_exposure_paper_position_counts_under_dry_run(tmp_path):
    _book(tmp_path / "book.json", status="OPEN", perp_qty=0.03, spot_qty=0.03)
    _paper(tmp_path / "paper.json", usdt=10.0, coins={"ETH": 0.03}, shorts={"ETHUSDT": 0.03})
    e = cx.check(None, base_cfg(), tmp_path / "book.json", LONG_KEY, tmp_path / "paper.json")
    assert e.verdict == cx.OPEN and not e.flat


def test_exposure_flat_paper_and_flat_book_under_dry_run(tmp_path):
    _book(tmp_path / "book.json", status="FLAT")
    _paper(tmp_path / "paper.json", usdt=0.0, earn_staked=100.0)
    e = cx.check(None, base_cfg(), tmp_path / "book.json", LONG_KEY, tmp_path / "paper.json")
    assert e.verdict == cx.FLAT


def test_exposure_live_book_without_key_is_unknown(tmp_path):
    _book(tmp_path / "book.json", status="FLAT")
    e = cx.check(None, base_cfg(DRY_RUN=False), tmp_path / "book.json", LONG_KEY)
    assert e.verdict == cx.UNKNOWN and not e.flat


def test_exposure_unreadable_paper_is_unknown(tmp_path):
    (tmp_path / "paper.json").write_text("{")
    e = cx.check(None, base_cfg(), tmp_path / "book.json", LONG_KEY, tmp_path / "paper.json")
    assert e.verdict == cx.UNKNOWN


@pytest.mark.parametrize("snap,verdict", [
    (snapshot(acct=account(coins={})), cx.FLAT),
    (snapshot(positions={"ETHUSDT": short(size=0.03)}, acct=account(coins={})), cx.OPEN),
    (snapshot(acct=account(coins={"ETH": 0.03})), cx.OPEN),                 # spot above dust
    (snapshot(acct=account(coins={"ETH": 0.0001})), cx.FLAT),               # dust (< min qty)
    (snapshot(errors={"positions:ETHUSDT": "HTTP 500"}), cx.UNKNOWN),
    (snapshot(errors={"account": "timeout"}), cx.UNKNOWN),
])
def test_exposure_reads_the_exchange(tmp_path, monkeypatch, snap, verdict):
    monkeypatch.setattr(cx.snapshot_mod, "take", lambda client, cfg, now_ms=None, private=True: snap)
    e = cx.check(object(), base_cfg(DRY_RUN=False), tmp_path / "book.json", LONG_KEY)
    assert e.verdict == verdict, e.lines


def test_exposure_exchange_exception_is_unknown(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(cx.snapshot_mod, "take", boom)
    assert cx.check(object(), base_cfg(), tmp_path / "b.json", LONG_KEY).verdict == cx.UNKNOWN


# =========================================================================== #
# preflight: the HMAC key and the carry book                                    #
# =========================================================================== #

def _profile(isolated_paths, **kv):
    _write_env(Path(isolated_paths["YIELD_ENV_FILE"]), **kv)
    return Path(isolated_paths["YIELD_ENV_FILE"])


def _shared(isolated_paths, **kv):
    _write_env(Path(isolated_paths["YIELD_SHARED_ENV_FILE"]), **kv)


def test_hmac_key_is_not_changed_while_the_carry_holds_a_position(isolated_paths, capsys):
    prof = _profile(isolated_paths, HERMES_RISK_HMAC_KEY=KEY, BYBIT_API_KEY="k",
                    BYBIT_API_SECRET="s", TELEGRAM_BOT_TOKEN="t")
    _book(settings.carry_book_file(), key=KEY, status="OPEN", perp_qty=0.03, spot_qty=0.03)
    _paper(settings.carry_paper_file(), coins={"ETH": 0.03}, shorts={"ETHUSDT": 0.03})
    before = prof.read_text()
    assert preflight.main(["keys"]) == 1
    out = capsys.readouterr().out
    assert prof.read_text() == before, "nothing written"
    assert "NOT CHANGED" in out and "carry holds positions" in out
    assert KEY not in out
    assert book_store.load(settings.carry_book_file(), KEY)[1] is None, "book still readable"


def test_hmac_key_is_not_changed_when_the_carry_cannot_be_read(isolated_paths, monkeypatch):
    prof = _profile(isolated_paths, HERMES_RISK_HMAC_KEY=KEY, BYBIT_API_KEY="k",
                    BYBIT_API_SECRET="s", TELEGRAM_BOT_TOKEN="t",
                    BYBIT_CARRY_API_KEY="ck", BYBIT_CARRY_API_SECRET="cs")
    monkeypatch.setattr(cx.snapshot_mod, "take",
                        lambda *a, **k: snapshot(errors={"positions:ETHUSDT": "HTTP 403"}))
    before = prof.read_text()
    assert preflight.main(["keys"]) == 1
    assert prof.read_text() == before


def test_hmac_key_is_generated_when_the_carry_is_flat(isolated_paths, monkeypatch):
    prof = _profile(isolated_paths, HERMES_RISK_HMAC_KEY=KEY, BYBIT_API_KEY="k",
                    BYBIT_API_SECRET="s", TELEGRAM_BOT_TOKEN="t",
                    BYBIT_CARRY_API_KEY="ck", BYBIT_CARRY_API_SECRET="cs")
    monkeypatch.setattr(cx.snapshot_mod, "take", lambda *a, **k: snapshot(acct=account(coins={})))
    assert preflight.main(["keys"]) == 0
    assert settings.load_env_file(prof)["HERMES_RISK_HMAC_KEY"] != KEY


def test_keys_for_the_carry_need_no_yield_key_and_never_share_it(isolated_paths, capsys):
    _profile(isolated_paths, HERMES_RISK_HMAC_KEY=LONG_KEY, TELEGRAM_BOT_TOKEN="t")
    assert preflight.main(["keys", "--system", "carry"]) == 0     # DRY_RUN: carry key optional
    assert preflight.main(["keys"]) == 1                          # the yield rotation needs its own
    _profile(isolated_paths, HERMES_RISK_HMAC_KEY=LONG_KEY, TELEGRAM_BOT_TOKEN="t",
             BYBIT_API_KEY="same-key", BYBIT_API_SECRET="s1",
             BYBIT_CARRY_API_KEY="same-key", BYBIT_CARRY_API_SECRET="s2")
    capsys.readouterr()
    assert preflight.main(["keys", "--system", "carry"]) == 1
    out = capsys.readouterr().out
    assert "never share an account" in out and "same-key" not in out


def test_keys_for_the_carry_refuse_testnet(isolated_paths, monkeypatch):
    _profile(isolated_paths, HERMES_RISK_HMAC_KEY=LONG_KEY, TELEGRAM_BOT_TOKEN="t")
    monkeypatch.setenv("BYBIT_TESTNET", "1")
    assert preflight.main(["keys", "--system", "carry"]) == 1


def test_unreadable_book_is_kept_while_the_carry_holds_a_position(isolated_paths, capsys):
    _profile(isolated_paths, HERMES_RISK_HMAC_KEY=LONG_KEY)
    _book(settings.carry_book_file(), key="the-old-key", status="OPEN", perp_qty=0.03,
          spot_qty=0.03)
    _paper(settings.carry_paper_file(), coins={"ETH": 0.03}, shorts={"ETHUSDT": 0.03})
    before = settings.carry_book_file().read_bytes()
    assert preflight.main(["carry-reset-book-if-invalid"]) == 1
    assert settings.carry_book_file().read_bytes() == before
    assert "/adopt carry" in capsys.readouterr().out


def test_unreadable_book_is_moved_aside_only_when_flat(isolated_paths):
    _profile(isolated_paths, HERMES_RISK_HMAC_KEY=LONG_KEY)
    _book(settings.carry_book_file(), key="the-old-key", status="FLAT")
    _paper(settings.carry_paper_file(), earn_staked=100.0)
    assert preflight.main(["carry-reset-book-if-invalid"]) == 0
    assert not settings.carry_book_file().exists()
    assert list(settings.carry_book_file().parent.glob("carry_book.json.unreadable.*"))


def test_valid_or_missing_book_is_kept(isolated_paths):
    _profile(isolated_paths, HERMES_RISK_HMAC_KEY=LONG_KEY)
    assert preflight.main(["carry-reset-book-if-invalid"]) == 0             # absent
    _book(settings.carry_book_file(), status="OPEN", perp_qty=0.03, spot_qty=0.03)
    before = settings.carry_book_file().read_bytes()
    assert preflight.main(["carry-reset-book-if-invalid"]) == 0
    assert settings.carry_book_file().read_bytes() == before


def test_carry_exposure_command(isolated_paths, capsys):
    _profile(isolated_paths, HERMES_RISK_HMAC_KEY=LONG_KEY)
    assert preflight.main(["carry-exposure"]) == 0                          # never ran
    _paper(settings.carry_paper_file(), shorts={"ETHUSDT": 0.03}, coins={"ETH": 0.03})
    assert preflight.main(["carry-exposure"]) == 1
    assert "paper ETHUSDT: short 0.03" in capsys.readouterr().out


def test_carry_dry_run_on(isolated_paths, monkeypatch, tmp_path):
    path = tmp_path / "carry.yaml"
    for value, rc in ((True, 0), (False, 1), ("true", 1)):
        path.write_text(yaml.safe_dump(base_cfg(DRY_RUN=value)))
        monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(path))
        assert preflight.main(["carry-dry-run-on"]) == rc


# =========================================================================== #
# the carry cycle record, the heartbeat, the carry key                          #
# =========================================================================== #

@pytest.fixture
def live_clock_regime():
    """A bullish regime around the real clock: the heartbeat writes with the
    real time, so the cycle must run at the real time too."""
    import time
    now = int(time.time() * 1000)
    return Regime([(40, 0.0003)], start_ms=now - 20 * 24 * H - (now % E8))


@pytest.fixture
def carry_files(isolated_paths, tmp_path, monkeypatch):
    log_dir = tmp_path / "carry_logs"
    log_dir.mkdir()
    c = base_cfg(LOG_DIR=str(log_dir), DEADMAN_URL=None)
    path = tmp_path / "carry.yaml"
    path.write_text(yaml.safe_dump(c))
    monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(path))
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    monkeypatch.setenv("YIELD_SKIP_API_CHECK", "1")
    return {"cfg": c, "log_dir": log_dir}


def _cycle(carry_files, regime):
    return rcc.run_cycle(carry_files["cfg"], {"HERMES_RISK_HMAC_KEY": KEY}, FakeClient(),
                         snapshot_fn=regime.snapshot_fn(), paper_start=(0.0, 100.0),
                         sleep=lambda s: None)


def test_heartbeat_promotes_the_carry_bootstrap_after_a_clean_cycle(carry_files,
                                                                      live_clock_regime):
    """The deploy's step 5 for the carry: bootstrap -> cycle -> NORMAL. Needs
    the cycle record to carry risk_state_meta (it did not before Phase 6)."""
    state = settings.carry_risk_state_file()
    assert heartbeat.main(["--system", "carry"]) == 0
    v = risk_state.verify(state, KEY, profile=risk_state.CARRY_PROFILE)
    assert (v.state, v.source) == ("NO_NEW_POSITIONS", risk_state.SOURCE_BOOTSTRAP)
    rec, rc = _cycle(carry_files, live_clock_regime)
    assert rc == 0, rec["alerts"]
    assert rec["risk_state_meta"]["signature_valid"] is True
    assert rec["risk_state_meta"]["ts"] == v.ts
    assert heartbeat.main(["--system", "carry"]) == 0
    assert risk_state.verify(state, KEY, profile=risk_state.CARRY_PROFILE).state == "NORMAL"
    assert preflight.main(["carry-check-cycle"]) == 0
    assert preflight.main(["carry-expect-normal"]) == 0


def test_cycle_record_has_the_funding_view(carry_files, live_clock_regime):
    rec, _ = _cycle(carry_files, live_clock_regime)
    f = rec["funding"]["ETHUSDT"]
    assert f["smoothed_apr"] == pytest.approx(0.0003 * 3 * 365)
    assert f["entry_min_expected_apr"] == 0.05
    assert f["required_apr"] == pytest.approx(0.05 + f["layer_a_apr"])


def test_carry_client_never_falls_back_to_the_yield_key(monkeypatch):
    from carry.client import CarryClient, CarryPublicClient
    monkeypatch.setenv("BYBIT_API_KEY", "yield-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "yield-secret")
    for cls in (CarryClient, CarryPublicClient):
        c = cls(testnet=False)
        assert c.api_key is None and c.api_secret is None


def test_carry_main_signs_with_the_carry_key(isolated_paths, monkeypatch):
    import carry.client
    seen = {}

    class Recorder:
        testnet = False

        def __init__(self, api_key=None, api_secret=None, **kw):
            seen.update(key=api_key, secret=api_secret)

    monkeypatch.setattr(carry.client, "CarryClient", Recorder)
    monkeypatch.setattr(rcc, "run_cycle", lambda *a, **k: ({"cycle_id": "x", "alerts": []}, 0))
    monkeypatch.setenv("BYBIT_API_KEY", "yield-key")
    monkeypatch.setenv("BYBIT_CARRY_API_KEY", "carry-key")
    monkeypatch.setenv("BYBIT_CARRY_API_SECRET", "carry-secret")
    assert rcc.main() == 0
    assert seen == {"key": "carry-key", "secret": "carry-secret"}


def test_carry_alerts_reach_the_operator_chat(isolated_paths):
    """config/carry.yaml has no chat: the carry's alerts go to the operator's."""
    from notify import Notifier
    n = Notifier({"TELEGRAM_BOT_TOKEN": "t"}, base_cfg())
    yield_cfg = yaml.safe_load(Path(isolated_paths["YIELD_CONFIG_FILE"]).read_text())
    assert n.chat == str(yield_cfg["ALERT_TELEGRAM_CHAT_ID"]) and n.enabled


# =========================================================================== #
# daily summary                                                                 #
# =========================================================================== #

@pytest.mark.parametrize("f,status,line", [
    ({"smoothed_apr": 0.026, "layer_a_apr": 0.017, "entry_min_expected_apr": 0.05,
      "required_apr": 0.067}, "FLAT", "ETH: 2,6% — χρειάζεται 6,7% (5% πάνω από το Earn 1,7%)"),
    ({"smoothed_apr": 0.081, "layer_a_apr": 0.017, "entry_min_expected_apr": 0.05,
      "required_apr": 0.067}, "OPEN", "ETH: 8,1% — θέση ανοιχτή"),
    ({"smoothed_apr": -0.012, "layer_a_apr": None, "entry_min_expected_apr": 0.05,
      "required_apr": None}, None, "ETH: -1,2% — χρειάζεται 5% πάνω από το Earn (APR του Earn άγνωστο)"),
    ({"smoothed_apr": None}, None, "ETH: εξομαλυμένο funding άγνωστο (λίγα settlements)"),
])
def test_funding_line(f, status, line):
    assert summary.funding_line("ETHUSDT", f, status) == line


def test_carry_summary_after_cycles(carry_files, live_clock_regime):
    for _ in range(2):
        _cycle(carry_files, live_clock_regime)
    risk.write_hold(settings.carry_hold_file(), "orphan protection", 1)
    text = summary.build_carry_summary(carry_files["log_dir"], settings.carry_risk_state_file(),
                                       settings.carry_hold_file(), KEY)
    assert text.startswith("📊 Carry — last 24 h  [DRY_RUN]")
    assert "cycles: 2" in text
    assert "hold: orphan protection" in text
    assert "ETH: 32,8% — χρειάζεται 6,7% (5% πάνω από το Earn 1,7%)" in text   # 0.03 %/8h
    assert "RISK_STATE_MISSING×2" in text and "RISK_STATE_RISK_STATE" not in text
    assert "ledger: funding" in text and "paper account: USDT" in text


def test_carry_summary_main_sends(carry_files, live_clock_regime, monkeypatch):
    _cycle(carry_files, live_clock_regime)
    sent = []
    monkeypatch.setattr(summary.Notifier, "event", lambda self, text: sent.append(text) or True)
    assert summary.main(["--system", "carry"]) == 0
    assert sent and sent[0].startswith("📊 Carry")


# =========================================================================== #
# Telegram                                                                      #
# =========================================================================== #

def msg(text):
    return {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": text}}


@pytest.fixture
def bot2():
    writes = []
    b = CommandBot("42", lambda s, r: writes.append(("yield", s)), lambda: "status",
                   new_code=lambda: "1",
                   write_carry_state=lambda s, r: writes.append(("carry", s)))
    b.writes = writes
    return b


@pytest.mark.parametrize("cmd,expected", [
    ("/unwind carry", [("carry", "UNWIND")]),
    ("/resume carry", [("carry", "NORMAL")]),
    ("/unwind all", [("yield", "UNWIND"), ("carry", "UNWIND")]),
    ("/unwind", [("yield", "UNWIND")]),
    ("/resume", [("yield", "NORMAL")]),
])
def test_scoped_commands(bot2, cmd, expected):
    assert "/confirm 1" in bot2.handle(msg(cmd))[1] and bot2.writes == []
    reply = bot2.handle(msg("/confirm 1"))[1]
    assert bot2.writes == expected and "✅" in reply


def test_resume_carry_says_the_hold_is_released(bot2):
    bot2.handle(msg("/resume carry"))
    assert "CARRY_HOLD" in bot2.handle(msg("/confirm 1"))[1]


@pytest.mark.parametrize("cmd", ["/resume all", "/unwind foo", "/unwind carry now"])
def test_unknown_scopes_do_nothing(bot2, cmd):
    assert bot2.handle(msg(cmd))[1] == telegram_bot.HELP
    assert bot2.pending is None and bot2.writes == []


def test_carry_scope_without_a_carry_writer_never_falls_to_yield():
    writes = []
    b = CommandBot("42", lambda s, r: writes.append(s), lambda: "", new_code=lambda: "1")
    assert "not available" in b.handle(msg("/unwind carry"))[1]
    assert b.handle(msg("/confirm 1"))[1] == "Nothing to confirm." and writes == []


def test_operator_write_releases_the_hold(carry_files, live_clock_regime):
    """/resume carry writes the carry risk state as operator -> the next
    cycle releases a CARRY_HOLD (risk.hold_released)."""
    risk.write_hold(settings.carry_hold_file(), "orphan protection", 1)
    rec, _ = _cycle(carry_files, live_clock_regime)
    assert any(a.startswith("CARRY_HOLD") for a in rec["alerts"])
    risk_state.write(settings.carry_risk_state_file(), KEY, "NORMAL", "telegram", "operator",
                     profile=risk_state.CARRY_PROFILE)
    rec, _ = _cycle(carry_files, live_clock_regime)
    assert any(a.startswith("HOLD_RELEASED") for a in rec["alerts"])
    assert risk.read_hold(settings.carry_hold_file()) is None


def test_status_text_shows_both_systems_and_the_hold(isolated_paths):
    risk_state.write(settings.risk_state_file(), KEY, "NORMAL", "t", "heartbeat_renew")
    risk_state.write(settings.carry_risk_state_file(), KEY, "UNWIND", "t", "operator",
                     profile=risk_state.CARRY_PROFILE)
    risk.write_hold(settings.carry_hold_file(), "BOOK_MISMATCH", 1)
    text = telegram_bot.status_text(KEY)
    assert "yield rotation: NORMAL" in text and "carry: UNWIND" in text
    assert "carry hold: BOOK_MISMATCH" in text


# =========================================================================== #
# monthly measurement (decision 13.7)                                           #
# =========================================================================== #

def test_monthly_measurement_offline(isolated_paths, tmp_path, monkeypatch):
    from tools import carry_monthly
    data = sorted((REPO / "calibration").glob("carry_data_*.json"))[-1]
    sent = []
    import notify
    monkeypatch.setattr(notify.Notifier, "event", lambda self, text: sent.append(text) or True)
    cfg_before = (REPO / "config" / "carry.yaml").read_bytes()
    assert carry_monthly.main(["--from-data", str(data), "--out", str(tmp_path / "m")]) == 0
    report = json.loads((tmp_path / "m" / "carry_calibration.json").read_text())
    assert report["shipped_thresholds"]["ETHUSDT"]["entries"] == 1          # as locked (13.12)
    assert (tmp_path / "m" / "CARRY_CALIBRATION.md").exists()
    assert sent and "monthly measurement" in sent[0] and "NO-GO" in sent[0]
    assert "ETHUSDT: entries 1" in sent[0] and "NOT changed" in sent[0]
    assert (REPO / "config" / "carry.yaml").read_bytes() == cfg_before


# =========================================================================== #
# deploy.sh with --system carry                                                 #
# =========================================================================== #

def _as_carry_active(box):
    """systemd after a carry install: carry timers on, yield timers off."""
    for t in CARRY_TIMERS:
        (box.dir / f"systemctl.is-enabled.{t}.out").write_text("enabled\n")
        (box.dir / f"systemctl.is-active.{t}.out").write_text("active\n")
    for t in YIELD_TIMERS:
        (box.dir / f"systemctl.is-enabled.{t}.out").write_text("disabled\n")
        (box.dir / f"systemctl.is-active.{t}.out").write_text("inactive\n")


def _only_yield_units(box):
    for verb, *args in box.systemctl_mutations():
        units = [a for a in args if not a.startswith("-")]
        assert units and all(u.startswith("yield-") for u in units), (verb, args)


def test_deploy_carry_runs_the_carry_steps_only(sandbox):
    _as_carry_active(sandbox)
    r = _deploy(sandbox, "--system", "carry")
    assert r.returncode == 0, r.stdout
    assert "ALL 7 STEPS PASSED — carry timers running" in r.stdout
    calls = sandbox.calls()
    joined = "\n".join(calls)
    for c in ("preflight.py keys --system carry", "carry-dry-run-on",
              "carry-reset-state-if-invalid", "carry-reset-book-if-invalid",
              "heartbeat.py --system carry", "run_carry_cycle.py", "carry-check-cycle",
              "carry-expect-normal", "install.sh --system carry"):
        assert c in joined, c
    for never in ("run_yield_cycle", "run_regression", "hermes chat", "testnet"):
        assert never not in joined, never
    i_book = next(i for i, c in enumerate(calls) if "carry-reset-book-if-invalid" in c)
    i_cycle = next(i for i, c in enumerate(calls) if "run_carry_cycle.py" in c)
    assert i_book < i_cycle
    _only_yield_units(sandbox)


def _deploy(box, *args):
    return box.run(*args)


def test_deploy_carry_fails_if_a_yield_timer_is_still_active(sandbox):
    _as_carry_active(sandbox)
    (sandbox.dir / "systemctl.is-active.yield-cycle.timer.out").write_text("active\n")
    r = _deploy(sandbox, "--system", "carry")
    assert "FAIL step 7/7" in r.stdout


def test_deploy_without_system_keeps_the_active_carry(sandbox):
    _as_carry_active(sandbox)
    r = _deploy(sandbox)
    assert r.returncode == 0, r.stdout
    assert "system carry" in r.stdout
    assert any("install.sh --system carry" in c for c in sandbox.calls())


def test_switch_back_to_yield_refused_while_the_carry_holds_positions(sandbox):
    _as_carry_active(sandbox)
    sandbox.reply("python.preflight.carry-exposure", out="carry exposure: OPEN\n", rc=1)
    r = _deploy(sandbox, "--system", "yield")
    assert "FAIL step 1/7" in r.stdout and "REFUSED" in r.stdout
    assert sandbox.systemctl_mutations() == []
    assert not any("install.sh" in c for c in sandbox.calls())


def test_switch_back_to_yield_disables_the_carry_timers_when_flat(sandbox):
    _as_carry_active(sandbox)
    _deploy(sandbox, "--system", "yield")
    # the stub systemd keeps answering "carry active", so step 7's
    # never-both check fails after the switch; what matters is the order.
    calls = sandbox.calls()
    exposure = [i for i, c in enumerate(calls) if "carry-exposure" in c]
    disables = [i for i, c in enumerate(calls) if c.startswith("systemctl disable")
                and "yield-carry-" in c]
    install = [i for i, c in enumerate(calls) if "install.sh --system yield" in c]
    assert exposure and disables and install
    assert exposure[-1] < disables[0] < install[0]
    assert len(disables) == len(CARRY_TIMERS)
    _only_yield_units(sandbox)


def test_carry_book_guard_failure_stops_before_the_cycle(sandbox):
    _as_carry_active(sandbox)
    sandbox.reply("python.preflight.carry-reset-book-if-invalid", out="ERROR: book", rc=1)
    r = _deploy(sandbox, "--system", "carry")
    assert "FAIL step 5/7" in r.stdout
    assert not any("run_carry_cycle" in c for c in sandbox.calls())
    assert not any("install.sh" in c for c in sandbox.calls())


def test_live_carry_config_installs_nothing(sandbox, tmp_path):
    _as_carry_active(sandbox)
    path = tmp_path / "carry_live.yaml"
    path.write_text(yaml.safe_dump(base_cfg(DRY_RUN=False)))
    sandbox.env_extra = {"YIELD_CARRY_CONFIG_FILE": str(path),
                         "STUB_PASSTHROUGH": "python.preflight.carry-dry-run-on"}
    r = _deploy(sandbox, "--system", "carry")
    assert "FAIL step 5/7" in r.stdout and "DRY_RUN" in r.stdout
    assert not any("run_carry_cycle" in c or "install.sh" in c for c in sandbox.calls())
    assert sandbox.systemctl_mutations() == []


def test_bad_system_argument(sandbox):
    r = _deploy(sandbox, "--system", "both")
    assert r.returncode == 2 and sandbox.calls() == []


def test_yield_deploy_checks_the_carry_timers_are_off(sandbox):
    (sandbox.dir / "systemctl.is-active.yield-carry-cycle.timer.out").write_text("active\n")
    r = _deploy(sandbox)
    assert "FAIL step 7/7" in r.stdout


# =========================================================================== #
# install.sh and the units                                                      #
# =========================================================================== #

INSTALL = (REPO / "deploy" / "install.sh").read_text()


def test_install_carry_stops_the_yield_timers_before_starting_the_carry():
    body = INSTALL[INSTALL.index('if [[ $SYSTEM == carry ]]; then\n    for t in "${YIELD'):]
    assert body.index('disable --now "$t"') < body.index('enable --now "$t"')
    for t in YIELD_TIMERS:
        assert t in INSTALL
    for t in CARRY_TIMERS:
        assert t in INSTALL


def test_install_yield_refuses_while_a_carry_timer_is_enabled():
    guard = INSTALL[INSTALL.index('if [[ $SYSTEM == yield ]]; then'):INSTALL.index("install -d")]
    assert "CARRY_TIMERS" in guard and "REFUSED" in guard and "exit 1" in guard


def test_install_never_touches_state_or_secrets():
    code = "\n".join(l for l in INSTALL.splitlines() if not l.lstrip().startswith("#"))
    for f in (".env", "carry_book", "risk_state", "carry_paper", "DRY_RUN"):
        assert f not in code, f


def test_carry_units_have_no_secret_or_dry_run():
    for unit in (REPO / "deploy").glob("yield-carry-*"):
        text = unit.read_text()
        assert "DRY_RUN" not in text and "KEY" not in text and "testnet" not in text.lower()


def test_ci_installs_both_systems_one_at_a_time():
    ci = (REPO / ".github" / "workflows" / "tests.yml").read_text()
    assert "install.sh --system carry" in ci
    assert "yield-carry-cycle.timer" in ci


def test_deploy_md_documents_the_switch_and_the_giannis_list():
    text = (REPO / "DEPLOY.md").read_text()
    assert "--system carry" in text and "--system yield" in text
    assert "BYBIT_CARRY_API_KEY" in text
    assert "Bybit UI" in text
