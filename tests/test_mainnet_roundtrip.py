"""scripts/mainnet_roundtrip.py — ONE supervised carry round trip with real
money on the carry subaccount (17/10, instead of testnet).

Written before the script. What it must do: redeem from Earn -> entry at the
MINIMUM ETHUSDT size -> wait one settlement -> exit -> USDT back to Earn,
then stop by itself. With the production cycle (run_carry_cycle.run_cycle:
same plan, execution, book and ledger) and the raw responses recorded
(body only, no header or key).

What it must refuse: running without --i-approve-real-money; more than 50
USDT notional (a hard limit no option lifts); running while a carry timer is
active; starting from anything but FLAT; changing config/carry.yaml
(DRY_RUN stays true there); writing the risk state.

The market is synthetic and the "exchange" is the paper account standing in
for Bybit; no network.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

import risk_state
import run_carry_cycle as rcc
import settings
from carry import book as book_store
from carry import config as cc
from carry import ledger, risk
from carry.paper import PaperExchange, PaperState
from carry.plan import FLAT, OPEN, SymbolBook
from carry.testnet_record import ResponseRecorder
from carry_regimes import E8, H, Regime
from helpers import KEY

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import mainnet_roundtrip as rt  # noqa: E402

MIN = 60_000
# 30' (and 7 s, so that no cycle lands on the exact settlement millisecond)
# after a settlement
T0 = 1_792_000_800_000 - (1_792_000_800_000 % E8) + 30 * MIN + 7_000
CARRY_YAML = REPO / "config" / "carry.yaml"


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# =========================================================================== #
# configuration: the hard limit and the production path                       #
# =========================================================================== #

def _base():
    return cc.load(CARRY_YAML, testnet=False)


def test_hard_limit_is_50_usdt_and_not_an_option():
    assert cc.ROUNDTRIP_MAX_NOTIONAL_USD == 50
    assert rt.MAX_NOTIONAL_USD == 50
    with pytest.raises(SystemExit):
        rt.parse_args(["--i-approve-real-money", "--max-notional", "100"])


def test_roundtrip_config_validates_only_as_a_roundtrip():
    cfg = rt.roundtrip_cfg(_base(), notional=30.0)
    assert cfg["DRY_RUN"] is False and cfg["SYMBOLS"] == ["ETHUSDT"]
    assert cc.validate(cfg, testnet=False, roundtrip=True) == []
    # the normal path refuses it: below the production floors
    assert any("production floor" in e for e in cc.validate(cfg, testnet=False))
    # never on testnet
    assert any("ROUNDTRIP" in e for e in cc.validate(cfg, testnet=True, roundtrip=True))


@pytest.mark.parametrize("key,value", [("MAX_NOTIONAL_PER_SYMBOL_USD", 50.01),
                                       ("TOTAL_CAPITAL_CAP_USD", 60),
                                       ("SYMBOLS", ["ETHUSDT", "BTCUSDT"]),
                                       ("DRY_RUN", True)])
def test_roundtrip_validation_enforces_the_limits(key, value):
    cfg = dict(rt.roundtrip_cfg(_base(), notional=30.0), **{key: value})
    assert cc.validate(cfg, testnet=False, roundtrip=True)


def test_roundtrip_cfg_never_exceeds_the_limit():
    with pytest.raises(ValueError, match="50"):
        rt.roundtrip_cfg(_base(), notional=50.5)


def test_force_state_only_makes_the_cycle_stricter():
    assert rcc.stricter("NO_NEW_POSITIONS", "NORMAL") == "NO_NEW_POSITIONS"
    assert rcc.stricter("NORMAL", "UNWIND") == "UNWIND"


# =========================================================================== #
# raw responses: body only, mainnet only with the approval                     #
# =========================================================================== #

def test_mainnet_recorder_needs_the_explicit_approval(tmp_path):
    with pytest.raises(ValueError):
        ResponseRecorder(tmp_path, testnet=False)
    assert ResponseRecorder(tmp_path, testnet=False, approved_mainnet=True).out_dir == tmp_path


def test_mainnet_recorder_keeps_only_the_body(tmp_path):
    from test_carry_testnet_run import API_KEY, API_SECRET, FakeBybit
    from carry.client import CarryClient
    rec = ResponseRecorder(tmp_path / "raw", testnet=False, approved_mainnet=True)
    fake = FakeBybit({"retCode": 0, "retMsg": "OK", "result": {"list": [], "nextPageCursor": ""}})
    rec.mount("https://", fake)
    CarryClient(api_key=API_KEY, api_secret=API_SECRET, testnet=False,
                session=rec).get_positions("ETHUSDT")
    text = next((tmp_path / "raw").glob("raw_*.jsonl")).read_text()
    sig = fake.sent[0].headers["X-BAPI-SIGN"]
    for s in (API_KEY, API_SECRET, sig, "X-BAPI", "symbol=ETHUSDT"):
        assert s not in text


# =========================================================================== #
# a simulated round trip: the paper account stands in for Bybit                #
# =========================================================================== #

class SimClient:
    """What the live cycle reads besides the snapshot: the transaction log."""
    testnet = False

    def __init__(self):
        self.settlements = []

    def get_transaction_log(self, start_ms, end_ms, currency="USDT", type_=None):
        return [r for r in self.settlements if start_ms <= int(r["transactionTime"]) <= end_ms]


class SimVenue(PaperExchange):
    live = True          # stands in for the real exchange: DRY_RUN false


class Sim:
    def __init__(self, tmp_path, monkeypatch, rate=0.0001, earn=40.0, usdt=0.0, price=2500.0):
        self.clock_ms = T0
        log_dir = tmp_path / "carry_logs"
        cfg = yaml.safe_load(CARRY_YAML.read_text())
        cfg["LOG_DIR"] = str(log_dir)
        path = tmp_path / "carry.yaml"
        path.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(path))
        monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
        self.cfg_file, self.log_dir = path, log_dir
        self.regime = Regime([(30, rate)], start_ms=T0 - 10 * 24 * H, price=lambda t: price)
        self.client = SimClient()
        self.venue = SimVenue(PaperState(usdt=usdt, earn_staked=earn), cfg)
        self.venue.s.last_ms = T0
        self.renew()

    def renew(self):
        """The carry heartbeat, at simulated time."""
        risk_state.write(settings.carry_risk_state_file(), KEY, "NORMAL", "hb",
                         risk_state.SOURCE_RENEW, ts_ms=self.clock_ms,
                         profile=risk_state.CARRY_PROFILE)

    def snapshot(self, client, cfg, now_ms=None, private=True):
        raw = self.regime.snapshot_fn()(client, cfg, now_ms=now_ms, private=private)
        for e in self.venue.accrue(raw, 0.0173):
            if e["type"] == "funding":
                self.client.settlements.append({"symbol": e["symbol"], "change": e["amount"],
                                                "transactionTime": e["ts_ms"]})
        return self.venue.overlay(raw, 0.0173, "1")

    def now(self):
        return self.clock_ms

    def sleep(self, s):
        self.clock_ms += int(s * 1000)

    def runner(self, **kw):
        args = dict(env={"HERMES_RISK_HMAC_KEY": KEY}, client=self.client, exchange=self.venue,
                    snapshot_fn=self.snapshot, now_ms=self.now, sleep=self.sleep,
                    heartbeat=self.renew, timers_active=lambda: [], notifier=None)
        args.update(kw)
        return rt.Roundtrip(cc.load(self.cfg_file, testnet=False), **args)


@pytest.fixture
def sim(isolated_paths, tmp_path, monkeypatch):
    return Sim(tmp_path, monkeypatch)


def _book():
    return book_store.load(settings.carry_book_file(), KEY)[0].get("ETHUSDT", SymbolBook())


def test_one_full_round_trip_then_it_stops_by_itself(sim):
    yaml_before = _sha(CARRY_YAML)
    run = sim.runner()
    assert run.checks() == []
    rc = run.run()
    assert rc == 0, run.report
    phases = [p for p, _ in run.timeline]
    assert phases == ["enter", "hold", "exit", "return", "done"]
    # redeem -> entry at the minimum size -> >= 1 settlement -> exit -> Earn
    rows = ledger.read(sim.log_dir)
    orders = [r for r in rows if r["type"] == "order" and float(r.get("filled") or 0) > 0]
    entry_perp = [o for o in orders if o["action"] == "ENTER" and o["leg"] == "perp"]
    assert len(entry_perp) == 1 and float(entry_perp[0]["filled"]) == pytest.approx(0.01)
    entry_cost = sum(float(o["filled"]) * float(o["avg_price"]) for o in orders
                     if o["action"] == "ENTER" and o["leg"] == "spot")
    assert entry_cost <= rt.MAX_NOTIONAL_USD
    funding = [r for r in rows if r["type"] == "funding"]
    assert funding and funding[0]["ts_ms"] > run.entered_ms
    assert any(r["type"] == "round_trip" for r in rows)
    assert _book().status == FLAT and sim.venue.s.shorts.get("ETHUSDT", 0) == 0
    assert sim.venue.s.coins.get("ETH", 0) < 0.00062                     # spot sold (dust)
    assert sim.venue.s.earn_staked > 35 and sim.venue.s.usdt < 1         # back in Earn
    # one cycle only: it stopped, and it never entered twice
    assert len([o for o in orders if o["action"] == "ENTER"]) == 2       # perp + spot, once
    # it never wrote the risk state, never changed carry.yaml
    v = risk_state.verify(settings.carry_risk_state_file(), KEY, now_ms=sim.clock_ms,
                          profile=risk_state.CARRY_PROFILE)
    assert v.source != risk_state.SOURCE_OPERATOR
    assert _sha(CARRY_YAML) == yaml_before
    assert yaml.safe_load(CARRY_YAML.read_text())["DRY_RUN"] is True
    # every cycle went through the production runner, live, as a roundtrip
    recs = [json.loads(l) for f in sorted(sim.log_dir.glob("*.jsonl"))
            for l in f.read_text().splitlines()]
    assert recs and all(r["dry_run"] is False and r.get("roundtrip") is True for r in recs)
    report = json.loads(Path(run.report_file).read_text())
    assert report["result"] == "done" and report["entry"]["perp_qty"] == pytest.approx(0.01)


def test_the_exit_waits_for_a_settlement(sim):
    run = sim.runner()
    run.run()
    times = dict(run.timeline)
    settlement = run.settlement_ms
    assert times["hold"] < settlement <= times["exit"] - rt.AFTER_SETTLEMENT_MIN * MIN


def test_stop_signal_during_hold_exits_flat(sim):
    run = sim.runner()
    calls = {"n": 0}

    def hb():
        sim.renew()
        calls["n"] += 1
        if run.phase == "hold" and calls["n"] > 3:
            run.request_stop("SIGTERM")

    run.heartbeat = hb
    assert run.run() == 0
    assert [p for p, _ in run.timeline][-3:] == ["exit", "return", "done"]
    assert run.settlement_ms is None or dict(run.timeline)["exit"] < run.settlement_ms
    assert _book().status == FLAT and sim.venue.s.shorts.get("ETHUSDT", 0) == 0


def test_a_blocking_alert_while_open_goes_straight_to_the_exit(sim):
    run = sim.runner()

    def hb():
        sim.renew()
        if run.phase == "hold":
            risk.write_hold(settings.carry_hold_file(), "test: orphan protection", sim.clock_ms)

    run.heartbeat = hb
    assert run.run() == 0
    assert "CARRY_HOLD" in run.report["exit_reason"]
    assert _book().status == FLAT and sim.venue.s.shorts.get("ETHUSDT", 0) == 0


def test_hard_limit_refuses_a_minimum_position_above_it(isolated_paths, tmp_path, monkeypatch):
    s = Sim(tmp_path, monkeypatch, price=6000.0)        # 0.01 ETH ~ 60 USDT > 50 - buffer
    problems = s.runner().checks()
    assert any("minimum position" in p for p in problems)


def test_refuses_without_enough_usdt(isolated_paths, tmp_path, monkeypatch):
    s = Sim(tmp_path, monkeypatch, earn=10.0)
    assert any("not enough USDT" in p for p in s.runner().checks())


def test_refuses_while_a_carry_timer_is_active(sim):
    run = sim.runner(timers_active=lambda: ["yield-carry-cycle.timer"])
    assert any("yield-carry-cycle.timer" in p for p in run.checks())


def test_refuses_unless_flat(sim):
    book_store.save(settings.carry_book_file(), KEY,
                    {"ETHUSDT": SymbolBook(status=OPEN, perp_qty=0.01, spot_qty=0.01)}, T0)
    assert any("not FLAT" in p for p in sim.runner().checks())


def test_refuses_unless_the_carry_risk_state_is_normal(sim):
    risk_state.write(settings.carry_risk_state_file(), KEY, "NO_NEW_POSITIONS", "op",
                     risk_state.SOURCE_OPERATOR, ts_ms=T0, profile=risk_state.CARRY_PROFILE)
    assert any("risk state" in p for p in sim.runner().checks())


def test_refuses_a_hold(sim):
    risk.write_hold(settings.carry_hold_file(), "earlier orphan", T0)
    assert any("CARRY_HOLD" in p for p in sim.runner().checks())


def test_refuses_if_carry_yaml_is_not_dry_run(sim):
    cfg = yaml.safe_load(sim.cfg_file.read_text())
    cfg.update(DRY_RUN=False, DEADMAN_URL="https://hc-ping.com/x")
    sim.cfg_file.write_text(yaml.safe_dump(cfg))
    assert any("DRY_RUN" in p for p in sim.runner().checks())


# =========================================================================== #
# the command line                                                             #
# =========================================================================== #

def test_refuses_without_the_flag_and_touches_nothing(isolated_paths, monkeypatch, capsys):
    import carry.client
    monkeypatch.setattr(carry.client, "CarryClient",
                        lambda *a, **k: pytest.fail("no client without the flag"))
    assert rt.main([]) == 2
    assert "--i-approve-real-money" in capsys.readouterr().err


def test_refuses_on_testnet(isolated_paths, monkeypatch, capsys):
    monkeypatch.setenv("BYBIT_TESTNET", "1")
    assert rt.main(["--i-approve-real-money"]) == 2
    assert "mainnet" in capsys.readouterr().err


def test_main_records_raw_responses_with_the_mainnet_approval(isolated_paths, monkeypatch):
    import carry.client
    seen = {}

    class Client:
        testnet = False

        def __init__(self, api_key=None, api_secret=None, testnet=None, session=None):
            seen.update(key=api_key, session=session, testnet=testnet)

    monkeypatch.setattr(carry.client, "CarryClient", Client)
    monkeypatch.setattr(rt.Roundtrip, "checks", lambda self: ["stop here (test)"])
    monkeypatch.setenv("BYBIT_CARRY_API_KEY", "real-key")
    monkeypatch.setenv("BYBIT_CARRY_API_SECRET", "real-secret")
    assert rt.main(["--i-approve-real-money"]) == 2
    assert seen["key"] == "real-key" and seen["testnet"] is False
    assert isinstance(seen["session"], ResponseRecorder)
    assert seen["session"].out_dir == Path("/opt/hermes/logs/carry/raw")


def test_while_it_runs_the_regular_cycle_is_locked_out(isolated_paths, monkeypatch):
    called = []
    monkeypatch.setattr(rcc, "run_cycle", lambda *a, **k: called.append(1) or ({}, 0))
    with rt.cycle_lock():
        assert rcc.main() == 0
    assert called == []


def test_deploy_refuses_while_the_roundtrip_runs(sandbox):
    (sandbox.dir / "systemctl.is-active.yield-carry-roundtrip.service.out").write_text("active\n")
    r = sandbox.run()
    assert "FAIL step 1/7" in r.stdout and "round trip" in r.stdout
    assert sandbox.systemctl_mutations() == []
    assert not any("run_carry_cycle" in c or "install.sh" in c for c in sandbox.calls())


def test_deploy_keeps_a_finished_roundtrip_unit(sandbox):
    (sandbox.dir / "systemctl.is-active.yield-carry-roundtrip.service.out").write_text("inactive\n")
    sandbox.reply("systemctl.list-unit-files",
                  out="yield-cycle.timer enabled\nyield-carry-roundtrip.service transient\n")
    r = sandbox.run()
    assert r.returncode == 0, r.stdout
    assert not any("yield-carry-roundtrip" in " ".join(m) for m in sandbox.systemctl_mutations())


from test_deploy import sandbox  # noqa: E402,F401


# =========================================================================== #
# robustness with a position open                                              #
# =========================================================================== #

def test_a_read_error_in_the_script_is_retried_not_fatal(sim):
    """The script's own sizing read fails twice: retried, then the round trip
    completes as usual."""
    run = sim.runner()
    real = sim.snapshot
    fails = {"n": 0}

    def flaky(client, cfg, now_ms=None, private=True):
        if run.phase == "enter" and run.cycles == 0 and fails["n"] < 2:
            fails["n"] += 1
            raise RuntimeError("connection reset")
        return real(client, cfg, now_ms=now_ms, private=private)

    run.snapshot_fn = flaky
    assert run.run() == 0
    assert fails["n"] == 2
    assert [p for p, _ in run.timeline] == ["enter", "hold", "exit", "return", "done"]
    assert _book().status == FLAT and sim.venue.s.shorts.get("ETHUSDT", 0) == 0


def test_a_crash_inside_the_cycle_while_holding_goes_to_the_exit(sim):
    """CYCLE_CRASH is a blocking code: with a position open, exit at once."""
    run = sim.runner()
    real = sim.snapshot
    done = {"n": 0}

    def crash_once(client, cfg, now_ms=None, private=True):
        if run.phase == "hold" and done["n"] == 0:
            done["n"] = 1
            raise RuntimeError("connection reset")
        return real(client, cfg, now_ms=now_ms, private=private)

    run.snapshot_fn = crash_once
    assert run.run() == 0
    assert "CYCLE_CRASH" in run.report["exit_reason"]
    assert _book().status == FLAT and sim.venue.s.shorts.get("ETHUSDT", 0) == 0


def test_persistent_errors_while_holding_go_to_the_exit(sim, monkeypatch):
    run = sim.runner()
    real_cycle = rcc.run_cycle

    def broken(*a, **k):
        if run.phase == "hold":
            raise RuntimeError("API down")
        return real_cycle(*a, **k)

    monkeypatch.setattr(rcc, "run_cycle", broken)
    assert run.run() == 0
    assert "loop errors" in run.report["exit_reason"]
    assert _book().status == FLAT and sim.venue.s.shorts.get("ETHUSDT", 0) == 0


def test_a_stop_is_seen_within_seconds_not_a_whole_cycle(sim):
    run = sim.runner()
    waits = []
    base_sleep = sim.sleep

    def sleep(s):
        waits.append(s)
        base_sleep(s)

    run._sleep = sleep
    hb_calls = {"n": 0}

    def hb():
        sim.renew()
        hb_calls["n"] += 1
        if run.phase == "hold" and hb_calls["n"] == 4:
            run.request_stop("SIGTERM")

    run.heartbeat = hb
    run.run()
    assert max(waits) <= rt.WAIT_STEP_S


def test_refuses_when_eth_is_not_active_collateral(sim, monkeypatch):
    from carry.snapshot import Collateral
    from dataclasses import replace
    from types import MappingProxyType
    real = sim.snapshot

    def no_coll(client, cfg, now_ms=None, private=True):
        s = real(client, cfg, now_ms=now_ms, private=private)
        acct = replace(s.account, collateral=MappingProxyType(
            {"ETH": Collateral(0.95, True, False)}))
        return replace(s, account=acct)

    assert any("collateral" in p for p in sim.runner(snapshot_fn=no_coll).checks())


def test_no_entry_ends_flat_with_its_own_exit_code(sim):
    """E.g. the basis never allows the entry: FLAT after the deadline, rc 5."""
    from dataclasses import replace
    real = sim.snapshot

    def bad_basis(client, cfg, now_ms=None, private=True):
        s = real(client, cfg, now_ms=now_ms, private=private)
        m = s.markets["ETHUSDT"]
        return replace(s, markets={"ETHUSDT": replace(m, perp_bid=m.spot_bid * 0.99,
                                                      perp_ask=m.spot_ask * 0.99)})

    run = sim.runner(snapshot_fn=bad_basis)
    assert run.run() == 5
    assert run.report["result"] == "no entry (FLAT)"
    assert any("basis" in w for w in run.entry.get("last_no_entry", ()))
    assert sim.venue.s.shorts.get("ETHUSDT", 0) == 0


# =========================================================================== #
# the second supervised round trip (after the audit of 18/10)                  #
# =========================================================================== #

def test_the_second_round_trip_takes_the_first_ones_dust_and_verifies(sim):
    """0.00000998 ETH left by the first round trip: into the book as dust
    before the entry, bought that much less, sold with the rest; the report
    says the position was read in every cycle and the ledger's net is the
    wallet's change (but for the Earn interest the paper account pays)."""
    sim.venue.s.coins["ETH"] = 0.00000998
    run = sim.runner()
    assert run.checks() == []
    assert run.run() == 0, run.report
    v = run.report["verify"]
    assert v["dust_adopted"] == 0.00000998 and v["dust_before"] == 0.00000998
    assert v["position_unreadable_cycles"] == 0
    assert v["earn"]["apr"] == pytest.approx(0.0173)
    rows = ledger.read(sim.log_dir)
    buy = [r for r in rows if r["type"] == "order" and r["leg"] == "spot" and r["side"] == "Buy"]
    assert len(buy) == 1 and float(buy[0]["qty"]) == pytest.approx(0.01001)     # not 0.01002
    assert _book().dust_qty == v["dust_after"] == pytest.approx(sim.venue.s.coins["ETH"])
    assert v["dust_after"] < 0.00001
    assert abs(v["ledger_minus_wallet"]) < 0.002, v


@pytest.mark.parametrize("eth,book", [(0.0007, SymbolBook()),                   # not dust
                                      (0.00000998, SymbolBook(dust_qty=0.00000998)),
                                      (0.0, SymbolBook())])
def test_dust_is_adopted_only_under_the_minimum_into_a_book_without_dust(sim, eth, book):
    sim.venue.s.coins["ETH"] = eth
    book_store.save(settings.carry_book_file(), KEY, {"ETHUSDT": book}, T0)
    assert sim.runner().adopt_dust() is None
    assert _book() == book
