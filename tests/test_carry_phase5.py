"""CARRY_PLAN Phase 5 — risk, ledger, paper, the cycle; replay of synthetic
bullish regimes (decision 13.6). Written before the implementation; no
network: the market is synthetic, the account is the paper one.

Acceptance (§7): ADL and liquidation in the simulations -> the orphan leg is
closed in the same cycle.
"""

import json
from pathlib import Path

import pytest

import risk_state
import run_carry_cycle as rcc
import settings
from carry import adopt, book as book_store, ledger, risk
from carry.paper import PaperExchange, PaperState
from carry.plan import SymbolBook
from carry_builders import NOW, cfg as base_cfg, market, snapshot
from carry_regimes import E8, H, FakeClient, Regime
from helpers import KEY

# --- book ------------------------------------------------------------------------------- #

def test_book_round_trip_and_tamper(tmp_path):
    path = tmp_path / "book.json"
    assert book_store.load(path, KEY) == ({}, None)                     # first start
    b = {"ETHUSDT": SymbolBook(status="OPEN", perp_qty=0.03, spot_qty=0.03004,
                               entry_times=(1, 2), pending_spot=("Buy:cy-x",))}
    book_store.save(path, KEY, b, NOW)
    assert book_store.load(path, KEY) == (b, None)
    data = json.loads(path.read_text())
    data["books"]["ETHUSDT"]["spot_qty"] = 5.0                           # edited by hand
    path.write_text(json.dumps(data))
    loaded, alert = book_store.load(path, KEY)
    assert loaded == {} and alert.startswith("CRITICAL: BOOK_UNREADABLE")
    path.write_text("{")
    assert book_store.load(path, KEY)[0] == {}


# --- paper -------------------------------------------------------------------------------- #

def paper_with(usdt=100.0, earn=0.0, **kw):
    p = PaperExchange(PaperState(usdt=usdt, earn_staked=earn, **kw), base_cfg())
    p.overlay(snapshot(), apr=0.0173)
    return p


def test_paper_fills_at_the_top_of_the_book_with_the_spot_fee_in_the_coin():
    from carry.client import order_request
    p = paper_with()
    m = snapshot().markets["ETHUSDT"]
    p.create_order(order_request("linear", "ETHUSDT", "Sell", "Limit", 0.03, 0.01, "cy-p",
                                 price=m.perp_bid, tick=0.01))
    p.create_order(order_request("spot", "ETHUSDT", "Buy", "Limit", 0.03004, 0.00001, "cy-s",
                                 price=m.spot_ask, tick=0.01))
    assert p.s.shorts["ETHUSDT"] == pytest.approx(0.03)
    assert p.s.coins["ETH"] == pytest.approx(0.03004 * 0.999)           # R22
    perp_fee = 0.03 * m.perp_bid * 0.00055
    assert p.s.usdt == pytest.approx(100 - 0.03004 * m.spot_ask - perp_fee)
    o = p.find_order("spot", "cy-s")
    assert o["orderStatus"] == "Filled" and "ETH" in o["cumFeeDetail"]


def test_paper_overlay_shows_the_paper_account_to_the_plan():
    p = paper_with(usdt=16.0, earn=50.0, coins={"ETH": 0.03}, shorts={"ETHUSDT": 0.03},
                   short_avg={"ETHUSDT": 2500.0})
    s = p.overlay(snapshot(), apr=0.0173)
    assert s.positions["ETHUSDT"].side == "Sell" and s.positions["ETHUSDT"].size == 0.03
    assert s.account.balance("ETH").wallet == 0.03 and s.account.balance("USDT").wallet == 16.0
    assert s.earn.staked == 50.0 and s.earn.apr == 0.0173
    assert "account" not in s.errors and s.missing_for_entry("ETHUSDT") == []


def test_paper_accrues_funding_of_settlements_passed_and_earn_interest():
    m = market(settled=[0.0003] * 12, now=NOW)
    p = paper_with(usdt=0.0, earn=100.0, shorts={"ETHUSDT": 0.03})
    p.s.last_ms = NOW - 9 * H                                          # one settlement passed
    ev = p.accrue(snapshot(markets={"ETHUSDT": m}), apr=0.0173)
    fund = [e for e in ev if e["type"] == "funding"]
    assert len(fund) == 1 and fund[0]["amount"] == pytest.approx(0.03 * m.mark_price * 0.0003)
    assert p.s.usdt == pytest.approx(fund[0]["amount"])
    interest = [e for e in ev if e["type"] == "earn_interest"][0]["amount"]
    assert interest == pytest.approx(100 * 0.0173 * 9 * H / (365 * 24 * H))


def test_paper_redeem_and_stake_settle_at_once():
    from bybit_earn_tool import place_order_request
    p = paper_with(usdt=0.0, earn=100.0)
    p.place_earn(place_order_request("Redeem", "UNIFIED", "USDT", "1", "90.00", "cy-r"))
    assert (p.s.usdt, p.s.earn_staked) == (90.0, 10.0)
    assert p.get_earn_orders("cy-r")[0]["status"] == "Success"
    p.place_earn(place_order_request("Redeem", "UNIFIED", "USDT", "1", "50.00", "cy-r2"))
    assert p.get_earn_orders("cy-r2")[0]["status"] == "Fail"


# --- ledger and R36 -------------------------------------------------------------------------- #

def test_ledger_summary_and_round_trip(tmp_path):
    rows = [{"type": "order", "symbol": "ETHUSDT", "action": "ENTER", "leg": "perp", "side": "Sell",
             "filled": 0.03, "avg_price": 2500.0, "fee": 0.04, "fee_detail": None},
            {"type": "order", "symbol": "ETHUSDT", "action": "ENTER", "leg": "spot", "side": "Buy",
             "filled": 0.03004, "avg_price": 2500.2, "fee": 0.00003, "fee_detail": {"ETH": "3e-05"}},
            {"type": "funding", "amount": 0.5}, {"type": "earn_interest", "amount": 0.1}]
    ledger.write(tmp_path, rows, NOW, "c1")
    back = ledger.read(tmp_path)
    assert len(back) == 4 and all(r["cycle_id"] == "c1" for r in back)
    s = ledger.summary(back)
    assert s["funding"] == 0.5 and s["orders"] == 2
    assert s["fees"] == pytest.approx(0.04 + 0.00003 * 2500.2)          # coin fee valued in USDT
    rt = ledger.round_trip("ETHUSDT", rows + [
        {"type": "order", "symbol": "ETHUSDT", "action": "SELL", "leg": "spot", "side": "Sell",
         "filled": 0.03, "avg_price": 2600.0, "fee": 0.07},
        {"type": "order", "symbol": "ETHUSDT", "action": "CLOSE", "leg": "perp", "side": "Buy",
         "filled": 0.03, "avg_price": 2600.5, "fee": 0.04}], NOW)
    assert rt["pnl"] == pytest.approx(0.03 * 2500 - 0.03004 * 2500.2 + 0.03 * 2600 - 0.03 * 2600.5)


def test_underperformance_needs_a_full_window_then_compares_with_expected():
    day = 86_400_000
    exp = [{"type": "expected_funding", "ts_ms": NOW - d * day, "amount": 1.0} for d in range(15)]
    good = [{"type": "funding", "ts_ms": NOW - d * day, "amount": 0.9} for d in range(15)]
    bad = [{"type": "funding", "ts_ms": NOW - d * day, "amount": 0.3} for d in range(15)]
    c = base_cfg()
    assert risk.underperformance(exp + good, c, NOW) is None
    assert risk.underperformance(exp + bad, c, NOW).startswith("UNDERPERFORMANCE")
    assert risk.underperformance(exp[:5] + bad[:5], c, NOW) is None     # 5 days only


def test_deadman_ping():
    calls = []

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def ok(url, timeout):
        calls.append(url)
        return R()

    def down(url, timeout):
        raise OSError("unreachable")
    assert risk.deadman_ping("https://hc-ping.com/x", opener=ok) is None and calls
    assert risk.deadman_ping("https://hc-ping.com/x", opener=down).startswith("DEADMAN_PING_FAILED")
    assert risk.deadman_ping(None, opener=down) is None


# --- the cycle on synthetic regimes ------------------------------------------------------- #

class Pinger:
    def __init__(self):
        self.n = 0

    def __call__(self, url, timeout):
        self.n += 1
        outer = self

        class R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def carry_env(isolated_paths, tmp_path):
    log_dir = tmp_path / "carry_logs"
    c = base_cfg(LOG_DIR=str(log_dir))
    return {"cfg": c, "env": {"HERMES_RISK_HMAC_KEY": KEY}, "log_dir": log_dir}


def renew(now, state="NORMAL", source=risk_state.SOURCE_RENEW):
    risk_state.write(settings.carry_risk_state_file(), KEY, state, "test", source, ts_ms=now,
                     profile=risk_state.CARRY_PROFILE)


def cycle(env, regime, now, **kw):
    renew(now)
    clock = Clock()
    return rcc.run_cycle(env["cfg"], env["env"], FakeClient(), now_ms=now,
                         snapshot_fn=regime.snapshot_fn(), clock=clock, sleep=clock.sleep,
                         paper_start=(0.0, 100.0), opener=Pinger(), **kw)


def run_all(env, regime, every_h=2.0, hook=None):
    recs = []
    for t in regime.cycle_times(every_h):
        if hook:
            hook(t)
        rec, rc = cycle(env, regime, t)
        assert rc == 0, rec["alerts"]
        recs.append(rec)
    return recs


def kinds_of(rec):
    return [a["kind"] for a in rec["actions"]]


BULL = [(5, 0.00001), (25, 0.0003), (15, -0.0005)]


def test_bullish_regime_enters_holds_collects_funding_and_exits(carry_env):
    regime = Regime(BULL)
    recs = run_all(carry_env, regime)
    bull_start = regime.start + 5 * 24 * H
    neg_start = regime.start + 30 * 24 * H
    redeem = [r for r in recs if "EARN_REDEEM_FOR_ENTRY" in kinds_of(r)]
    enter = [r for r in recs if "ENTER" in kinds_of(r)]
    exits = [r for r in recs if "EXIT" in kinds_of(r)]
    assert not [r for r in recs if r["cycle_ms"] < bull_start and kinds_of(r)]  # nothing neutral
    assert redeem and enter and len(enter) == 1                       # redeem, then the legs
    assert bull_start <= redeem[0]["cycle_ms"] < enter[0]["cycle_ms"] < bull_start + 3 * 24 * H
    assert len(exits) == 1 and neg_start < exits[0]["cycle_ms"] < neg_start + 4 * 24 * H
    rows = ledger.read(carry_env["log_dir"])
    s = ledger.summary(rows)
    assert s["funding"] > 0 and s["fees"] > 0
    assert s["funding"] == pytest.approx(s["expected_funding"], rel=1e-9)  # paper = expected
    assert [r for r in rows if r["type"] == "round_trip"]
    last = recs[-1]
    assert last["book"]["ETHUSDT"]["status"] == "FLAT"
    assert last["paper"]["shorts"]["ETHUSDT"] == 0
    assert last["paper"]["coins"]["ETH"] < 0.00062                    # dust at most
    assert last["paper"]["earn_staked"] > 99                           # USDT back to Earn
    bad = [a for r in recs for a in r["alerts"]
           if a.startswith(("ORPHAN_LEG", "CRITICAL", "CYCLE_CRASH", "BOOK_MISMATCH"))]
    assert bad == []


def test_neutral_regime_never_trades(carry_env):
    recs = run_all(carry_env, Regime([(20, 0.00001)]), every_h=4.0)
    assert not [r for r in recs if kinds_of(r)]


def _entered(carry_env, regime):
    """Run until the position is open; return the paper file path."""
    for t in regime.cycle_times(2.0):
        rec, _ = cycle(carry_env, regime, t)
        if rec["book"].get("ETHUSDT", {}).get("status") == "OPEN":
            return t
    raise AssertionError("never entered")


def _paper():
    return PaperExchange.load(settings.carry_paper_file(), base_cfg())


def _patch_paper(fn):
    p = _paper()
    fn(p)
    p.save(settings.carry_paper_file())


def test_adl_sells_the_excess_spot_in_the_same_cycle(carry_env):
    regime = Regime([(30, 0.0003)])
    t = _entered(carry_env, regime)
    _patch_paper(lambda p: p.adl("ETHUSDT", 0.01))
    rec, rc = cycle(carry_env, regime, t + 2 * H)
    assert any(a.startswith("ADL_DETECTED") for a in rec["alerts"])
    assert "REBALANCE_TOWARD_NEUTRAL" in kinds_of(rec)
    p = _paper()
    assert p.s.shorts["ETHUSDT"] == pytest.approx(0.02)
    assert p.s.coins["ETH"] - 0.02 < 0.01                              # hedged again, same cycle
    assert rec["book"]["ETHUSDT"]["spot_qty"] == pytest.approx(p.s.coins["ETH"])


def test_liquidation_sells_the_orphan_spot_in_the_same_cycle(carry_env):
    regime = Regime([(30, 0.0003)])
    t = _entered(carry_env, regime)
    _patch_paper(lambda p: p.liquidate("ETHUSDT"))
    rec, rc = cycle(carry_env, regime, t + 2 * H)
    assert any(a.startswith("ORPHAN_LEG") for a in rec["alerts"])
    p = _paper()
    assert p.s.shorts["ETHUSDT"] == 0 and p.s.coins["ETH"] < 0.00062
    assert rec["book"]["ETHUSDT"]["status"] == "FLAT"


def test_lost_book_holds_until_adopt_and_operator_release(carry_env):
    regime = Regime([(40, 0.0003)])
    t = _entered(carry_env, regime)
    settings.carry_book_file().unlink()                                # the book is lost
    rec, _ = cycle(carry_env, regime, t + 2 * H)
    assert any(a.startswith("BOOK_MISMATCH") for a in rec["alerts"]) and rec["orders"] == []
    assert risk.read_hold(settings.carry_hold_file())
    p = _paper()
    assert p.s.shorts["ETHUSDT"] == pytest.approx(0.03)                # nothing touched
    # the operator adopts: the book is rebuilt without a trade
    adopt.write_request(settings.carry_adopt_file(), KEY, "telegram chat 42", t + 4 * H - 600_000)
    rec, _ = cycle(carry_env, regime, t + 4 * H)
    assert rec.get("adopted") == ["ETHUSDT"] and rec["orders"] == []
    assert rec["book"]["ETHUSDT"]["status"] == "OPEN"
    assert not settings.carry_adopt_file().exists()                    # single use
    assert any(a.startswith("CARRY_HOLD") for a in rec["alerts"])      # still held
    assert rec["risk_state"] == "NO_NEW_POSITIONS"
    # the operator writes the carry risk state: the hold is released
    risk_state.write(settings.carry_risk_state_file(), KEY, "NORMAL", "ack",
                     risk_state.SOURCE_OPERATOR, ts_ms=t + 5 * H,
                     profile=risk_state.CARRY_PROFILE)
    clock = Clock()
    rec, _ = rcc.run_cycle(carry_env["cfg"], carry_env["env"], FakeClient(), now_ms=t + 6 * H,
                           snapshot_fn=regime.snapshot_fn(), clock=clock, sleep=clock.sleep,
                           opener=Pinger())
    assert any(a.startswith("HOLD_RELEASED") for a in rec["alerts"])
    assert not risk.read_hold(settings.carry_hold_file())


def test_hold_is_not_released_by_the_heartbeat(carry_env):
    regime = Regime([(40, 0.0003)])
    risk.write_hold(settings.carry_hold_file(), "test", regime.start)
    rec, _ = cycle(carry_env, regime, regime.start + 30 * 60_000)       # renew = heartbeat source
    assert any(a.startswith("CARRY_HOLD") for a in rec["alerts"])
    assert rec["risk_state"] == "NO_NEW_POSITIONS"


def test_cycle_record_is_written_for_the_heartbeat_and_pings_the_deadman(carry_env):
    import heartbeat
    regime = Regime([(2, 0.00001)])
    pinger = Pinger()
    renew(regime.start + H)
    clock = Clock()
    rec, rc = rcc.run_cycle(carry_env["cfg"], carry_env["env"], FakeClient(),
                            now_ms=regime.start + H, snapshot_fn=regime.snapshot_fn(), clock=clock,
                            sleep=clock.sleep, opener=pinger)
    assert rc == 0 and pinger.n == 1
    last = heartbeat.last_cycle_record(carry_env["log_dir"])
    assert last["cycle_id"] == rec["cycle_id"] and last["system"] == "carry"
    ok, code = heartbeat.check_last_cycle_ok(carry_env["log_dir"], heartbeat.CARRY_BLOCKING_CODES)
    assert ok, code


def test_bad_config_or_missing_key_writes_config_incomplete(carry_env):
    regime = Regime([(2, 0.00001)])
    pinger = Pinger()
    rec, rc = rcc.run_cycle(carry_env["cfg"], {}, FakeClient(), now_ms=regime.start + H,
                            snapshot_fn=regime.snapshot_fn(), opener=pinger)
    assert rc == 3 and rec["alerts"][0].startswith("CONFIG_INCOMPLETE") and pinger.n == 0
    bad = dict(carry_env["cfg"], MAX_SPREAD_BPS=None)
    rec, rc = rcc.run_cycle(bad, carry_env["env"], FakeClient(), now_ms=regime.start + H,
                            snapshot_fn=regime.snapshot_fn(), opener=pinger)
    assert rc == 3


def test_crash_is_recorded_and_blocks(carry_env):
    regime = Regime([(2, 0.00001)])

    def boom(*a, **k):
        raise RuntimeError("synthetic")
    renew(regime.start + H)
    rec, rc = rcc.run_cycle(carry_env["cfg"], carry_env["env"], FakeClient(),
                            now_ms=regime.start + H, snapshot_fn=boom, opener=Pinger())
    assert rc == 4 and any(a.startswith("CYCLE_CRASH") for a in rec["alerts"])


def test_dry_run_never_touches_a_live_exchange(carry_env):
    """DRY_RUN always trades the paper account, whatever exchange is passed."""
    class Live:
        live = True

        def __getattr__(self, name):
            raise AssertionError(f"live exchange used: {name}")
    regime = Regime([(10, 0.0003)])
    for t in regime.cycle_times(4.0)[:20]:
        rec, rc = cycle(carry_env, regime, t, exchange=Live())
        assert rc == 0, rec["alerts"]


def test_bullish_regime_with_a_rising_price_stays_delta_neutral(carry_env):
    """ETH +20 % during the bull phase: the hedge makes the legs' PnL small
    against the notional (~75 USD) — the carry is the funding, not the price."""
    regime0 = Regime(BULL)
    start, span = regime0.start, 45 * 24 * H
    regime = Regime(BULL, price=lambda t: 2500.0 * (1 + 0.2 * min(1.0, max(0.0, (t - start) / span))))
    run_all(carry_env, regime)
    rows = ledger.read(carry_env["log_dir"])
    [rt] = [r for r in rows if r["type"] == "round_trip"]
    assert abs(rt["pnl"]) < 0.5                                         # < 0.7 % of notional
    s = ledger.summary(rows)
    assert s["net"] > 0


def test_choppy_funding_does_not_churn(carry_env):
    """Funding that flips daily: hysteresis, MIN_HOLD and the round-trip cap
    keep the position count small (MAX_ROUND_TRIPS_PER_30D = 2)."""
    phases = [(1, 0.0004 if i % 2 == 0 else -0.0003) for i in range(40)]
    recs = run_all(carry_env, Regime(phases), every_h=2.0)
    enters = [r["cycle_ms"] for r in recs if "ENTER" in kinds_of(r)]
    for t in enters:                                                    # any 30-day window
        assert len([u for u in enters if t <= u < t + 30 * 24 * H]) <= 2
    assert not [a for r in recs for a in r["alerts"] if a.startswith(("ORPHAN_LEG", "CRITICAL"))]



def test_rising_price_between_redeem_and_entry_never_loops_earn(carry_env):
    """Regression (found by the replay): the slack was required at the entry
    too, so a price rise between the redeem and the entry dropped the entry
    and returned the USDT to Earn, forever. The entry now happens."""
    regime0 = Regime(BULL)
    start = regime0.start
    regime = Regime(BULL, price=lambda t: 2500.0 * (1 + 0.004 * (t - start) / (24 * H)))
    recs = run_all(carry_env, regime)
    redeems = [r for r in recs if "EARN_REDEEM_FOR_ENTRY" in kinds_of(r)]
    assert len(redeems) <= 2 and [r for r in recs if "ENTER" in kinds_of(r)]

