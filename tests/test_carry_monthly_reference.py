"""The monthly measurement's reference coins (30/10): LINKUSDT, HBARUSDT,
DOGEUSDT, NEARUSDT — measured for reference only, never in SYMBOLS.

Each gets the altcoin GO of tools/carry_alt_calibrate.py (§13.10
eligibility, then the §2 criteria with the thresholds of config/carry.yaml).
The monthly Telegram message has one line per coin; a coin GO this month and
last month gets a separate message. The config is never changed.
"""

import json
from pathlib import Path

import pytest
import yaml

from bybit_earn_tool import BybitAPIError
from tools import carry_monthly as cm

REPO = Path(__file__).resolve().parent.parent
DATA_FILE = sorted((REPO / "calibration").glob("carry_data_*.json"))[-1]
DATA = json.loads(DATA_FILE.read_text())
CFG = yaml.safe_load((REPO / "config" / "carry.yaml").read_text())
DAY = 86_400_000
NOW = DATA["fetched_at_ms"]
TIMES = [t for t, _ in DATA["symbols"]["ETHUSDT"]["funding"]]


class PublicClient:
    """Public Bybit data for the four coins: LINK pays a steady 0.03 % / 8 h
    (GO), DOGE 0.001 % (NO-GO), HBAR is not UTA collateral, NEAR's perp
    cannot be read."""
    rates = {"LINKUSDT": 0.0003, "DOGEUSDT": 0.00001, "HBARUSDT": 0.0003, "NEARUSDT": 0.0003}

    def get_collateral_ratios(self):
        return {"LINK": 0.8, "DOGE": 0.8, "NEAR": 0.7}

    def get_instrument(self, category, symbol):
        if symbol == "NEARUSDT":
            raise BybitAPIError("instruments-info: timeout")
        base = symbol[:-4]
        if category == "linear":
            return {"contractType": "LinearPerpetual", "status": "Trading", "baseCoin": base,
                    "launchTime": str(NOW - 900 * DAY)}
        return {"status": "Trading", "baseCoin": base}

    def get_funding_history(self, symbol, start_ms, end_ms):
        return [[t, self.rates[symbol]] for t in TIMES if start_ms <= t <= end_ms]


def measured():
    ref = cm.fetch_reference(client=PublicClient(), now_ms=NOW)
    return ref, cm.measure_reference(ref, DATA["layer_a"]["points"], CFG)


def test_the_four_reference_coins_are_never_in_symbols():
    assert cm.REFERENCE_SYMBOLS == ("LINKUSDT", "HBARUSDT", "DOGEUSDT", "NEARUSDT")
    assert not set(cm.REFERENCE_SYMBOLS) & set(CFG["SYMBOLS"])


def test_each_coin_gets_a_result():
    ref, res = measured()
    assert list(res) == list(cm.REFERENCE_SYMBOLS)                 # in that order
    assert res["LINKUSDT"]["go"] is True and res["LINKUSDT"]["status"] == "measured"
    assert res["LINKUSDT"]["excess_apr"] > 0.03
    assert res["DOGEUSDT"]["go"] is False and res["DOGEUSDT"]["status"] == "measured"
    assert res["HBARUSDT"] == {"go": False, "status": "excluded", "reason": "not UTA collateral"}
    assert res["NEARUSDT"]["status"] == "excluded"
    assert "perp instrument unreadable" in res["NEARUSDT"]["reason"]


def test_a_coin_that_raises_is_reported_unreadable_not_dropped():
    class Broken(PublicClient):
        def get_funding_history(self, symbol, start_ms, end_ms):
            if symbol == "DOGEUSDT":
                raise RuntimeError("connection reset")
            return super().get_funding_history(symbol, start_ms, end_ms)

    ref = cm.fetch_reference(client=Broken(), now_ms=NOW)
    res = cm.measure_reference(ref, DATA["layer_a"]["points"], CFG)
    assert res["DOGEUSDT"] == {"go": False, "status": "unreadable",
                               "reason": "RuntimeError: connection reset"}
    assert res["LINKUSDT"]["go"] is True


def test_one_telegram_line_per_coin():
    _, res = measured()
    lines = [cm.reference_line(s, r) for s, r in res.items()]
    assert lines[0].startswith("  LINKUSDT: GO — excess over Earn +")
    assert lines[1] == "  HBARUSDT: NO-GO — excluded: not UTA collateral"
    assert lines[2].startswith("  DOGEUSDT: NO-GO — excess over Earn ") and "(fails:" in lines[2]
    assert lines[3].startswith("  NEARUSDT: NO-GO — excluded: perp instrument unreadable")


@pytest.mark.parametrize("month,prev", [("2026-11", "2026-10"), ("2027-01", "2026-12")])
def test_previous_month(month, prev):
    assert cm.previous_month(month) == prev


def _run(tmp_path, monkeypatch, month, ref_file):
    import notify
    sent = []
    monkeypatch.setattr(notify.Notifier, "event", lambda self, text: sent.append(text) or True)
    out = tmp_path / "reports" / month
    rc = cm.main(["--from-data", str(DATA_FILE), "--reference-data", str(ref_file),
                  "--out", str(out), "--month", month])
    return rc, sent, json.loads((out / "carry_calibration.json").read_text())


@pytest.fixture
def ref_file(tmp_path):
    ref, _ = measured()
    path = tmp_path / "carry_reference_data.json"
    path.write_text(json.dumps(ref))
    return path


def test_the_monthly_message_has_the_reference_lines(isolated_paths, tmp_path, monkeypatch,
                                                     ref_file):
    cfg_before = (REPO / "config" / "carry.yaml").read_bytes()
    rc, sent, report = _run(tmp_path, monkeypatch, "2026-11", ref_file)
    assert rc == 0 and len(sent) == 1                              # first month: no alert
    text = sent[0]
    assert "reference coins (not in SYMBOLS, never traded):" in text
    for sym in cm.REFERENCE_SYMBOLS:
        assert sum(line.startswith(f"  {sym}:") for line in text.splitlines()) == 1
    assert report["reference"]["LINKUSDT"]["go"] is True
    assert (REPO / "config" / "carry.yaml").read_bytes() == cfg_before


def test_go_two_months_in_a_row_is_a_separate_message(isolated_paths, tmp_path, monkeypatch,
                                                      ref_file):
    _run(tmp_path, monkeypatch, "2026-11", ref_file)
    rc, sent, _ = _run(tmp_path, monkeypatch, "2026-12", ref_file)
    assert rc == 0 and len(sent) == 2
    assert sent[1].startswith("🔔 Carry — reference coin(s) GO two months in a row "
                              "(2026-11, 2026-12): LINKUSDT.")
    assert "not traded" in sent[1] and "DOGEUSDT" not in sent[1]


def test_no_alert_when_last_month_was_no_go(isolated_paths, tmp_path, monkeypatch, ref_file):
    _run(tmp_path, monkeypatch, "2026-11", ref_file)
    prev = tmp_path / "reports" / "2026-11" / "carry_calibration.json"
    report = json.loads(prev.read_text())
    report["reference"]["LINKUSDT"]["go"] = False
    prev.write_text(json.dumps(report))
    rc, sent, _ = _run(tmp_path, monkeypatch, "2026-12", ref_file)
    assert rc == 0 and len(sent) == 1


def test_no_alert_after_a_gap_month(isolated_paths, tmp_path, monkeypatch, ref_file):
    """GO in 2026-10 and 2026-12, nothing in 2026-11: not two in a row."""
    _run(tmp_path, monkeypatch, "2026-10", ref_file)
    rc, sent, _ = _run(tmp_path, monkeypatch, "2026-12", ref_file)
    assert rc == 0 and len(sent) == 1


def test_offline_without_reference_data_measures_no_reference(isolated_paths, tmp_path,
                                                             monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify.Notifier, "event", lambda self, text: sent.append(text) or True)
    assert cm.main(["--from-data", str(DATA_FILE), "--out", str(tmp_path / "m")]) == 0
    assert "reference coins" not in sent[0]
    assert "reference" not in json.loads((tmp_path / "m" / "carry_calibration.json").read_text())
