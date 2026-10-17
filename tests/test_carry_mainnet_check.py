"""The first real `mainnet_roundtrip.py --check` (Hermes, 17/10) refused for
two READING errors, not account ones. Locked on its exact output
(tests/data/carry_mainnet_check_20261017.json):

1. position/list returned two ETHUSDT rows, both positionIdx 0 and size "0"
   (one a placeholder with seq -1 and empty fields), on an account confirmed
   One-Way. Hedge mode = a row with positionIdx 1 or 2, not several rows.
   positionIdx 0 rows all of size 0 -> FLAT; several rows with a size ->
   unreadable, as before.
2. accountMMRate empty, all USDT in Earn, dust in the UTA (USDT 0.00008,
   TAC 0.075; the app shows "Margin Balance: --"). Empty accountMMRate AND
   empty totalMaintenanceMargin AND totalOrderIM 0 on every coin AND no open
   position -> margin rate 0; wallet balances play no part. Anything else
   stays unreadable. Locked on the real body the recorder wrote (2nd run).
3. --check records the raw response bodies too (raw/ did not exist).
"""

import ast
import copy
import json
from pathlib import Path

import pytest

from carry import exposure as cx
from carry import snapshot as sn
from replay import carry_replay_client

REPO = Path(__file__).resolve().parent.parent
FX = json.loads((REPO / "tests" / "data" / "carry_mainnet_check_20261017.json").read_text())
SYN = json.loads((REPO / "tests" / "data" / "carry" / "SYNTHETIC_snapshot.json").read_text())
ROWS = FX["position_list_ETHUSDT"]
POS_KEY = "/v5/position/list?category=linear&symbol=ETHUSDT"
WALLET_KEY = "/v5/account/wallet-balance?accountType=UNIFIED"
CFG = {"SYMBOLS": ["ETHUSDT"], "SMOOTHING_SETTLEMENTS": 9, "EARN_COIN": "USDT"}


def ok(result):
    return {"retCode": 0, "retMsg": "OK", "result": result, "time": SYN["recorded_at_ms"]}


def payload(rows=ROWS, wallet=None):
    p = copy.deepcopy(SYN)
    p["responses"][POS_KEY] = ok({"list": copy.deepcopy(rows), "nextPageCursor": "",
                                  "category": "linear"})
    if wallet is not None:
        p["responses"][WALLET_KEY] = copy.deepcopy(wallet)          # a full response body
    return p


def take(p):
    return sn.take(carry_replay_client(p), CFG, now_ms=p["recorded_at_ms"])


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def get_positions(self, symbol):
        return self.rows


# --- the fixture is the output, verbatim ------------------------------------------------ #

def test_fixture_rows_are_exactly_the_rows_of_the_refusal():
    text = FX["check_problems"][0]
    start = text.index("hedge-mode rows ") + len("hedge-mode rows ")
    end = text.rindex("; one-way mode is required")
    assert ast.literal_eval(text[start:end]) == ROWS
    assert [r["seq"] for r in ROWS] == [411933266361, -1]
    assert all(r["positionIdx"] == 0 and r["size"] == "0" for r in ROWS)
    assert "wallet-balance: accountMMRate missing" in text
    assert FX["check_problems"][1] == "account unreadable"


# --- 1. position rows -------------------------------------------------------------------- #

def test_the_two_real_rows_are_flat():
    p = sn.read_position(Rows(ROWS), "ETHUSDT")
    assert p.flat and p.size == 0 and p.side == "" and p.adl_rank == 0


def test_the_real_rows_through_the_whole_snapshot_are_flat():
    s = take(payload())
    assert "positions:ETHUSDT" not in s.errors
    assert s.positions["ETHUSDT"].flat


@pytest.mark.parametrize("idx", [1, 2, "1", "2"])
def test_a_hedge_row_is_still_unreadable(idx):
    rows = copy.deepcopy(ROWS)
    rows[1]["positionIdx"] = idx
    with pytest.raises(sn.Unreadable, match="hedge-mode"):
        sn.read_position(Rows(rows), "ETHUSDT")


def test_several_one_way_rows_with_a_size_are_unreadable():
    rows = copy.deepcopy(ROWS)
    rows[0].update(size="0.01", side="Sell", avgPrice="2418.62")
    with pytest.raises(sn.Unreadable, match="2 one-way rows, with a size"):
        sn.read_position(Rows(rows), "ETHUSDT")


def test_a_single_row_with_a_size_reads_as_before():
    row = dict(ROWS[0], size="0.01", side="Sell", avgPrice="2418.62", liqPrice="4800",
               adlRankIndicator=2)
    p = sn.read_position(Rows([row]), "ETHUSDT")
    assert (p.side, p.size, p.avg_price, p.adl_rank) == ("Sell", 0.01, 2418.62, 2)


def test_a_missing_size_is_still_unreadable():
    rows = copy.deepcopy(ROWS)
    rows[1]["size"] = ""
    with pytest.raises(sn.Unreadable, match="size missing"):
        sn.read_position(Rows(rows), "ETHUSDT")


# --- 2. empty margin rates: the real wallet-balance body of the second --check ----------- #

RAW = FX["raw_wallet_balance"]
REAL = RAW["body"]                                    # the response body, as recorded


def wallet_with(**acct_changes):
    w = copy.deepcopy(REAL)
    w["result"]["list"][0].update(acct_changes)
    return w


def with_coin(name, **changes):
    w = copy.deepcopy(REAL)
    for c in w["result"]["list"][0]["coin"]:
        if c["coin"] == name:
            c.update(changes)
    return w


def test_the_recorded_line_is_a_response_body_only():
    assert set(RAW) == {"ts_ms", "method", "path", "status", "body"}
    assert RAW["path"] == "/v5/account/wallet-balance" and RAW["status"] == 200
    acct = REAL["result"]["list"][0]
    assert acct["accountMMRate"] == "" and acct["totalMaintenanceMargin"] == ""
    assert acct["totalEquity"] == "0.00139782"
    assert {c["coin"]: c["walletBalance"] for c in acct["coin"]} == {
        "BTC": "0", "USDT": "0.00008226", "TAC": "0.075", "USDC": "0"}
    assert all(c["totalOrderIM"] == "0" for c in acct["coin"])
    assert "wallet-balance: accountMMRate missing" in FX["check2_problems"][0]


def test_the_real_body_reads_as_no_margin_in_use():
    s = take(payload(wallet=REAL))
    assert "account" not in s.errors
    a = s.account
    assert a.margin_unreported and a.mm_rate == 0.0 and a.im_rate == 0.0
    # balances are read as they are; they play no part in the rule
    assert a.balance("USDT").wallet == 0.00008226 and a.balance("TAC").wallet == 0.075


def test_wallet_balances_play_no_part():
    w = with_coin("USDT", walletBalance="50", equity="50")
    w["result"]["list"][0]["totalEquity"] = "50.0014"
    s = take(payload(wallet=w))
    assert "account" not in s.errors and s.account.mm_rate == 0.0


@pytest.mark.parametrize("wallet", [
    wallet_with(totalMaintenanceMargin="0.5"),                    # margin in use
    wallet_with(accountMMRate="0.01", accountIMRate=""),         # MM reported, IM not
    with_coin("USDT", totalOrderIM="1.2"),                        # an order holds margin
    with_coin("TAC", totalOrderIM=""),                            # unknown
    wallet_with(coin=[]),                                         # no coin rows at all
])
def test_any_sign_of_margin_in_use_stays_unreadable(wallet):
    s = take(payload(wallet=wallet))
    assert s.account is None
    assert "wallet-balance: accountMMRate missing" in s.errors["account"] or \
        "wallet-balance: accountIMRate missing" in s.errors["account"]


def test_empty_mm_rate_with_an_open_position_stays_unreadable():
    rows = [dict(ROWS[0], size="0.01", side="Sell", avgPrice="2418.62", adlRankIndicator=1)]
    s = take(payload(rows=rows, wallet=REAL))
    assert s.account is None
    assert "accountMMRate missing while positions ['ETHUSDT'] are open" in s.errors["account"]


def test_empty_mm_rate_with_an_unread_position_stays_unreadable():
    rows = copy.deepcopy(ROWS)
    rows[0]["positionIdx"] = 1                                   # positions unreadable
    s = take(payload(rows=rows, wallet=REAL))
    assert s.account is None and "accountMMRate missing" in s.errors["account"]


def test_a_reported_mm_rate_is_read_as_before():
    s = take(payload())
    assert not s.account.margin_unreported and s.account.mm_rate == 0.02


# --- TAC in the wallet does not touch ETHUSDT ----------------------------------------------- #

def test_tac_in_the_wallet_does_not_affect_ethusdt(tmp_path):
    from carry.plan import plan_cycle
    from carry_builders import cfg as carry_cfg
    s = take(payload(wallet=REAL))
    assert s.account.balance("ETH").wallet == 0.0                # the ETHUSDT base coin
    assert s.missing_for_entry("ETHUSDT") == []
    plan = plan_cycle(s, carry_cfg(SYMBOLS=["ETHUSDT"]), "NORMAL", {}, "c-tac")
    assert not any("TAC" in a or "FOREIGN" in a for a in plan.alerts), plan.alerts
    assert not any("TAC" in w for w in plan.no_entry.get("ETHUSDT", ())), plan.no_entry
    # and the exposure check sees nothing held on ETHUSDT
    e = cx.check(carry_replay_client(payload(wallet=REAL)), CFG, tmp_path / "b.json", "k" * 64,
                 now_ms=SYN["recorded_at_ms"],
                 take=lambda c, cfg, now_ms=None, private=True: sn.take(c, cfg, now_ms=now_ms,
                                                                        private=private))
    assert e.verdict == cx.FLAT and not any("TAC" in l for l in e.lines), e.lines


# --- the check of 17/10 now passes its exposure step ---------------------------------------- #

def test_the_exposure_check_of_17_10_is_flat(tmp_path):
    p = payload(wallet=REAL)
    e = cx.check(carry_replay_client(p), CFG, tmp_path / "book.json", "k" * 64,
                 now_ms=p["recorded_at_ms"],
                 take=lambda c, cfg, now_ms=None, private=True: sn.take(c, cfg, now_ms=now_ms,
                                                                        private=private))
    assert e.verdict == cx.FLAT, e.lines


# --- 3. --check records the raw bodies -------------------------------------------------------- #

def test_check_records_raw_responses(isolated_paths, monkeypatch):
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    import mainnet_roundtrip as rt
    import carry.client
    from carry.testnet_record import ResponseRecorder
    seen = {}

    class Client:
        testnet = False

        def __init__(self, api_key=None, api_secret=None, testnet=None, session=None):
            seen["session"] = session

    monkeypatch.setattr(carry.client, "CarryClient", Client)
    monkeypatch.setattr(rt.Roundtrip, "checks", lambda self: [])
    assert rt.main(["--check"]) == 0
    assert isinstance(seen["session"], ResponseRecorder)
    assert seen["session"].out_dir == Path("/opt/hermes/logs/carry/raw")
