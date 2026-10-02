"""CARRY_PLAN Phase 0B — calibration: public data -> simulation with the
production decide() -> report with the §2 GO criteria. No network: the
client is tested with a fake session, the simulation with synthetic data."""

import json
import sys
from pathlib import Path

import pytest

import carry.backtest as bt
import carry.decide as decide_mod
from carry.client import CarryPublicClient
from bybit_earn_tool import BybitAPIError

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import carry_calibrate  # noqa: E402

H = 3600 * 1000
DAY = 24 * H
T0 = 1_780_000_000_000 - (1_780_000_000_000 % (8 * H))


def series(rates, start=T0, step_h=8):
    return [(start + i * step_h * H, r) for i, r in enumerate(rates)]


PARAMS = dict(entry_min_expected_apr=0.03, entry_min_predicted_rate=0.0001,
              exit_predicted_floor=-0.00005, exit_horizon_hours=168, smoothing_settlements=9,
              min_hold_hours=168, max_round_trips_30d=4,
              no_funding_action_before_settlement_min=15, max_entry_basis_bps=None,
              max_spread_bps=None, spot_taker_fee=0.001, perp_taker_fee=0.00055)


def p(**over):
    return decide_mod.Params(**{**PARAMS, **over})


def flat_a(apr=0.02):
    return [(T0 - 10 * DAY, apr)]


# --- simulation ------------------------------------------------------------ #

def test_calibration_uses_production_decide(monkeypatch):
    calls = []
    real = decide_mod.decide_symbol

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(decide_mod, "decide_symbol", spy)
    res = bt.simulate(series([0.0002] * 60), flat_a(), p(), predictor="lagged")
    assert len(calls) == 60 and res.settlements == 60


def test_constant_positive_funding_enters_once_and_earns():
    res = bt.simulate(series([0.0002] * 540), flat_a(0.02), p(), predictor="lagged")
    assert res.entries == 1 and res.exits == 0
    assert res.net_apr == pytest.approx(0.0002 * 1095 * res.time_in_position, abs=0.02)
    assert res.net_apr - res.layer_a_apr > 0.03


def test_constant_negative_funding_never_enters_and_equals_layer_a():
    res = bt.simulate(series([-0.0001] * 540), flat_a(0.02), p(), predictor="lagged")
    assert res.entries == 0 and res.time_in_position == 0
    assert res.net_apr == pytest.approx(res.layer_a_apr, rel=1e-9)
    assert res.layer_a_apr == pytest.approx(0.02, rel=0.01)


def test_costs_are_charged_and_sensitivity_scales_them():
    s = series([0.0002] * 540)
    base = bt.simulate(s, flat_a(), p(), predictor="lagged")
    worse = bt.simulate(s, flat_a(), p(), predictor="lagged", cost_multiplier=1.5)
    assert base.costs == pytest.approx(0.0031 * base.entries - 0.00155 * (base.entries - base.exits))
    assert worse.costs == pytest.approx(1.5 * base.costs)
    assert worse.net_apr < base.net_apr


def test_oracle_vs_lagged_predictor():
    """oracle = the rate that actually settled (≈ the predicted rate visible
    15' before); lagged = the previous settled rate (conservative)."""
    rates = [0.0001 * (i % 7) for i in range(40)]
    o = bt.simulate(series(rates), flat_a(), p(), predictor="oracle")
    lag = bt.simulate(series(rates), flat_a(), p(), predictor="lagged")
    assert o.predicted_used == rates
    assert lag.predicted_used == [None] + rates[:-1]
    with pytest.raises(ValueError):
        bt.simulate(series(rates), flat_a(), p(), predictor="crystal-ball")


def test_worst_30d_and_negative_stats():
    # enter on good funding, then a long negative stretch
    rates = [0.0003] * 30 + [-0.0003] * 120 + [0.0003] * 30
    res = bt.simulate(series(rates), flat_a(), p(), predictor="lagged")
    assert res.worst_30d_return < 0
    assert res.negative_share == pytest.approx(120 / 180)
    assert res.longest_negative_days == pytest.approx(40, abs=0.5)


def test_interval_change_blocks_entry_at_that_settlement():
    # 8h settlements, then 1h settlements: the step after the change sees
    # previous != current interval
    s = series([0.0] * 12) + series([0.0003] * 12, start=T0 + 12 * 8 * H, step_h=1)
    res = bt.simulate(s, flat_a(), p(), predictor="lagged")
    assert any("interval changed" in r for r in res.reasons_seen)


def test_go_criteria():
    good = bt.simulate(series([0.0002] * 540), flat_a(0.02), p(), predictor="lagged")
    bad = bt.simulate(series([-0.0001] * 540), flat_a(0.02), p(), predictor="lagged")
    assert bt.go_check(good, good)["go"] is True
    verdict = bt.go_check(bad, bad)
    assert verdict["go"] is False and verdict["excess_ok"] is False


def test_layer_a_lookup_is_a_step_function():
    pts = [(T0, 0.01), (T0 + 10 * DAY, 0.03)]
    assert bt.layer_a_at(pts, T0 + DAY) == 0.01
    assert bt.layer_a_at(pts, T0 + 11 * DAY) == 0.03
    assert bt.layer_a_at(pts, T0 - DAY) == 0.01  # before the first point: first value


# --- public client (fake session) ------------------------------------------- #

class _Resp:
    def __init__(self, payload):
        self.payload, self.status_code = payload, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages, self.headers, self.urls = list(pages), {}, []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.urls.append(url)
        return _Resp(self.pages.pop(0))


def _page(rows):
    return {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": rows}}


def test_funding_history_paginates_backwards_and_sorts():
    newest = [{"symbol": "BTCUSDT", "fundingRate": "0.0001",
               "fundingRateTimestamp": str(T0 + i * 8 * H)} for i in range(399, 199, -1)]
    older = [{"symbol": "BTCUSDT", "fundingRate": "0.0002",
              "fundingRateTimestamp": str(T0 + i * 8 * H)} for i in range(199, -1, -1)]
    s = FakeSession([_page(newest), _page(older), _page([])])
    c = CarryPublicClient(session=s)
    rows = c.get_funding_history("BTCUSDT", start_ms=T0, end_ms=T0 + 400 * 8 * H)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert len(rows) == 400 and rows[0] == (T0, 0.0002)
    assert "endTime=" in s.urls[1] and "limit=200" in s.urls[0]


def test_funding_history_error_raises():
    s = FakeSession([{"retCode": 10001, "retMsg": "params error", "result": {}}])
    with pytest.raises(BybitAPIError):
        CarryPublicClient(session=s).get_funding_history("BTCUSDT", T0, T0 + DAY)


def test_unparseable_funding_row_raises():
    s = FakeSession([_page([{"symbol": "BTCUSDT", "fundingRate": "x",
                             "fundingRateTimestamp": str(T0)}])])
    with pytest.raises(BybitAPIError):
        CarryPublicClient(session=s).get_funding_history("BTCUSDT", T0, T0 + DAY)


class AprServer:
    """/v5/earn/product and /v5/earn/apr-history honouring startTime/endTime;
    without them it returns only the last 7 days, as Bybit did on the VPS."""

    def __init__(self, first_ms, last_ms, fail_at=None):
        self.points = list(range(first_ms, last_ms + 1, H))
        self.headers, self.windows, self.fail_at = {}, [], fail_at

    def request(self, method, url, headers=None, data=None, timeout=None):
        import urllib.parse
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/v5/earn/product":
            return _Resp({"retCode": 0, "retMsg": "OK", "result": {"list": [
                {"coin": "USDT", "productId": "1", "category": "FlexibleSaving"}]}})
        if "startTime" in q:
            s, e = int(q["startTime"]), int(q["endTime"])
            self.windows.append((s, e))
            if self.fail_at is not None and len(self.windows) == self.fail_at:
                return _Resp({"retCode": 10016, "retMsg": "server error", "result": {}})
        else:
            e = self.points[-1]
            s = e - 7 * DAY
        rows = [{"timestamp": str(t), "apr": "1.73%"} for t in self.points if s <= t <= e]
        return _Resp({"retCode": 0, "retMsg": "OK", "result": {"list": rows[::-1]}})


def test_layer_a_history_covers_the_requested_180_days():
    end = T0 + 200 * DAY
    srv = AprServer(T0, end)
    pid, rows = CarryPublicClient(session=srv).get_usdt_flexible_apr_history(
        start_ms=end - 180 * DAY, end_ms=end)
    ts = sorted(int(r["timestamp"]) for r in rows)
    assert pid == "1"
    assert ts[0] <= end - 180 * DAY + H and ts[-1] == end
    assert len(ts) == len(set(ts))                           # windows do not double-count
    assert all(e - s <= 7 * DAY for s, e in srv.windows)     # never beyond the observed window
    assert srv.windows[0][1] == end                           # newest first, walking back


def test_layer_a_history_failure_in_any_window_raises():
    end = T0 + 200 * DAY
    with pytest.raises(BybitAPIError):
        CarryPublicClient(session=AprServer(T0, end, fail_at=3)).get_usdt_flexible_apr_history(
            start_ms=end - 180 * DAY, end_ms=end)


def test_calibrate_fetch_asks_for_the_whole_period(monkeypatch):
    calls = {}

    class Client:
        base_url = "x"

        def get_funding_history(self, sym, start, end):
            return [(start + i * 8 * H, 0.0001) for i in range(40)]

        def get_instrument(self, cat, sym):
            return {}

        def get_usdt_flexible_apr_history(self, start_ms=None, end_ms=None):
            calls["span"] = (start_ms, end_ms)
            return "1", [{"timestamp": str(start_ms), "apr": "1.7%"}]

    import carry.client
    monkeypatch.setattr(carry.client, "CarryPublicClient", lambda: Client())
    data = carry_calibrate.fetch(["BTCUSDT"], 180)
    s, e = calls["span"]
    assert e - s == 180 * DAY and data["layer_a"]["points"][0][0] == s


# --- the tool end to end (offline) ------------------------------------------ #

def _dataset(tmp_path, rates=None, layer_a=True):
    rates = rates or ([0.00015] * 300 + [-0.00005] * 60 + [0.0002] * 180)
    data = {"fetched_at_ms": T0 + 540 * 8 * H, "days": 180, "source": "synthetic",
            "symbols": {sym: {"funding": series(rates),
                              "instrument_linear": {"fundingInterval": 480, "status": "Trading"},
                              "instrument_spot": {"status": "Trading"}}
                        for sym in ("BTCUSDT", "ETHUSDT")},
            "layer_a": {"source": "synthetic", "points": flat_a(0.02) if layer_a else []}}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data))
    return path


def test_tool_writes_report_offline(tmp_path):
    out = tmp_path / "report"
    rc = carry_calibrate.main(["--from-data", str(_dataset(tmp_path)), "--out", str(out)])
    assert rc == 0
    report = json.loads((out / "carry_calibration.json").read_text())
    assert set(report["verdict"]) >= {"go", "excess_ok", "worst_30d_ok", "robust_to_costs"}
    assert report["recommended_params"]["smoothing_settlements"] is not None
    assert report["out_of_sample"]["net_apr"] is not None
    for sym in ("BTCUSDT", "ETHUSDT"):
        assert sym in report["per_symbol"]
    md = (out / "CARRY_CALIBRATION.md").read_text()
    assert "GO" in md and "synthetic" in md


def test_tool_fails_without_layer_a_data(tmp_path, capsys):
    rc = carry_calibrate.main(["--from-data", str(_dataset(tmp_path, layer_a=False)),
                               "--out", str(tmp_path / "r")])
    assert rc == 1 and "layer A" in capsys.readouterr().out


def test_tool_marks_assumed_layer_a(tmp_path):
    out = tmp_path / "r"
    rc = carry_calibrate.main(["--from-data", str(_dataset(tmp_path, layer_a=False)),
                               "--out", str(out), "--assume-layer-a-apr", "0.02"])
    assert rc == 0
    assert "ASSUMED" in (out / "CARRY_CALIBRATION.md").read_text()


def test_recommended_params_use_carry_plan_config_names(tmp_path):
    out = tmp_path / "report"
    carry_calibrate.main(["--from-data", str(_dataset(tmp_path)), "--out", str(out)])
    md = (out / "CARRY_CALIBRATION.md").read_text()
    yaml_block = md.split("```yaml\n", 1)[1].split("```", 1)[0]
    keys = [line.split(":", 1)[0] for line in yaml_block.strip().splitlines()]
    plan = (REPO / "CARRY_PLAN.md").read_text()
    plan_yaml = plan.split("```yaml\n", 1)[1].split("```", 1)[0]
    for k in keys:
        assert f"\n{k}:" in "\n" + plan_yaml, f"{k} is not a CARRY_PLAN §8 config name"


def test_report_states_layer_a_coverage(tmp_path):
    """The real run had ~7 days of Easy Earn APR history for a 180-day
    period; the rest was the first value carried back. Say so."""
    data = json.loads(_dataset(tmp_path).read_text())
    last = data["symbols"]["BTCUSDT"]["funding"][-1][0]
    data["layer_a"]["points"] = [[last - 7 * DAY + i * H, 0.02] for i in range(7 * 24)]
    path = tmp_path / "short_a.json"
    path.write_text(json.dumps(data))
    out = tmp_path / "r"
    assert carry_calibrate.main(["--from-data", str(path), "--out", str(out)]) == 0
    report = json.loads((out / "carry_calibration.json").read_text())
    cov = report["layer_a_coverage"]
    assert cov["covered_days"] == pytest.approx(7, abs=0.1)
    assert cov["extrapolated_days"] == pytest.approx(report["period"]["days"] - 7, abs=0.2)
    md = (out / "CARRY_CALIBRATION.md").read_text()
    assert "ιστορικό APR καλύπτει" in md


def test_report_yaml_block_parses_to_numbers(tmp_path):
    import yaml
    out = tmp_path / "report"
    carry_calibrate.main(["--from-data", str(_dataset(tmp_path)), "--out", str(out)])
    md = (out / "CARRY_CALIBRATION.md").read_text()
    block = yaml.safe_load(md.split("```yaml\n", 1)[1].split("```", 1)[0])
    for k, v in block.items():
        assert v is None or isinstance(v, (int, float)), (k, v)
