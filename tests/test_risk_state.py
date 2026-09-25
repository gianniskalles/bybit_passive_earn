"""risk_state.py — structured verification (T1.2) and the signed `source` (T1.3)."""

import json
import time

import pytest

import risk_state as rs
from signing import sign

KEY = "k"


def _now():
    return int(time.time() * 1000)


def test_roundtrip_ok(tmp_path):
    p = tmp_path / "s.json"
    rec = rs.write(p, KEY, "NORMAL", "why", rs.SOURCE_OPERATOR)
    v = rs.verify(p, KEY)
    assert (v.code, v.signature_valid, v.fresh) == ("OK", True, True)
    assert (v.state, v.source, v.ts, v.reason) == ("NORMAL", "operator", rec["ts"], "why")
    assert set(json.loads(p.read_text())) == {"profile", "state", "ts", "reason", "source", "sig"}
    assert not list(tmp_path.glob("*.tmp"))


def test_stale_is_signed_but_not_fresh(tmp_path):
    p = tmp_path / "s.json"
    rs.write(p, KEY, "UNWIND", "x", rs.SOURCE_OPERATOR, ts_ms=_now() - 2 * 3600 * 1000)
    v = rs.verify(p, KEY)
    assert (v.code, v.signature_valid, v.fresh, v.state) == ("RISK_STATE_STALE", True, False, "UNWIND")


def test_future_ts_is_not_fresh(tmp_path):
    p = tmp_path / "s.json"
    rs.write(p, KEY, "NORMAL", "x", rs.SOURCE_OPERATOR, ts_ms=_now() + 3600 * 1000)
    v = rs.verify(p, KEY)
    assert (v.code, v.signature_valid, v.fresh) == ("RISK_STATE_FUTURE_TS", True, False)


def test_source_is_signed(tmp_path):
    p = tmp_path / "s.json"
    rec = rs.write(p, KEY, "NO_NEW_POSITIONS", "x", rs.SOURCE_OPERATOR)
    rec["source"] = rs.SOURCE_BOOTSTRAP  # try to make an operator hold promotable
    p.write_text(json.dumps(rec))
    v = rs.verify(p, KEY)
    assert (v.code, v.signature_valid, v.state) == ("RISK_STATE_BAD_SIGNATURE", False, None)


@pytest.mark.parametrize("content,code", [
    (None, "RISK_STATE_MISSING"),
    ('{"state":"UNWIND","ts":17', "RISK_STATE_UNREADABLE"),
    ("[]", "RISK_STATE_MALFORMED"),
])
def test_unverifiable_codes(tmp_path, content, code):
    p = tmp_path / "s.json"
    if content is not None:
        p.write_text(content)
    v = rs.verify(p, KEY)
    assert (v.code, v.signature_valid, v.fresh, v.state) == (code, False, False, None)


def test_legacy_record_without_source_is_malformed(tmp_path):
    obj = {"profile": rs.PROFILE, "state": "NORMAL", "ts": _now(), "reason": "old"}
    obj["sig"] = sign(KEY, obj)
    p = tmp_path / "s.json"
    p.write_text(json.dumps(obj))
    assert rs.verify(p, KEY).code == "RISK_STATE_MALFORMED"


def test_no_key(tmp_path):
    assert rs.verify(tmp_path / "s.json", "").code == "RISK_STATE_NO_KEY"


def test_make_record_rejects_bad_input():
    with pytest.raises(ValueError):
        rs.make_record(KEY, "PANIC", "x", rs.SOURCE_OPERATOR)
    with pytest.raises(ValueError):
        rs.make_record(KEY, "NORMAL", "x", "someone")
    with pytest.raises(ValueError):
        rs.make_record("", "NORMAL", "x", rs.SOURCE_OPERATOR)


def test_cli_write_is_operator(isolated_paths, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_RISK_HMAC_KEY", KEY)
    assert rs.main(["write", "UNWIND", "manual", "stop"]) == 0
    v = rs.verify(isolated_paths["YIELD_STATE_FILE"], KEY)
    assert (v.state, v.source, v.reason) == ("UNWIND", "operator", "manual stop")
    assert rs.main(["verify"]) == 0
