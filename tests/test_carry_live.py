"""The way to live (18/10), with config/carry.yaml DRY_RUN: true for ever:

1. carry/live.py + scripts/carry_live.py: a signed switch outside the repo,
   written only by Giannis on a terminal (one-time Telegram code, typed
   phrase, every check passing). deploy.sh never writes it and refuses to
   run while it exists; the supervised round trip refuses too.
2. DEADMAN_URL from /opt/hermes/.env, never the repo's config.
3. The Earn's 100 USDT tier boundary: what the carry does around it.
"""

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

import risk_state
import run_carry_cycle as rcc
import settings
from carry import book as book_store
from carry import config as cc
from carry import live
from carry import snapshot as sn
from carry.paper import PaperExchange, PaperState
from carry.plan import SymbolBook, plan_cycle
from carry_builders import NOW, account, cfg as base_cfg, earn, market, short, snapshot
from helpers import KEY

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import carry_live  # noqa: E402

DEADMAN = "https://hc-ping.com/0f3c-test"
TIERS = [{"min": "0", "max": "100", "estimateApr": "6.94%"},
         {"min": "100", "max": "-1", "estimateApr": "1.94%"}]


@pytest.fixture
def paths(isolated_paths, monkeypatch, tmp_path):
    monkeypatch.delenv("DEADMAN_URL", raising=False)
    return {"switch": settings.carry_live_file(), "book": settings.carry_book_file()}


def carry_yaml():
    return yaml.safe_load((REPO / "config" / "carry.yaml").read_text())


# --- 2. DEADMAN_URL ---------------------------------------------------------------------- #

def test_the_repo_never_holds_the_deadman_url():
    assert carry_yaml()["DEADMAN_URL"] is None
    assert carry_yaml()["DRY_RUN"] is True


def test_the_deadman_url_comes_from_the_env(paths):
    out, alerts, force, switch = live.effective(carry_yaml(), {"DEADMAN_URL": DEADMAN},
                                                paths["switch"], paths["book"], KEY)
    assert out["DEADMAN_URL"] == DEADMAN and out["DRY_RUN"] is True
    assert (alerts, force, switch) == ([], None, None)


def test_a_live_config_without_the_env_deadman_url_is_incomplete():
    cfg = dict(carry_yaml(), DRY_RUN=False)
    assert any(e.startswith("DEADMAN_URL") for e in cc.validate(cfg, testnet=False))
    assert not any(e.startswith("DEADMAN_URL")
                   for e in cc.validate(dict(cfg, DEADMAN_URL=DEADMAN), testnet=False))


# --- 1. the switch ---------------------------------------------------------------------- #

def test_no_switch_is_paper(paths):
    out, alerts, force, switch = live.effective(carry_yaml(), {}, paths["switch"],
                                                paths["book"], KEY)
    assert out["DRY_RUN"] is True and alerts == [] and force is None and switch is None


def test_a_valid_switch_is_live_with_the_signed_cap(paths):
    live.write(paths["switch"], KEY, 60.0, ["ETHUSDT"], "giannis", NOW)
    out, alerts, force, switch = live.effective(carry_yaml(), {"DEADMAN_URL": DEADMAN},
                                                paths["switch"], paths["book"], KEY)
    assert out["DRY_RUN"] is False and out["TOTAL_CAPITAL_CAP_USD"] == 60.0
    assert alerts == [] and force is None and switch["by"] == "giannis"


def test_the_signed_cap_is_never_raised_by_the_repo(paths):
    live.write(paths["switch"], KEY, 60.0, ["ETHUSDT"], "giannis", NOW)
    out = live.effective(dict(carry_yaml(), TOTAL_CAPITAL_CAP_USD=500), {}, paths["switch"],
                         paths["book"], KEY)[0]
    assert out["TOTAL_CAPITAL_CAP_USD"] == 60.0
    low = live.effective(dict(carry_yaml(), TOTAL_CAPITAL_CAP_USD=40), {}, paths["switch"],
                         paths["book"], KEY)[0]
    assert low["TOTAL_CAPITAL_CAP_USD"] == 40


@pytest.mark.parametrize("damage", ["cap", "key", "profile", "garbage"])
def test_a_switch_that_cannot_be_trusted_is_never_live(paths, damage):
    live.write(paths["switch"], KEY if damage != "key" else "another-key-" + "x" * 40, 60.0,
               ["ETHUSDT"], "giannis", NOW)
    data = json.loads(paths["switch"].read_text())
    if damage == "cap":
        data["capital_cap"] = 1000.0
    elif damage == "profile":
        data["profile"] = "hermes-carry-book"
    paths["switch"].write_text("{nope" if damage == "garbage" else json.dumps(data))
    out, alerts, force, switch = live.effective(carry_yaml(), {}, paths["switch"],
                                                paths["book"], KEY)
    assert out["DRY_RUN"] is True and switch is None and force is None
    assert len(alerts) == 1 and alerts[0].startswith("CRITICAL: LIVE_SWITCH_INVALID:")


@pytest.mark.parametrize("switch_state", ["absent", "broken"])
def test_a_live_position_without_a_valid_switch_runs_live_unwind_only(paths, switch_state):
    book_store.save(paths["book"], KEY, {"ETHUSDT": SymbolBook(status="OPEN", perp_qty=0.01,
                                                               spot_qty=0.01)}, NOW)
    if switch_state == "broken":
        paths["switch"].write_text("{}")
    out, alerts, force, switch = live.effective(carry_yaml(), {}, paths["switch"],
                                                paths["book"], KEY)
    assert out["DRY_RUN"] is False and force == "UNWIND" and switch is None
    assert any(a.startswith("CRITICAL: LIVE_WITHOUT_SWITCH:") for a in alerts)


def test_the_round_trip_config_is_left_alone(paths):
    cfg = dict(carry_yaml(), DRY_RUN=False)
    out, alerts, force, switch = live.effective(cfg, {}, paths["switch"], paths["book"], KEY)
    assert out == cfg and (alerts, force, switch) == ([], None, None)


# --- the cycle with the switch ----------------------------------------------------------- #

class Opener:
    def __init__(self):
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)

        class R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


def _sim(tmp_path, monkeypatch):
    from test_mainnet_roundtrip import Sim
    sim = Sim(tmp_path, monkeypatch)
    # a paper cycle also reads layer A's public APR
    sim.client.get_usdt_flexible_apr_history = lambda: ("1", [{"timestamp": "1",
                                                               "apr": "1.73%"}])
    return sim


def test_the_cycle_runs_live_only_with_the_switch_and_pings_the_env_deadman(
        isolated_paths, tmp_path, monkeypatch):
    sim = _sim(tmp_path, monkeypatch)
    cfg = cc.load(sim.cfg_file, testnet=False)
    assert cfg["DRY_RUN"] is True
    env = {"HERMES_RISK_HMAC_KEY": KEY, "DEADMAN_URL": DEADMAN}
    ping = Opener()
    rec, rc = rcc.run_cycle(cfg, env, sim.client, now_ms=sim.clock_ms, snapshot_fn=sim.snapshot,
                            exchange=sim.venue, opener=ping, sleep=sim.sleep)
    assert rc == 0 and rec["dry_run"] is True and "live_switch" not in rec, rec["alerts"]
    assert ping.urls == [DEADMAN]
    live.write(settings.carry_live_file(), KEY, 100.0, ["ETHUSDT"], "giannis", sim.clock_ms)
    sim.clock_ms += 300_000
    sim.renew()
    rec, rc = rcc.run_cycle(cfg, env, sim.client, now_ms=sim.clock_ms, snapshot_fn=sim.snapshot,
                            exchange=sim.venue, opener=ping, sleep=sim.sleep)
    assert rc == 0 and rec["dry_run"] is False, rec["alerts"]
    assert rec["live_switch"]["capital_cap"] == 100.0 and ping.urls == [DEADMAN, DEADMAN]
    # and without the .env's DEADMAN_URL a live cycle does nothing at all
    rec, rc = rcc.run_cycle(cfg, {"HERMES_RISK_HMAC_KEY": KEY}, sim.client,
                            now_ms=sim.clock_ms + 300_000, snapshot_fn=sim.snapshot,
                            exchange=sim.venue, opener=ping, sleep=sim.sleep)
    assert rc == 3 and any("DEADMAN_URL" in a for a in rec["alerts"])


# --- deploy, preflight and the round trip refuse a live carry ------------------------------- #

def test_preflight_dry_run_on_refuses_with_the_switch(paths, monkeypatch, capsys):
    sys.path.insert(0, str(REPO / "deploy"))
    import preflight
    monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(REPO / "config" / "carry.yaml"))
    assert preflight.main(["carry-dry-run-on"]) == 0
    live.write(paths["switch"], KEY, 60.0, ["ETHUSDT"], "giannis", NOW)
    assert preflight.main(["carry-dry-run-on"]) == 1
    assert "live switch" in capsys.readouterr().out


def test_preflight_keys_shows_the_deadman_url_as_a_fingerprint(paths, monkeypatch, capsys):
    sys.path.insert(0, str(REPO / "deploy"))
    import preflight
    monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(REPO / "config" / "carry.yaml"))
    settings.env_file().write_text(f"HERMES_RISK_HMAC_KEY={KEY}\nDEADMAN_URL={DEADMAN}\n")
    preflight.main(["keys", "--system", "carry"])
    out = capsys.readouterr().out
    assert "DEADMAN_URL" in out and "OK (from the .env" in out and DEADMAN not in out


def test_the_round_trip_refuses_next_to_a_live_carry(isolated_paths, tmp_path, monkeypatch):
    sim = _sim(tmp_path, monkeypatch)
    live.write(settings.carry_live_file(), KEY, 100.0, ["ETHUSDT"], "giannis", NOW)
    assert any("live switch" in p for p in sim.runner().checks())


# --- scripts/carry_live.py ------------------------------------------------------------------ #

class Tg:
    enabled = True

    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def event(self, text):
        self.sent.append(text)
        return self.ok

    def code(self):
        return re.search(r"code (\d{6})", self.sent[0]).group(1)


def flat_snap(c, cfg, now_ms=None, private=True):
    return snapshot(acct=account(usdt=0.0, symbols=("ETHUSDT",)),
                    positions={"ETHUSDT": short()}, earn_=earn(staked=57.0))


def open_snap(c, cfg, now_ms=None, private=True):
    return snapshot(acct=account(usdt=5.0, coins={"ETH": 0.01}, symbols=("ETHUSDT",)),
                    positions={"ETHUSDT": short(size=0.01)}, earn_=earn(staked=30.0))


def cli(answers=None, tty=True, snap=flat_snap, tg=None, env=None, cfg=None, client=object(),
        roundtrip=False):
    answers = list(answers or [])
    tg = tg or Tg()
    env = {"HERMES_RISK_HMAC_KEY": KEY, "BYBIT_CARRY_API_KEY": "k", "BYBIT_CARRY_API_SECRET": "s",
           "DEADMAN_URL": DEADMAN} if env is None else env
    out = []

    def ask(prompt):
        a = answers.pop(0)
        return tg.code() if a == "<code>" else a

    c = carry_live.LiveCli(cfg or base_cfg(DEADMAN_URL=None), env, client, tg, ask=ask,
                           isatty=lambda: tty, now_ms=lambda: NOW, clock=lambda: 0.0,
                           snapshot_fn=snap, roundtrip_active=lambda: roundtrip,
                           out=out.append)
    return c, tg, out


@pytest.fixture
def ready(paths):
    risk_state.write(settings.carry_risk_state_file(), KEY, "NORMAL", "hb",
                     risk_state.SOURCE_RENEW, ts_ms=NOW, profile=risk_state.CARRY_PROFILE)
    return paths


def test_enable_with_every_check_the_code_and_the_phrase(ready):
    c, tg, out = cli(["<code>", "LIVE 100 USDT"])
    assert c.enable(100.0) == 0, out
    body, broken = live.read(ready["switch"], KEY)
    assert broken is None and body["capital_cap"] == 100.0 and body["symbols"] == ["ETHUSDT"]
    assert "code" in tg.sent[0] and tg.sent[-1].startswith("🟢 CARRY LIVE enabled")
    assert base_cfg()["DRY_RUN"] is True                      # the repo is untouched


@pytest.mark.parametrize("answers,why", [(["000000x"], "wrong or expired code"),
                                         (["<code>", "LIVE 50 USDT"], "phrase did not match"),
                                         (["<code>", "yes"], "phrase did not match")])
def test_enable_refuses_a_wrong_code_or_phrase(ready, answers, why):
    c, tg, out = cli(answers)
    assert c.enable(100.0) == 1 and any(why in o for o in out)
    assert not ready["switch"].exists()


def test_enable_refuses_without_a_terminal(ready):
    c, tg, out = cli(tty=False)
    assert c.enable(100.0) == 1 and tg.sent == [] and not ready["switch"].exists()


@pytest.mark.parametrize("change,why", [
    ({"env": {"HERMES_RISK_HMAC_KEY": KEY, "BYBIT_CARRY_API_KEY": "k",
              "BYBIT_CARRY_API_SECRET": "s"}}, "DEADMAN_URL (https://...) not set"),
    ({"env": {"HERMES_RISK_HMAC_KEY": KEY, "DEADMAN_URL": DEADMAN}}, "BYBIT_CARRY_API_KEY"),
    ({"cfg": base_cfg(DEADMAN_URL="https://in.the/repo")}, "must stay null in the repo"),
    ({"cfg": base_cfg(DRY_RUN=False, DEADMAN_URL=None)}, "DRY_RUN must stay true"),
    ({"snap": open_snap}, "not FLAT"),
    ({"roundtrip": True}, "round trip"),
    ({"tg": Tg(ok=True)}, None),
])
def test_enable_refuses_unless_every_check_passes(ready, change, why):
    if why is None:
        change["tg"].enabled = False
        why = "Telegram not configured"
    c, tg, out = cli(["<code>", "LIVE 100 USDT"], **change)
    assert c.enable(100.0) == 1
    assert any(why in o for o in out), out
    assert not ready["switch"].exists()


@pytest.mark.parametrize("cap", [0, 101, 1000])
def test_enable_refuses_a_cap_above_the_config(ready, cap):
    c, tg, out = cli(["<code>", f"LIVE {cap:g} USDT"])
    assert c.enable(float(cap)) == 1 and not ready["switch"].exists()


def test_enable_refuses_a_cap_the_limits_do_not_fit(ready):
    """30 USDT: the buffer and the symbol's notional limit no longer fit."""
    c, tg, out = cli(["<code>", "LIVE 30 USDT"])
    assert c.enable(30.0) == 1 and any("live config:" in o for o in out), out


def test_enable_refuses_unless_the_risk_state_is_normal(paths):
    risk_state.write(settings.carry_risk_state_file(), KEY, "NO_NEW_POSITIONS", "op",
                     risk_state.SOURCE_OPERATOR, ts_ms=NOW, profile=risk_state.CARRY_PROFILE)
    c, tg, out = cli(["<code>", "LIVE 100 USDT"])
    assert c.enable(100.0) == 1 and any("risk state" in o for o in out)


def test_enable_refuses_twice(ready):
    live.write(ready["switch"], KEY, 100.0, ["ETHUSDT"], "giannis", NOW)
    c, tg, out = cli(["<code>", "LIVE 100 USDT"])
    assert c.enable(100.0) == 1 and any("exists already" in o for o in out)


def test_disable_only_when_flat(ready):
    live.write(ready["switch"], KEY, 100.0, ["ETHUSDT"], "giannis", NOW)
    c, tg, out = cli(snap=open_snap)
    assert c.disable() == 1 and ready["switch"].exists()
    assert any("/unwind carry" in o for o in out)
    c, tg, out = cli()
    assert c.disable() == 0 and not ready["switch"].exists()
    assert tg.sent[-1].startswith("⚪ CARRY LIVE disabled")


def test_status_reports_the_switch(ready):
    c, tg, out = cli()
    c.status()
    assert out[0].startswith("paper (DRY_RUN)")
    live.write(ready["switch"], KEY, 100.0, ["ETHUSDT"], "giannis", NOW)
    out.clear()
    c.status()
    assert out[0].startswith("LIVE:") and "capital cap 100 USDT" in out[0]


# --- 3. the Earn's 100 USDT tier boundary ----------------------------------------------------- #

def tiered(staked):
    t = sn.tiers_of({"hasTieredApr": True, "tierAprDetails": TIERS})
    return earn(staked=staked, apr=sn.tier_apr(t, staked))


@pytest.mark.parametrize("staked,apr", [(57.17, 0.0694), (99.99, 0.0694), (100.0, 0.0694),
                                        (100.01, (100 * 0.0694 + 0.01 * 0.0194) / 100.01),
                                        (150.0, (100 * 0.0694 + 50 * 0.0194) / 150)])
def test_layer_a_on_either_side_of_100(staked, apr):
    assert tiered(staked).apr == pytest.approx(apr)


def test_the_paper_interest_is_paid_tier_by_tier():
    """Above 100 USDT: 100 at 6.94 %, the rest at 1.94 % (a year, 150 USDT)."""
    t = sn.tiers_of({"hasTieredApr": True, "tierAprDetails": TIERS})
    p = PaperExchange(PaperState(earn_staked=150.0, last_ms=NOW), base_cfg())
    snap = snapshot(now=NOW + 365 * 86_400_000)
    [e] = [x for x in p.accrue(snap, 0.0194, t) if x["type"] == "earn_interest"]
    assert e["amount"] == pytest.approx(100 * 0.0694 + 50 * 0.0194)


def test_the_entry_hurdle_on_either_side_of_100():
    """The same funding (~14 % APR): with 57 or 100 USDT in Earn the layer A
    rate is 6.94 % and the MIN_HOLD payback fails: the carry stays out; with
    150 USDT the average rate on the whole amount (5.27 %) is layer A, and
    it enters. AUDIT note of 18/10 (live
    readiness): the entry gives up the TOP slice of the Earn balance, so its
    true cost is the marginal rate (1.94 % above 100), not the average."""
    m = market(rate=0.00013, settled=[0.00013] * 12)          # ~14 % APR
    decisions = {}
    for staked in (57.17, 100.0, 150.0):
        snap = snapshot(markets={"ETHUSDT": m}, earn_=tiered(staked), acct=account(usdt=0.0))
        d = plan_cycle(snap, base_cfg(SYMBOLS=["ETHUSDT"]), "NORMAL", {}, "c").decisions["ETHUSDT"]
        decisions[staked] = d
    assert decisions[57.17].action == decisions[100.0].action == "STAY_OUT"
    assert "MIN_HOLD income" in decisions[100.0].reason
    assert decisions[150.0].action == "ENTER" and "0.0527" in decisions[150.0].reason


def test_returning_to_earn_across_100_is_one_stake():
    """FLAT with 29.23 USDT idle and 95 in Earn: one EARN_RETURN of all of it
    (the tiers change nothing in what is returned)."""
    snap = snapshot(acct=account(usdt=29.23), earn_=tiered(95.0),
                    positions={"ETHUSDT": short()})
    plan = plan_cycle(snap, base_cfg(SYMBOLS=["ETHUSDT"]), "NO_NEW_POSITIONS", {}, "c")
    [a] = [x for x in plan.actions if x.kind == "EARN_RETURN"]
    assert a.usdt == 29.23
