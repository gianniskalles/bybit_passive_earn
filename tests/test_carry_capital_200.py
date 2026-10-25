"""Decision 24 (25/10): the capital goes to ~200 USDT, BTCUSDT joins ETHUSDT.

1. The shipped config: SYMBOLS [ETHUSDT, BTCUSDT], TOTAL_CAPITAL_CAP_USD 200,
   a limit per symbol (MAX_NOTIONAL_BY_SYMBOL: BTC 130, ETH 50), USDT_BUFFER_USD
   20 (10 %): 130 + 50 + 20 = 200. The BTC minimum (perp 0.001, spot 0.001002
   after the 0.1 % fee) is ~85.5 USDT at the real 30/9 price (85,310.8) and
   fits 130 up to a spot price of ~129,700 (+52 %); the ETH minimum (perp
   0.01, spot 0.01002) fits 50 up to ~4,990 (+106 % on 17/10's 2,417). (A
   single limit of 90 left BTC 5 % of room: refused by Giannis, 25/10.)
2. New USDT in the UTA (a manual transfer from Funding) goes to Earn by
   itself (EARN_RETURN), the buffer kept while a position is open; the book
   and the ledger's PnL are untouched.
3. BTC must be active collateral: `preflight carry-symbols` and the live
   switch's checks (carry/live_ops.py) refuse otherwise.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

import run_carry_cycle as rcc
import settings
from carry import book as book_store
from carry import config as cc
from carry import ledger, live, live_ops
from carry import preflight as pf
from carry.plan import SymbolBook, plan_cycle
from carry.snapshot import Collateral
from carry_builders import NOW, account, cfg as base_cfg, earn, market, short, snapshot
from helpers import KEY

REPO = Path(__file__).resolve().parent.parent
SHIPPED = yaml.safe_load((REPO / "config" / "carry.yaml").read_text())
BTC_30_9 = 85_310.8                       # the real spot ask, calibration/carry_market_*.json
ETH_17_10 = 2_416.96                      # the real spot fill of the round trip


def both(btc=BTC_30_9, eth=ETH_17_10, rate=0.0003):
    return {"ETHUSDT": market("ETHUSDT", price=eth, rate=rate),
            "BTCUSDT": market("BTCUSDT", price=btc, rate=rate)}


def acct(usdt=0.0, coins=None, btc_collateral=True):
    a = account(usdt=usdt, coins=coins)
    coll = dict(a.collateral)
    coll["BTC"] = Collateral(0.95, True, btc_collateral)
    from dataclasses import replace
    from types import MappingProxyType
    return replace(a, collateral=MappingProxyType(coll))


# --- 1. the shipped config ------------------------------------------------------------- #

def test_the_shipped_config_is_decision_24():
    assert SHIPPED["SYMBOLS"] == ["ETHUSDT", "BTCUSDT"]
    assert (SHIPPED["TOTAL_CAPITAL_CAP_USD"], SHIPPED["USDT_BUFFER_USD"]) == (200, 20)
    assert SHIPPED["MAX_NOTIONAL_BY_SYMBOL"] == {"BTCUSDT": 130, "ETHUSDT": 50}
    assert (cc.notional_cap(SHIPPED, "BTCUSDT"), cc.notional_cap(SHIPPED, "ETHUSDT")) == (130, 50)
    assert SHIPPED["USDT_BUFFER_USD"] >= 0.10 * SHIPPED["TOTAL_CAPITAL_CAP_USD"]
    assert 130 + 50 + 20 == 200
    assert cc.validate(SHIPPED, testnet=False,
                       alt_report=cc.read_alt_report(cc.ALT_REPORT_FILE)) == []
    assert SHIPPED["DRY_RUN"] is True and SHIPPED["DEADMAN_URL"] is None


@pytest.mark.parametrize("sym,perp,spot,limit,top,now", [
    ("BTCUSDT", 0.001, 0.001002, 130, 129_740, BTC_30_9),
    ("ETHUSDT", 0.01, 0.01002, 50, 4_990, ETH_17_10),
])
def test_each_minimum_fits_its_limit_with_room(sym, perp, spot, limit, top, now):
    m = both()[sym]
    mp = pf.min_position(m, spot_fee=0.001)
    assert (mp.perp_qty, mp.spot_buy_qty) == (perp, spot)
    assert mp.notional_usd <= cc.notional_cap(SHIPPED, sym) == limit
    assert limit / mp.spot_buy_qty == pytest.approx(top, abs=1)    # where it stops fitting
    assert top / now > 1.5                                        # >= 50 % of room


def test_both_minimums_and_the_buffer_fit_the_cap():
    snap = snapshot(markets=both(), acct=acct(usdt=200.0))
    fit, alerts = pf.tradable_symbols(snap, SHIPPED)
    assert fit == ["ETHUSDT", "BTCUSDT"] and alerts == []
    total = sum(pf.min_position(m, 0.001).notional_usd for m in snap.markets.values())
    assert total + SHIPPED["USDT_BUFFER_USD"] <= SHIPPED["TOTAL_CAPITAL_CAP_USD"]


def test_above_its_price_a_symbol_is_left_out_with_an_alert():
    snap = snapshot(markets=both(btc=90_000.0), acct=acct(usdt=200.0))
    assert pf.tradable_symbols(snap, SHIPPED) == (["ETHUSDT", "BTCUSDT"], [])   # 90k fits now
    snap = snapshot(markets=both(btc=130_000.0, eth=5_000.0), acct=acct(usdt=200.0))
    fit, alerts = pf.tradable_symbols(snap, SHIPPED)
    assert fit == [] and [a.split(":")[1].split()[0] for a in alerts] == ["ETHUSDT", "BTCUSDT"]


def test_both_enter_within_the_cap():
    """Funding high on both, 200 USDT in the UTA: two entries, each within
    90, together with the buffer within 200."""
    snap = snapshot(markets=both(), positions={"ETHUSDT": short(), "BTCUSDT": short("BTCUSDT")},
                    acct=acct(usdt=200.0), earn_=earn(staked=0.0))
    plan = plan_cycle(snap, SHIPPED, "NORMAL", {}, "c")
    enters = [a for a in plan.actions if a.kind == "ENTER"]
    assert [a.symbol for a in enters] == ["ETHUSDT", "BTCUSDT"], (plan.decisions, plan.no_entry)
    eth, btc = enters
    assert eth.usdt <= 50 and btc.usdt <= 130
    assert (eth.perp_qty, btc.perp_qty) == (0.02, 0.001)       # 0.03 ETH > 50; 0.002 BTC > 130
    assert eth.usdt + btc.usdt + 20 <= 200


# --- 2. new USDT in the UTA ----------------------------------------------------------- #

OPEN_ETH = SymbolBook(status="OPEN", entered_ms=NOW - 400 * 3_600_000, perp_qty=0.03,
                      spot_qty=0.03, entry_times=(NOW - 400 * 3_600_000,))


def test_a_deposit_while_flat_goes_to_earn():
    snap = snapshot(markets=both(rate=0.00001), acct=acct(usdt=100.0),
                    positions={"ETHUSDT": short(), "BTCUSDT": short("BTCUSDT")},
                    earn_=earn(staked=57.0))
    plan = plan_cycle(snap, SHIPPED, "NORMAL", {}, "c")
    assert [(a.kind, a.usdt) for a in plan.actions] == [("EARN_RETURN", 100.0)]
    # the book only notes the collateral ratio it saw (R16), as every FLAT cycle
    assert {s: (b.status, b.perp_qty, b.spot_qty, b.dust_qty)
            for s, b in plan.book_updates.items()} == {s: ("FLAT", 0.0, 0.0, 0.0)
                                                        for s in plan.book_updates}
    assert plan.alerts == ()


def test_a_deposit_while_open_goes_to_earn_above_the_buffer():
    """ETH open, 15 USDT left in the UTA + 100 transferred: 95 go to Earn,
    the 20 USDT buffer stays; the book is not touched."""
    snap = snapshot(markets=both(rate=0.0003), acct=acct(usdt=115.0, coins={"ETH": 0.03}),
                    positions={"ETHUSDT": short(size=0.03), "BTCUSDT": short("BTCUSDT")},
                    earn_=earn(staked=57.0))
    plan = plan_cycle(snap, dict(SHIPPED, SYMBOLS=["ETHUSDT"]), "NO_NEW_POSITIONS",
                      {"ETHUSDT": OPEN_ETH}, "c")
    assert [(a.kind, a.usdt) for a in plan.actions] == [("EARN_RETURN", 95.0)]
    assert "ETHUSDT" not in plan.book_updates
    assert not any(a.startswith(("FOREIGN", "BOOK", "CRITICAL")) for a in plan.alerts)


def test_a_deposit_waits_while_an_entry_waits_for_its_redeem():
    from carry_builders import redeem_order
    sb = SymbolBook(status="REDEEMING", redeem_link="cy-r", redeem_started_ms=NOW - 60_000,
                    redeem_amount=90.0)
    snap = snapshot(markets=both(), acct=acct(usdt=100.0),
                    positions={"ETHUSDT": short(), "BTCUSDT": short("BTCUSDT")},
                    earn_=earn(staked=57.0, orders=(redeem_order("cy-r"),)))
    plan = plan_cycle(snap, SHIPPED, "NORMAL", {"ETHUSDT": sb}, "c")
    assert not any(a.kind == "EARN_RETURN" for a in plan.actions)


def test_a_deposit_in_a_live_cycle_leaves_the_book_and_the_ledger_pnl_alone(
        isolated_paths, tmp_path, monkeypatch):
    """Funding too low to enter (else the new USDT would rightly fund an
    entry): the deposit goes to Earn."""
    from test_carry_live import DEADMAN, Opener
    from test_mainnet_roundtrip import Sim
    sim = Sim(tmp_path, monkeypatch, rate=0.00001)
    cfg = cc.load(sim.cfg_file, testnet=False)
    live.write(settings.carry_live_file(), KEY, 200.0, cfg["SYMBOLS"], "giannis", sim.clock_ms)
    book_store.save(settings.carry_book_file(), KEY,
                    {"ETHUSDT": SymbolBook(dust_qty=0.00000997)}, sim.clock_ms)
    before = book_store.load(settings.carry_book_file(), KEY)[0]
    staked = sim.venue.s.earn_staked
    sim.venue.s.usdt += 100.0                          # the manual transfer from Funding
    env = {"HERMES_RISK_HMAC_KEY": KEY, "DEADMAN_URL": DEADMAN}
    rec, rc = rcc.run_cycle(cfg, env, sim.client, now_ms=sim.clock_ms, snapshot_fn=sim.snapshot,
                            exchange=sim.venue, opener=Opener(), sleep=sim.sleep)
    assert rc == 0 and rec["dry_run"] is False, rec["alerts"]
    assert [(a["kind"], a["usdt"]) for a in rec["actions"]] == [("EARN_RETURN", 100.0)]
    assert sim.venue.s.earn_staked == pytest.approx(staked + 100.0)
    from dataclasses import replace
    after = book_store.load(settings.carry_book_file(), KEY)[0]
    assert {k: replace(v, collateral_ratio=None) for k, v in after.items()} == before
    assert after["ETHUSDT"].dust_qty == 0.00000997                   # the book's dust kept
    rows = ledger.read(sim.log_dir)
    assert [(r["type"], r["leg"], r["filled"]) for r in rows] == [("order", "earn", 100.0)]
    s = ledger.summary(rows)
    assert (s["funding"], s["fees"], s["basis_pnl"], s["net"]) == (0.0, 0.0, 0.0, 0.0)


# --- 3. BTC collateral ---------------------------------------------------------------- #

def _ops(snap, cfg=None):
    env = {"HERMES_RISK_HMAC_KEY": KEY, "BYBIT_CARRY_API_KEY": "k", "BYBIT_CARRY_API_SECRET": "s",
           "DEADMAN_URL": "https://hc-ping.com/x"}
    return live_ops.LiveOps(cfg or dict(SHIPPED), env, object(), now_ms=lambda: NOW,
                            snapshot_fn=lambda c, cfg, now_ms=None, private=True: snap,
                            roundtrip_active=lambda: False)


def _flat(**kw):
    return snapshot(markets=both(**kw.pop("prices", {})),
                    positions={"ETHUSDT": short(), "BTCUSDT": short("BTCUSDT")},
                    acct=kw.pop("acct", acct()), earn_=earn(staked=200.0))


def test_live_refuses_btc_collateral_off(isolated_paths):
    problems = _ops(_flat(acct=acct(btc_collateral=False))).problems(200.0)
    assert "BTC is not active collateral (Bybit UI: Collateral -> On)" in problems


def test_live_refuses_a_btc_minimum_beyond_the_limit(isolated_paths):
    problems = _ops(_flat(prices={"btc": 135_000.0})).problems(200.0)
    assert any(p.startswith("SYMBOL_BELOW_MIN_SIZE: BTCUSDT") for p in problems)


def test_live_refuses_enable_100_with_the_200_config(isolated_paths):
    """The step list's order matters: /live enable 200, never 100 under it."""
    problems = _ops(_flat()).problems(100.0)
    assert any("MAX_NOTIONAL_PER_SYMBOL_USD" in p for p in problems)


@pytest.fixture
def preflight_mod(isolated_paths, monkeypatch):
    sys.path.insert(0, str(REPO / "deploy"))
    import preflight
    monkeypatch.setenv("YIELD_CARRY_CONFIG_FILE", str(REPO / "config" / "carry.yaml"))
    settings.env_file().write_text("BYBIT_CARRY_API_KEY=k\nBYBIT_CARRY_API_SECRET=s\n")
    return preflight


def _take(snap):
    return lambda c, cfg, now_ms=None, private=True: snap


def test_preflight_carry_symbols_ok(preflight_mod, capsys):
    assert preflight_mod.cmd_carry_symbols([], take=_take(_flat())) == 0
    out = capsys.readouterr().out
    assert "BTCUSDT: minimum perp 0.001 / spot 0.001002" in out
    assert "limit 130 (fits; fits up to a spot price of 129,741)" in out
    assert "ETHUSDT: minimum perp 0.01 / spot 0.01002" in out
    assert "limit 50 (fits; fits up to a spot price of 4,990)" in out
    assert "BTC collateral: active" in out and "ETH collateral: active" in out
    assert out.strip().endswith("OK")


@pytest.mark.parametrize("snap,why", [
    (lambda: _flat(acct=acct(btc_collateral=False)), "BTC collateral: NOT ACTIVE"),
    (lambda: _flat(prices={"btc": 135_000.0}), "DOES NOT FIT"),
])
def test_preflight_carry_symbols_fails(preflight_mod, capsys, snap, why):
    assert preflight_mod.cmd_carry_symbols([], take=_take(snap())) == 1
    out = capsys.readouterr().out
    assert why in out and out.strip().endswith("FAIL")


def test_preflight_carry_symbols_needs_the_carry_key(preflight_mod, capsys):
    settings.env_file().write_text("")
    assert preflight_mod.cmd_carry_symbols([], take=_take(_flat())) == 1
    assert "collateral cannot be read" in capsys.readouterr().out


# --- the per-symbol limit -------------------------------------------------------------- #

@pytest.mark.parametrize("value", [{"SOLUSDT": 30}, {"BTCUSDT": 0}, {"BTCUSDT": "130"}, [130]])
def test_a_bad_per_symbol_limit_is_refused(value):
    errors = cc.validate(dict(SHIPPED, MAX_NOTIONAL_BY_SYMBOL=value), testnet=False)
    assert any(e.startswith("MAX_NOTIONAL_BY_SYMBOL") for e in errors), errors


def test_the_per_symbol_limits_must_fit_the_cap():
    errors = cc.validate(dict(SHIPPED, MAX_NOTIONAL_BY_SYMBOL={"BTCUSDT": 140, "ETHUSDT": 50}),
                         testnet=False)
    assert any("add up to 190" in e for e in errors), errors


def test_a_core_symbol_not_listed_takes_the_common_limit():
    cfg = dict(SHIPPED, MAX_NOTIONAL_BY_SYMBOL={"BTCUSDT": 130})
    assert (cc.notional_cap(cfg, "BTCUSDT"), cc.notional_cap(cfg, "ETHUSDT")) == (130, 50)
    assert cc.notional_cap(dict(SHIPPED, MAX_NOTIONAL_BY_SYMBOL=None), "BTCUSDT") == 50


def test_the_round_trip_keeps_its_single_50_usdt_limit():
    sys.path.insert(0, str(REPO / "scripts"))
    import mainnet_roundtrip as rt
    cfg = rt.roundtrip_cfg(SHIPPED, notional=30.0)
    assert "MAX_NOTIONAL_BY_SYMBOL" not in cfg and cc.notional_cap(cfg, "ETHUSDT") == 30.0
    assert cc.validate(cfg, testnet=False, roundtrip=True) == []
    errors = cc.validate(dict(cfg, MAX_NOTIONAL_BY_SYMBOL={"ETHUSDT": 500}), testnet=False,
                         roundtrip=True)
    assert any(e.startswith("ROUNDTRIP: MAX_NOTIONAL_BY_SYMBOL") for e in errors)
