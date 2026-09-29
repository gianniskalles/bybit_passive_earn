"""carry/preflight.py — the minimum neutral position per symbol from
instruments-info (both legs, fee in base, R21/R22), and which symbols fit
their notional limit (decision 13.11). Offline: the SYNTHETIC capture."""

import pytest

from carry import preflight as pf
from test_carry_phase2 import load, take

CFG = {"SYMBOLS": ["BTCUSDT", "ETHUSDT"], "MAX_NOTIONAL_PER_SYMBOL_USD": 80,
       "MAX_NOTIONAL_PER_ALT_USD": 30, "SPOT_TAKER_FEE": 0.001}


def test_eth_minimum_is_one_perp_step_plus_the_spot_fee():
    m = take(load()).markets["ETHUSDT"]
    mp = pf.min_position(m, spot_fee=0.001)
    assert mp.perp_qty == pytest.approx(0.01)            # minOrderQty / qtyStep 0.01
    assert mp.spot_buy_qty == pytest.approx(0.01002)     # 0.01 / 0.999 = 0.010010 -> up to 0.00001 (R22)
    assert mp.notional_usd == pytest.approx(0.01002 * 2499.81)


def test_perp_min_notional_raises_the_quantity():
    m = take(load()).markets["ETHUSDT"]
    import dataclasses
    perp = dataclasses.replace(m.perp, min_notional=60.0)
    mp = pf.min_position(dataclasses.replace(m, perp=perp), spot_fee=0.001)
    assert mp.perp_qty == pytest.approx(0.03)            # 60 / 2500.3 -> 0.024 -> up to 0.03
    assert mp.perp_qty * m.mark_price >= 60


def test_symbols_that_do_not_fit_are_left_out_with_an_alert():
    snap = take(load())
    fit, alerts = pf.tradable_symbols(snap, dict(CFG, MAX_NOTIONAL_PER_SYMBOL_USD=30))
    assert fit == ["ETHUSDT"]                             # ~25 USD fits, BTC ~65 does not
    assert len(alerts) == 1 and alerts[0].startswith("SYMBOL_BELOW_MIN_SIZE: BTCUSDT")


def test_shipped_limit_fits_both_core_symbols():
    fit, alerts = pf.tradable_symbols(take(load()), CFG)
    assert fit == ["BTCUSDT", "ETHUSDT"] and alerts == []


def test_unreadable_or_non_trading_market_is_left_out():
    snap = take(load(), private=False)
    import dataclasses
    from types import MappingProxyType
    m = snap.markets["BTCUSDT"]
    markets = dict(snap.markets)
    markets["BTCUSDT"] = dataclasses.replace(m, perp=dataclasses.replace(m.perp, status="Settling"))
    del markets["ETHUSDT"]
    snap = dataclasses.replace(snap, markets=MappingProxyType(markets))
    fit, alerts = pf.tradable_symbols(snap, CFG)
    assert fit == []
    assert any("BTCUSDT" in a and "Trading" in a for a in alerts)
    assert any("ETHUSDT" in a and "unreadable" in a for a in alerts)


def test_account_spot_fee_is_used_when_known():
    snap = take(load())
    assert pf.spot_fee(snap, "ETHUSDT", CFG) == 0.001        # from fee-rate
    assert pf.spot_fee(take(load(), private=False), "ETHUSDT", dict(CFG, SPOT_TAKER_FEE=0.002)) == 0.002
