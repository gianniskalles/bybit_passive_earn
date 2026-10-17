"""The forced testnet run (17/10).

- Raw Bybit responses are recorded on testnet only, and only the response:
  body, status, method, URL path. Never a header (X-BAPI-*), the query, the
  request body, a key, a secret or a signature.
- The loop runs as the transient unit yield-carry-testnet
  (scripts/carry_testnet_loop.sh), never nohup; it stops with
  `systemctl stop yield-carry-testnet` or the STOP file; deploy.sh keeps it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter

import run_carry_cycle as rcc
from carry.client import CarryClient, order_request
from carry.testnet_record import ResponseRecorder

REPO = Path(__file__).resolve().parent.parent
API_KEY, API_SECRET = "TESTKEY-abc123", "TESTSECRET-xyz789"


class FakeBybit(BaseAdapter):
    """Answers every request; remembers what was sent (headers included)."""

    def __init__(self, body):
        super().__init__()
        self.body, self.sent = body, []

    def send(self, request, **kw):
        self.sent.append(request)
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = 200, json.dumps(self.body).encode(), \
            request.url, request
        r.headers["Content-Type"] = "application/json"
        r.headers["X-Bapi-Limit-Status"] = "99"           # a response header: not kept either
        return r

    def close(self):
        pass


def _client(tmp_path, body):
    rec = ResponseRecorder(tmp_path / "raw", testnet=True)
    fake = FakeBybit(body)
    rec.mount("https://", fake)
    return CarryClient(api_key=API_KEY, api_secret=API_SECRET, testnet=True, session=rec), fake


def _raw(tmp_path):
    files = list((tmp_path / "raw").glob("raw_*.jsonl"))
    assert len(files) == 1
    return files[0].read_text()


def test_recorder_refuses_mainnet(tmp_path):
    for flag in (False, None, "true", 1):
        with pytest.raises(ValueError, match="testnet only"):
            ResponseRecorder(tmp_path, testnet=flag)


def test_only_the_response_body_is_kept_never_a_header_key_or_signature(tmp_path):
    body = {"retCode": 0, "retMsg": "OK", "result": {
        "list": [{"symbol": "ETHUSDT", "size": "0.03", "side": "Sell", "positionIdx": 0,
                  "adlRankIndicator": 2}], "nextPageCursor": ""}}
    client, fake = _client(tmp_path, body)
    client.get_positions("ETHUSDT")                                    # signed GET
    req = order_request("linear", "ETHUSDT", "Sell", "Market", 0.03, 0.01,
                        "cy-REQUEST-BODY-LINK", reduce_only=True)
    client.create_order(req)                                           # signed POST
    text = _raw(tmp_path)
    rows = [json.loads(l) for l in text.splitlines()]
    assert [r["path"] for r in rows] == ["/v5/position/list", "/v5/order/create"]
    assert all(set(r) == {"ts_ms", "method", "path", "status", "body"} for r in rows)
    assert rows[0]["body"] == body and rows[0]["method"] == "GET"
    # The key and signature headers really went out (the timestamp header is
    # not secret and may equal the record's own ts_ms, so it is not checked).
    keys = [p.headers["X-BAPI-API-KEY"] for p in fake.sent]
    sigs = [p.headers["X-BAPI-SIGN"] for p in fake.sent]
    assert keys == [API_KEY, API_KEY] and len(set(sigs)) == 2
    for secret in [API_KEY, API_SECRET, "X-BAPI", "X-Bapi", "x-bapi", "SIGN", *sigs]:
        assert secret not in text, secret
    assert "cy-REQUEST-BODY-LINK" not in text                          # no request body
    assert "category=linear" not in text and "symbol=ETHUSDT" not in text   # no query


def test_a_non_json_response_is_kept_as_text(tmp_path):
    rec = ResponseRecorder(tmp_path / "raw", testnet=True)

    class Text(FakeBybit):
        def send(self, request, **kw):
            r = super().send(request, **kw)
            r._content = b"<html>bad gateway</html>"
            return r

    rec.mount("https://", Text({}))
    rec.get("https://api-testnet.bybit.com/v5/market/time")
    row = json.loads(_raw(tmp_path))
    assert row["body"] == {"_non_json": "<html>bad gateway</html>"}


def _main_session(monkeypatch, testnet):
    import carry.client
    seen = {}

    class Recorder:
        def __init__(self, api_key=None, api_secret=None, testnet=None, session=None, **kw):
            self.testnet = testnet
            seen["session"] = session

    monkeypatch.setattr(carry.client, "CarryClient", Recorder)
    monkeypatch.setattr(rcc, "run_cycle", lambda *a, **k: ({"cycle_id": "x", "alerts": []}, 0))
    if testnet:
        monkeypatch.setenv("BYBIT_TESTNET", "1")
    rcc.main()
    return seen["session"]


def test_the_testnet_cycle_records_into_its_own_log_dir(isolated_paths, monkeypatch):
    s = _main_session(monkeypatch, testnet=True)
    assert isinstance(s, ResponseRecorder)
    assert s.out_dir == Path("/opt/hermes/logs/carry-testnet/raw")     # carry.testnet.yaml


def test_the_mainnet_cycle_records_nothing(isolated_paths, monkeypatch):
    assert _main_session(monkeypatch, testnet=False) is None


# --- the loop -------------------------------------------------------------------------- #

LOOP = REPO / "scripts" / "carry_testnet_loop.sh"
STUB = r'''#!/usr/bin/env bash
echo "$(basename "$1") ${2:-} ${3:-} BYBIT_TESTNET=${BYBIT_TESTNET:-}" >> "$CALLS"
n=$(grep -c heartbeat "$CALLS")
if [[ $(basename "$1") == heartbeat.py && $n -ge 2 ]]; then touch "$STOP_FILE"; fi
'''


def _loop(tmp_path, **env):
    stub = tmp_path / "py"
    stub.write_text(STUB)
    stub.chmod(0o755)
    home = tmp_path / "hermes"
    base = {"PATH": os.environ["PATH"], "YIELD_PY": str(stub), "YIELD_HERMES_HOME": str(home),
            "LOOP_SLEEP_S": "0", "CALLS": str(tmp_path / "calls"),
            "STOP_FILE": str(home / "state" / "carry-testnet" / "STOP")}
    return subprocess.run(["bash", str(LOOP)], env={**base, **env}, capture_output=True,
                          text=True, timeout=30), tmp_path / "calls", home


def test_loop_runs_cycle_then_heartbeat_until_stop(tmp_path):
    r, calls, home = _loop(tmp_path, BYBIT_TESTNET="1")
    assert r.returncode == 0, r.stderr
    assert calls.read_text().splitlines() == [
        "run_carry_cycle.py   BYBIT_TESTNET=1", "heartbeat.py --system carry BYBIT_TESTNET=1",
        "run_carry_cycle.py   BYBIT_TESTNET=1", "heartbeat.py --system carry BYBIT_TESTNET=1"]
    assert "STOP file found" in r.stdout
    assert (home / "logs" / "carry-testnet").is_dir()


def test_loop_refuses_without_testnet(tmp_path):
    r, calls, _ = _loop(tmp_path)
    assert r.returncode == 2 and "BYBIT_TESTNET=1 is required" in r.stderr
    assert not calls.exists()


def test_loop_refuses_a_leftover_stop_file(tmp_path):
    stop = tmp_path / "hermes" / "state" / "carry-testnet" / "STOP"
    stop.parent.mkdir(parents=True)
    stop.touch()
    r, calls, _ = _loop(tmp_path, BYBIT_TESTNET="1")
    assert r.returncode == 2 and "remove it to start" in r.stderr and not calls.exists()


def test_loop_is_a_transient_unit_never_nohup():
    text = LOOP.read_text()
    assert "systemd-run --uid=hermes" in text and "--unit=yield-carry-testnet" in text
    assert "systemctl stop yield-carry-testnet" in text
    assert "nohup" not in text
    assert os.access(LOOP, os.X_OK)
    deploy_md = (REPO / "DEPLOY.md").read_text()
    section = deploy_md[deploy_md.index("## Carry — testnet"):]
    assert "systemd-run --uid=hermes" in section and "nohup" not in section


def test_deploy_keeps_the_transient_testnet_unit(sandbox):
    sandbox.reply("systemctl.list-unit-files",
                  out="yield-cycle.timer enabled\nyield-carry-testnet.service transient\n")
    r = sandbox.run()
    assert r.returncode == 0, r.stdout
    assert "keep    yield-carry-testnet.service (transient testnet run" in r.stdout
    assert not any("yield-carry-testnet" in " ".join(m) for m in sandbox.systemctl_mutations())


from test_deploy import sandbox  # noqa: E402,F401
