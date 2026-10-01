"""tools/carry_market_sample.py — basis/spread thresholds from live data
(CARRY_PLAN §13.9). Offline: synthetic samples, no network."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import carry_market_sample as cms  # noqa: E402
from carry.snapshot import basis_bps  # noqa: E402


def samples(sym, n, basis_every=None, wide_every=None):
    out = []
    for i in range(n):
        spot = 100.0
        perp = 100.025 if not (basis_every and i % basis_every == 0) else 100.5  # 2.5 vs 50 bps
        half = 0.004 if not (wide_every and i % wide_every == 0) else 0.2        # 0.8 vs 40 bps
        out.append({"ts": 1_000 + i * 60_000, "symbol": sym, "perp_bid": perp - half,
                    "perp_ask": perp + half, "spot_bid": spot - 0.004, "spot_ask": spot + 0.004})
    return out


def test_percentile_nearest_rank():
    assert cms.percentile(list(range(1, 101)), 95) == 95
    assert cms.percentile([3.0], 99) == 3.0


def test_recommendation_uses_the_cycle_functions_and_ignores_rare_outliers():
    data = samples("BTCUSDT", 200, basis_every=50) + samples("ETHUSDT", 200)
    rep = cms.analyze(data)
    # 4 of 200 samples at 50 bps are above p95 -> the normal 2.5 bps sets the limit
    expected = basis_bps(100.021, 100.029, 99.996, 100.004)
    assert rep["per_symbol"]["BTCUSDT"]["abs_basis_bps"]["p95"] == pytest.approx(expected)
    assert rep["recommended"]["MAX_ENTRY_BASIS_BPS"] == 3
    assert rep["recommended"]["MAX_SPREAD_BPS"] == 1


def test_signed_basis_is_recorded_alongside_the_absolute():
    """The entry check is one-sided (perp above spot is favourable), so the
    report keeps the sign: a perp 2.5 bps BELOW spot shows as negative."""
    data = samples("BTCUSDT", 100)
    for s in data[:10]:                                # 10 % with the perp below spot
        s["perp_bid"], s["perp_ask"] = 99.971, 99.979
    rep = cms.analyze(data + samples("ETHUSDT", 100))
    btc = rep["per_symbol"]["BTCUSDT"]
    below = basis_bps(99.971, 99.979, 99.996, 100.004)
    above = basis_bps(100.021, 100.029, 99.996, 100.004)
    assert below < 0 < above
    assert btc["basis_bps"]["min"] == pytest.approx(below)
    assert btc["basis_bps"]["p5"] == pytest.approx(below)
    assert btc["basis_bps"]["p50"] == pytest.approx(above)
    assert btc["basis_bps"]["max"] == pytest.approx(above)
    assert btc["negative_basis_share"] == pytest.approx(0.10)
    assert btc["abs_basis_bps"]["p95"] == pytest.approx(abs(above))   # the recommendation input
    assert rep["per_symbol"]["ETHUSDT"]["negative_basis_share"] == 0


def test_report_shows_the_signed_basis(tmp_path):
    data = samples("BTCUSDT", 100) + samples("ETHUSDT", 100)
    data[0]["perp_bid"], data[0]["perp_ask"] = 99.971, 99.979
    raw = tmp_path / "carry_market_x.json"
    raw.write_text(json.dumps({"source": "test", "samples": data}))
    assert cms.main(["--from-data", str(raw), "--out", str(tmp_path)]) == 0
    md = (tmp_path / "CARRY_MARKET_SAMPLE.md").read_text()
    assert "basis με πρόσημο" in md
    assert "-2.50" in md                               # BTC min, perp below spot
    assert "| 1.0% |" in md                            # share of negative samples


def test_frequent_wide_spreads_raise_the_limit():
    rep = cms.analyze(samples("BTCUSDT", 100, wide_every=2) + samples("ETHUSDT", 100))
    assert rep["recommended"]["MAX_SPREAD_BPS"] >= 40


def test_errors_and_crossed_books_are_not_samples():
    bad = [{"ts": 1, "symbol": "BTCUSDT", "error": "down"},
           {"ts": 2, "symbol": "BTCUSDT", "perp_bid": 101, "perp_ask": 100, "spot_bid": 1,
            "spot_ask": 1}]
    rep = cms.analyze(samples("BTCUSDT", 60) + bad + samples("ETHUSDT", 60))
    assert rep["per_symbol"]["BTCUSDT"]["samples"] == 60
    assert rep["per_symbol"]["BTCUSDT"]["rejected"] == 2


def test_too_few_samples_recommend_nothing():
    rep = cms.analyze(samples("BTCUSDT", cms.MIN_SAMPLES - 1) + samples("ETHUSDT", 500))
    assert rep["recommended"] is None


def test_offline_run_writes_the_report(tmp_path):
    raw = tmp_path / "carry_market_x.json"
    raw.write_text(json.dumps({"source": "test", "samples": samples("BTCUSDT", 100)
                               + samples("ETHUSDT", 100)}))
    assert cms.main(["--from-data", str(raw), "--out", str(tmp_path)]) == 0
    md = (tmp_path / "CARRY_MARKET_SAMPLE.md").read_text()
    assert "MAX_ENTRY_BASIS_BPS: 3" in md and "MAX_SPREAD_BPS: 1" in md
    assert json.loads((tmp_path / "carry_market_sample.json").read_text())["recommended"]


def test_offline_run_without_enough_data_fails(tmp_path):
    raw = tmp_path / "carry_market_x.json"
    raw.write_text(json.dumps({"source": "test", "samples": samples("BTCUSDT", 3)}))
    assert cms.main(["--from-data", str(raw), "--out", str(tmp_path)]) == 1
    assert "Καμία σύσταση" in (tmp_path / "CARRY_MARKET_SAMPLE.md").read_text()
