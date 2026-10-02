"""Altcoin extension (CARRY_PLAN §13.10): candidate selection, a GO of its
own per alt, and the config gate. Offline: fake clients, synthetic data."""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import carry_alt_calibrate as cac  # noqa: E402
from bybit_earn_tool import BybitAPIError  # noqa: E402
from carry import config as cc  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SHIPPED = yaml.safe_load((REPO / "config" / "carry.yaml").read_text())
NOW = 1_790_000_000_000
DAY = 86_400_000
E8 = 8 * 3_600_000


# --- candidate selection ------------------------------------------------------ #

class FakeClient:
    """Public market data for a small universe; `collateral` None = unreadable."""

    def __init__(self, universe, collateral):
        self.u, self.collateral = universe, collateral

    def get_linear_tickers(self):
        return [{"symbol": s, "turnover24h": str(v["turnover"])} for s, v in self.u.items()]

    def get_instrument(self, category, symbol):
        v = self.u[symbol]
        if category == "linear":
            row = {"symbol": symbol, "contractType": v.get("type", "LinearPerpetual"),
                   "status": v.get("status", "Trading"), "baseCoin": v.get("base", symbol[:-4]),
                   "quoteCoin": "USDT"}
            if v.get("launch", True) is not None:
                row["launchTime"] = str(NOW - v.get("age_days", 400) * DAY)
            return row
        if not v.get("spot", True):
            raise BybitAPIError(f"no spot instrument {symbol}")
        return {"symbol": symbol, "status": "Trading", "baseCoin": v.get("spot_base", symbol[:-4]),
                "quoteCoin": "USDT"}

    def get_collateral_ratios(self):
        if self.collateral is None:
            raise BybitAPIError("collateral endpoint unreadable")
        return self.collateral


UNIVERSE = {
    "BTCUSDT": {"turnover": 9e9}, "ETHUSDT": {"turnover": 8e9},
    "SOLUSDT": {"turnover": 3e9}, "XRPUSDT": {"turnover": 2e9},
    "DOGEUSDT": {"turnover": 1.5e9, "age_days": 100},              # too young
    "1000PEPEUSDT": {"turnover": 1.4e9, "spot_base": "PEPE"},      # spot base differs
    "USDCUSDT": {"turnover": 1.3e9},                               # stablecoin
    "SUIUSDT": {"turnover": 1.2e9, "spot": False},                 # no spot
    "WIFUSDT": {"turnover": 1.1e9},                                # not collateral
    "ADAUSDT": {"turnover": 1.0e9, "launch": None},                # launchTime unknown
    "LINKUSDT": {"turnover": 0.9e9, "status": "Settling"},         # not trading
    "TINYUSDT": {"turnover": 1e3},                                 # outside the top
}
COLLATERAL = {"SOL": 0.9, "XRP": 0.85, "DOGE": 0.8, "PEPE": 0.7, "SUI": 0.8, "ADA": 0.8,
              "LINK": 0.8, "USDC": 1.0, "TINY": 0.5, "WIF": 0.0}


def test_candidates_follow_all_four_rules():
    sel = cac.select_candidates(FakeClient(UNIVERSE, COLLATERAL), top=9, now_ms=NOW)
    assert sel["candidates"] == ["SOLUSDT", "XRPUSDT"]
    why = sel["excluded"]
    assert "months" in why["DOGEUSDT"]
    assert "spot" in why["1000PEPEUSDT"] and "spot" in why["SUIUSDT"]
    assert "stablecoin" in why["USDCUSDT"]
    assert "collateral" in why["WIFUSDT"]
    assert "launchTime" in why["ADAUSDT"]
    assert "Trading" in why["LINKUSDT"]
    assert "TINYUSDT" not in why and "TINYUSDT" not in sel["candidates"]
    assert "BTCUSDT" not in why and "BTCUSDT" not in sel["candidates"]


def test_unreadable_collateral_excludes_every_candidate():
    sel = cac.select_candidates(FakeClient(UNIVERSE, None), top=9, now_ms=NOW)
    assert sel["candidates"] == []
    assert "collateral unknown" in sel["excluded"]["SOLUSDT"]


def test_top_is_ranked_by_perp_turnover():
    sel = cac.select_candidates(FakeClient(UNIVERSE, COLLATERAL), top=1, now_ms=NOW)
    assert sel["candidates"] == ["SOLUSDT"] and sel["ranked"][:3] == ["SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    assert sel["excluded"] == {}


def _client(body):
    from test_carry_phase2 import Scripted
    from carry.client import CarryPublicClient
    return CarryPublicClient(session=Scripted(lambda p, q: body), testnet=False)


def test_collateral_ratios_parse_the_first_tier():
    body = {"retCode": 0, "retMsg": "OK", "result": {"list": [
        {"currency": "sol", "collateralRatioList": [{"minQty": "0", "maxQty": "100",
                                                     "collateralRatio": "0.9"},
                                                    {"minQty": "100", "maxQty": "",
                                                     "collateralRatio": "0.5"}]}]}}
    assert _client(body).get_collateral_ratios() == {"SOL": 0.9}


@pytest.mark.parametrize("lst", [[], [{"currency": "SOL"}], [{"currency": "SOL",
                                                              "collateralRatioList": []}]])
def test_collateral_ratios_surprise_is_an_error(lst):
    with pytest.raises(BybitAPIError):
        _client({"retCode": 0, "retMsg": "OK", "result": {"list": lst}}).get_collateral_ratios()


# --- a GO of its own ------------------------------------------------------------- #

def data_with(funding_by_symbol, days=180):
    start = NOW - days * DAY
    start -= start % E8
    return {"fetched_at_ms": NOW, "days": days, "source": "test",
            "layer_a": {"source": "test", "points": [[start, 0.0173]]},
            "selection": {"candidates": list(funding_by_symbol), "excluded": {}, "ranked": []},
            "symbols": {s: {"funding": [[start + i * E8, r] for i in range(days * 3)]}
                        for s, r in funding_by_symbol.items()}}


def test_each_alt_gets_its_own_verdict_with_the_shipped_thresholds():
    rep = cac.calibrate_alts(data_with({"SOLUSDT": 0.0003, "XRPUSDT": 0.00003}), SHIPPED)
    assert rep["per_symbol"]["SOLUSDT"]["verdict"]["go"] is True
    assert rep["per_symbol"]["XRPUSDT"]["verdict"]["go"] is False
    assert rep["go_symbols"] == ["SOLUSDT"]
    assert rep["thresholds"] == {k: SHIPPED[k] for k in cc.GO_THRESHOLD_KEYS}


def test_short_history_is_no_go():
    d = data_with({"SOLUSDT": 0.0003}, days=100)
    rep = cac.calibrate_alts(d, SHIPPED)
    assert rep["per_symbol"]["SOLUSDT"]["verdict"]["go"] is False
    assert "history" in rep["per_symbol"]["SOLUSDT"]["verdict"]["reason"]


def test_offline_run_writes_a_separate_report(tmp_path):
    raw = tmp_path / "carry_alt_data_x.json"
    raw.write_text(json.dumps(data_with({"SOLUSDT": 0.0003, "XRPUSDT": 0.00003})))
    assert cac.main(["--from-data", str(raw), "--out", str(tmp_path),
                     "--config", str(REPO / "config" / "carry.yaml")]) == 0
    assert (tmp_path / "CARRY_CALIBRATION_ALTS.md").exists()
    assert not (tmp_path / "CARRY_CALIBRATION.md").exists()
    rep = json.loads((tmp_path / "carry_alt_calibration.json").read_text())
    assert rep["go_symbols"] == ["SOLUSDT"]
    assert "SOLUSDT" in (tmp_path / "CARRY_CALIBRATION_ALTS.md").read_text()


# --- config gate ------------------------------------------------------------------- #

def complete(**over):
    cfg = dict(SHIPPED)
    cfg.update(MAX_ENTRY_BASIS_BPS=10, MAX_SPREAD_BPS=5, TOTAL_CAPITAL_CAP_USD=1000,
               MAX_NOTIONAL_PER_SYMBOL_USD=450, USDT_BUFFER_USD=100,
               DEADMAN_URL="https://hc-ping.com/abc")
    cfg.update(over)
    return cfg


def go_report(symbols, thresholds=None):
    return {"go_symbols": list(symbols),
            "thresholds": thresholds or {k: SHIPPED[k] for k in cc.GO_THRESHOLD_KEYS}}


def keys_of(errors):
    return {e.split(":", 1)[0] for e in errors}


def test_core_symbols_need_no_alt_report():
    assert cc.validate(complete(), testnet=False, alt_report=None) == []


def test_alt_without_its_own_go_is_refused():
    cfg = complete(SYMBOLS=["BTCUSDT", "SOLUSDT"], MAX_NOTIONAL_PER_ALT_USD=100)
    assert "SYMBOLS" in keys_of(cc.validate(cfg, testnet=False, alt_report=None))
    assert "SYMBOLS" in keys_of(cc.validate(cfg, testnet=False, alt_report=go_report(["XRPUSDT"])))
    assert cc.validate(cfg, testnet=False, alt_report=go_report(["SOLUSDT"])) == []


def test_go_measured_with_other_thresholds_does_not_count():
    other = {k: SHIPPED[k] for k in cc.GO_THRESHOLD_KEYS}
    other["MIN_HOLD_HOURS"] = 72
    cfg = complete(SYMBOLS=["SOLUSDT"], MAX_NOTIONAL_PER_ALT_USD=100)
    errors = cc.validate(cfg, testnet=False, alt_report=go_report(["SOLUSDT"], other))
    assert "SYMBOLS" in keys_of(errors)


@pytest.mark.parametrize("value", [None, 0, 450, 500])
def test_alt_notional_must_be_set_and_below_the_core_limit(value):
    cfg = complete(SYMBOLS=["SOLUSDT"], MAX_NOTIONAL_PER_ALT_USD=value)
    errors = cc.validate(cfg, testnet=False, alt_report=go_report(["SOLUSDT"]))
    assert "MAX_NOTIONAL_PER_ALT_USD" in keys_of(errors)


def test_notional_cap_per_symbol():
    cfg = complete(SYMBOLS=["BTCUSDT", "SOLUSDT"], MAX_NOTIONAL_PER_ALT_USD=100)
    assert cc.notional_cap(cfg, "BTCUSDT") == 450
    assert cc.notional_cap(cfg, "SOLUSDT") == 100


def test_malformed_symbol_is_refused():
    assert "SYMBOLS" in keys_of(cc.validate(complete(SYMBOLS=["sol-usdt"]), testnet=False,
                                            alt_report=None))


def test_load_reads_the_committed_alt_report(tmp_path, monkeypatch):
    rep = tmp_path / "alts.json"
    rep.write_text(json.dumps(go_report(["SOLUSDT"])))
    monkeypatch.setattr(cc, "ALT_REPORT_FILE", rep)
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(complete(SYMBOLS=["SOLUSDT"], MAX_NOTIONAL_PER_ALT_USD=100)))
    assert cc.load(path, testnet=False)["SYMBOLS"] == ["SOLUSDT"]
    rep.unlink()
    with pytest.raises(cc.CarryConfigError, match="SYMBOLS"):
        cc.load(path, testnet=False)
