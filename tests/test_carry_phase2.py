"""CARRY_PLAN Phase 2 — client + snapshot. Every API failure is an error,
never an empty list; each section is fully read or absent with a reason;
the tests are locked on the captures in tests/data/carry/ (today only the
SYNTHETIC one; real testnet captures are picked up automatically)."""

import copy
import json

import pytest
import requests

from bybit_earn_tool import BybitAPIError
from carry import client as cl
from carry import snapshot as sn
from replay import CARRY_DATA_DIR, carry_replay_client

CFG = {"SYMBOLS": ["BTCUSDT", "ETHUSDT"], "SMOOTHING_SETTLEMENTS": 9, "EARN_COIN": "USDT"}
CAPTURES = sorted(p.name for p in CARRY_DATA_DIR.glob("*.json"))
SYN = "SYNTHETIC_snapshot.json"


def load(name=SYN):
    return json.loads((CARRY_DATA_DIR / name).read_text())


def take(payload, **kw):
    return sn.take(carry_replay_client(payload), CFG, now_ms=payload["recorded_at_ms"], **kw)


def with_response(payload, key, fn):
    out = copy.deepcopy(payload)
    fn(out["responses"][key])
    return out


# --- captures ---------------------------------------------------------------- #

def test_there_is_at_least_one_carry_capture():
    assert SYN in CAPTURES


@pytest.mark.parametrize("name", CAPTURES)
def test_capture_gives_a_complete_fresh_snapshot(name):
    s = take(load(name))
    assert dict(s.errors) == {} and dict(s.stale) == {}
    assert set(s.markets) == set(s.positions) == set(CFG["SYMBOLS"])
    assert s.account is not None and s.open_orders is not None and s.earn is not None
    assert not s.region_restricted
    for sym in CFG["SYMBOLS"]:
        assert s.missing_for_entry(sym) == []
        m = s.markets[sym]
        assert m.perp.trading and m.spot.trading
        assert m.funding_interval_min and m.funding_interval_min > 0
        assert len(m.settled) >= CFG["SMOOTHING_SETTLEMENTS"]


def test_synthetic_values_parse_as_documented():
    s = take(load())
    btc, eth = s.markets["BTCUSDT"], s.markets["ETHUSDT"]
    assert btc.funding_interval_min == 480 and btc.previous_interval_min == 480
    assert btc.predicted_rate == 0.0001 and btc.settled[-1] == (1789977600000, 0.0001)
    assert btc.perp.qty_step == 0.001 and btc.spot.qty_step == 0.000001
    assert btc.basis_bps == pytest.approx((65010.05 - 64995.005) / 64995.005 * 1e4)
    assert btc.spread_bps == pytest.approx(0.1 / 65010.05 * 1e4)
    assert s.positions["BTCUSDT"].flat
    p = s.positions["ETHUSDT"]
    assert (p.side, p.size, p.adl_rank, p.liq_price) == ("Sell", 0.4, 2, 4900.0)
    a = s.account
    assert a.cross_margin and a.mm_rate == 0.02
    assert a.balance("USDT").wallet == 1100 and a.balance("BTC").wallet == 0
    assert a.fees[("spot", "ETHUSDT")] == (0.001, 0.001)
    assert a.collateral["ETH"].active and a.collateral["BTC"].ratio == 0.95
    e = s.earn
    assert (e.product_id, e.apr, e.staked) == ("1", pytest.approx(0.0173), 900.0)
    assert e.order("cy-redeem-1").state == sn.EARN_SUCCESS and e.unfinished == ()


# --- failures stay in their section ----------------------------------------------- #

SECTION_OF = {
    "/v5/market/tickers?category=spot&symbol=BTCUSDT": "market:BTCUSDT",
    "/v5/market/funding/history?category=linear&symbol=ETHUSDT": "market:ETHUSDT",
    "/v5/position/list?category=linear&symbol=ETHUSDT": "positions:ETHUSDT",
    "/v5/account/wallet-balance?accountType=UNIFIED": "account",
    "/v5/account/collateral-info?currency=BTC": "account",
    "/v5/account/fee-rate?category=spot&symbol=ETHUSDT": "account",
    "/v5/order/realtime?category=spot": "orders",
    "/v5/earn/order?category=FlexibleSaving": "earn",
    "/v5/earn/position?category=FlexibleSaving&coin=USDT": "earn",
}


@pytest.mark.parametrize("key,section", SECTION_OF.items())
def test_api_error_marks_only_its_section(key, section):
    payload = with_response(load(), key, lambda r: r.update(retCode=10001, retMsg="params error"))
    s = take(payload)
    assert set(s.errors) == {section}
    assert "retCode=10001" in s.errors[section]
    sym = section.split(":")[1] if ":" in section else "BTCUSDT"
    assert s.missing_for_entry(sym)


@pytest.mark.parametrize("key,section", SECTION_OF.items())
def test_missing_list_is_an_error_not_empty(key, section):
    payload = with_response(load(), key, lambda r: r["result"].pop("list"))
    assert section in take(payload).errors


@pytest.mark.parametrize("key,mutation,section", [
    ("/v5/market/instruments-info?category=linear&symbol=BTCUSDT",
     lambda r: r["result"]["list"][0].pop("fundingInterval"), "market:BTCUSDT"),
    ("/v5/market/tickers?category=linear&symbol=ETHUSDT",
     lambda r: r["result"]["list"][0].update(fundingRate=""), "market:ETHUSDT"),
    ("/v5/position/list?category=linear&symbol=ETHUSDT",
     lambda r: r["result"]["list"][0].pop("adlRankIndicator"), "positions:ETHUSDT"),
    ("/v5/position/list?category=linear&symbol=ETHUSDT",
     lambda r: r["result"]["list"][0].update(side=""), "positions:ETHUSDT"),
    ("/v5/account/wallet-balance?accountType=UNIFIED",
     lambda r: r["result"]["list"][0].update(accountMMRate=""), "account"),
    ("/v5/account/info", lambda r: r["result"].pop("marginMode"), "account"),
    ("/v5/account/collateral-info?currency=ETH",
     lambda r: r["result"]["list"][0].pop("collateralRatio"), "account"),
    ("/v5/account/collateral-info?currency=ETH",
     lambda r: r["result"]["list"][0].update(collateralSwitch="true"), "account"),
    ("/v5/earn/product?category=FlexibleSaving&coin=USDT",
     lambda r: r["result"]["list"][0].update(estimateApr="n/a"), "earn"),
    ("/v5/earn/position?category=FlexibleSaving&coin=USDT",
     lambda r: r["result"]["list"][0].update(amount=None), "earn"),
    ("/v5/earn/order?category=FlexibleSaving",
     lambda r: r["result"]["list"][0].pop("status"), "earn"),
])
def test_unreadable_field_is_an_error_never_a_default(key, mutation, section):
    s = take(with_response(load(), key, mutation))
    assert section in s.errors
    assert set(s.errors) == {section}


def test_hedge_mode_positions_are_refused():
    def two_rows(r):
        row = r["result"]["list"][0]
        r["result"]["list"] = [dict(row, positionIdx=1), dict(row, positionIdx=2, side="Buy")]
    s = take(with_response(load(), "/v5/position/list?category=linear&symbol=ETHUSDT", two_rows))
    assert "hedge-mode" in s.errors["positions:ETHUSDT"]


def test_empty_position_list_is_flat_because_the_call_succeeded():
    s = take(with_response(load(), "/v5/position/list?category=linear&symbol=BTCUSDT",
                           lambda r: r["result"].update(list=[])))
    assert s.positions["BTCUSDT"].flat and "positions:BTCUSDT" not in s.errors


def test_http_failure_is_an_error(monkeypatch):
    payload = load()

    class Down:
        headers = {}

        def request(self, *a, **k):
            raise requests.ConnectionError("down")

    s = sn.take(cl.CarryClient(api_key="k", api_secret="s", testnet=False, session=Down()),
                CFG, now_ms=payload["recorded_at_ms"])
    assert s.markets == {} and s.account is None and s.earn is None and s.open_orders is None
    assert {"market:BTCUSDT", "market:ETHUSDT", "account", "orders", "earn"} <= set(s.errors)


def test_no_credentials_means_private_sections_unread():
    payload = load()
    c = carry_replay_client(payload)
    c.api_key = c.api_secret = None
    s = sn.take(c, CFG, now_ms=payload["recorded_at_ms"])
    assert set(s.markets) == {"BTCUSDT", "ETHUSDT"}
    assert "credentials" in s.errors["account"] and "credentials" in s.errors["earn"]


def test_market_only_snapshot_reports_private_sections_not_read():
    s = take(load(), private=False)
    assert s.errors["account"] == "not read" and s.missing_for_entry("BTCUSDT")


# --- region restriction (R1) ----------------------------------------------------- #

def test_region_restriction_ret_code_sets_the_flag():
    payload = with_response(load(), "/v5/account/wallet-balance?accountType=UNIFIED",
                            lambda r: r.update(retCode=10024, retMsg="Compliance rules triggered"))
    s = take(payload)
    assert s.region_restricted and "REGION_RESTRICTED" in s.missing_for_entry("ETHUSDT")


def test_cloudfront_country_block_is_a_region_restriction():
    err = BybitAPIError("x: HTTP error: 403", http_status=403,
                        body="The Amazon CloudFront distribution is configured to block access "
                             "from your country.")
    assert cl.is_region_restricted(err)
    assert not cl.is_region_restricted(BybitAPIError("x", ret_code=10001, body="params error"))
    assert not cl.is_region_restricted(ValueError("from your country"))


# --- staleness (R27) ------------------------------------------------------------- #

def test_settlement_time_in_the_past_is_stale():
    payload = load()
    s = sn.take(carry_replay_client(payload), CFG, now_ms=1790006400000 + 1)
    assert "market:BTCUSDT" in s.stale and s.missing_for_entry("BTCUSDT")


def test_funding_history_lagging_the_ticker_is_stale():
    key = "/v5/market/funding/history?category=linear&symbol=ETHUSDT"
    s = take(with_response(load(), key, lambda r: r["result"].update(list=r["result"]["list"][2:])))
    assert "lags" in s.stale["market:ETHUSDT"]
    assert "market:BTCUSDT" not in s.stale


# --- earn orders (decision 13.4) ---------------------------------------------- #

@pytest.mark.parametrize("status,state", [
    ("Success", sn.EARN_SUCCESS), ("SUCCESS", sn.EARN_SUCCESS), ("Fail", sn.EARN_FAIL),
    ("Pending", sn.EARN_PENDING), ("PartiallyProcessed", sn.EARN_PENDING),
    ("Processing", sn.EARN_PENDING), ("Something", sn.EARN_UNKNOWN),
])
def test_only_success_completes_a_redeem(status, state):
    o = sn.EarnOrder("1", "cy-r", "Redeem", status, 10.0, 1)
    assert o.state == state


def test_unfinished_earn_orders_are_listed():
    payload = with_response(load(), "/v5/earn/order?category=FlexibleSaving",
                            lambda r: r["result"]["list"][0].update(status="Pending"))
    s = take(payload)
    assert [o.order_link_id for o in s.earn.unfinished] == ["cy-redeem-1"]


# --- foreign activity (R2) -------------------------------------------------------- #

def test_order_without_our_prefix_is_foreign():
    order = {"symbol": "ETHUSDT", "orderId": "o1", "orderLinkId": "", "side": "Buy", "qty": "0.1",
             "orderStatus": "New"}
    payload = with_response(load(), "/v5/order/realtime?category=spot",
                            lambda r: r["result"].update(list=[order, dict(order, orderId="o2",
                                                                            orderLinkId="cy-x")]))
    s = take(payload)
    assert [o.order_id for o in s.foreign_orders] == ["o1"]


# --- client mechanics ---------------------------------------------------------------- #

class Scripted:
    """A session answering from a function of (path, params)."""

    def __init__(self, fn):
        self.fn, self.headers, self.calls = fn, {}, []

    def request(self, method, url, headers=None, data=None, timeout=None):
        import urllib.parse
        u = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((u.path, params))
        body = self.fn(u.path, params)

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return body
        return R()


def ok(result):
    return {"retCode": 0, "retMsg": "OK", "result": result}


def client(fn):
    return cl.CarryClient(api_key="k", api_secret="s", testnet=False, session=Scripted(fn))


def test_pagination_follows_the_cursor():
    pages = {"": ({"symbol": "A"}, "c1"), "c1": ({"symbol": "B"}, "")}

    def fn(path, params):
        row, nxt = pages[params.get("cursor", "")]
        return ok({"list": [row], "nextPageCursor": nxt})
    assert [r["symbol"] for r in client(fn).get_open_orders("spot")] == ["A", "B"]


def test_endless_pagination_is_an_error_not_a_partial_list():
    c = client(lambda p, q: ok({"list": [{"x": 1}], "nextPageCursor": "again"}))
    with pytest.raises(BybitAPIError, match="pages"):
        c.get_positions("BTCUSDT")


def test_find_order_checks_open_then_history():
    def fn(path, params):
        if path == "/v5/order/realtime":
            return ok({"list": []})
        return ok({"list": [{"orderLinkId": params["orderLinkId"], "orderStatus": "Filled"}]})
    c = client(fn)
    assert c.find_order("linear", "cy-1")["orderStatus"] == "Filled"
    assert [p for p, _ in c.session.calls] == ["/v5/order/realtime", "/v5/order/history"]


def test_find_order_none_only_when_both_reads_succeed_empty():
    assert client(lambda p, q: ok({"list": []})).find_order("spot", "cy-1") is None
    failing = client(lambda p, q: ok({"list": []}) if p == "/v5/order/realtime"
                     else {"retCode": 10016, "retMsg": "server error"})
    with pytest.raises(BybitAPIError):
        failing.find_order("spot", "cy-1")


def test_recent_funding_is_oldest_first_and_capped():
    rows = [{"fundingRate": str(i), "fundingRateTimestamp": str(1000 - i)} for i in range(5)]
    got = client(lambda p, q: ok({"list": rows})).get_recent_funding("BTCUSDT", 3)
    assert got == [(998, 2.0), (999, 1.0), (1000, 0.0)]


def test_response_key_is_stable():
    a = cl.response_key("https://x/v5/market/tickers?symbol=BTCUSDT&category=spot&limit=5")
    b = cl.response_key("https://x/v5/market/tickers?category=spot&symbol=BTCUSDT&cursor=zz")
    assert a == b == "/v5/market/tickers?category=spot&symbol=BTCUSDT"


def test_snapshot_is_immutable():
    s = take(load())
    with pytest.raises(Exception):
        s.markets["X"] = None
    with pytest.raises(Exception):
        s.taken_ms = 0
