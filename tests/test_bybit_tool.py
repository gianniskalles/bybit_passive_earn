"""Phase 2 — bybit_earn_tool: place-order request shape, signing, errors,
testnet switch.  HTTP is mocked at the requests.Session level."""

import hashlib
import hmac
import json

import pytest
import requests

import bybit_earn_tool as bet
from bybit_earn_tool import BybitAPIError, BybitEarnTool


class FakeResponse:
    def __init__(self, payload=None, status=200, text=None):
        self.payload, self.status_code = payload, status
        self.text = text if text is not None else json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        if self.payload is None:
            raise ValueError("not json")
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []
        self.headers = {}

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.sent.append({"method": method, "url": url, "headers": headers or {}, "data": data})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(result):
    return FakeResponse({"retCode": 0, "retMsg": "OK", "result": result})


def tool_with(*responses, testnet=False):
    s = FakeSession(*responses)
    return BybitEarnTool(api_key="KEY", api_secret="SECRET", testnet=testnet, session=s), s


def test_place_order_request_shape():
    req = bet.place_order_request(order_type="Stake", account_type="UNIFIED", coin="USDT",
                                  product_id="1", amount="5", order_link_id="c1-S-1")
    assert req == {"method": "POST", "path": "/v5/earn/place-order", "body": {
        "category": "FlexibleSaving", "orderType": "Stake", "accountType": "UNIFIED",
        "amount": "5", "coin": "USDT", "productId": "1", "orderLinkId": "c1-S-1"}}


def test_place_order_sends_exact_body_and_valid_signature():
    tool, s = tool_with(ok({"orderId": "o1", "orderLinkId": "c1-S-1"}))
    req = bet.place_order_request("Stake", "UNIFIED", "USDT", "1", "5", "c1-S-1")
    assert tool.place_order(req) == {"orderId": "o1", "orderLinkId": "c1-S-1"}
    [sent] = s.sent
    assert sent["method"] == "POST"
    assert sent["url"] == "https://api.bybit.com/v5/earn/place-order"
    assert json.loads(sent["data"]) == req["body"]
    h = sent["headers"]
    expected = hmac.new(b"SECRET", (h["X-BAPI-TIMESTAMP"] + "KEY" + bet.RECV_WINDOW
                                    + sent["data"]).encode(), hashlib.sha256).hexdigest()
    assert h["X-BAPI-API-KEY"] == "KEY" and h["X-BAPI-SIGN"] == expected


def test_get_signature_covers_query_string():
    tool, s = tool_with(ok({"list": []}))
    tool.get_earn_positions(coin="USDT")
    [sent] = s.sent
    qs = sent["url"].split("?", 1)[1]
    assert qs == "category=FlexibleSaving&coin=USDT"
    h = sent["headers"]
    expected = hmac.new(b"SECRET", (h["X-BAPI-TIMESTAMP"] + "KEY" + bet.RECV_WINDOW + qs).encode(),
                        hashlib.sha256).hexdigest()
    assert h["X-BAPI-SIGN"] == expected


@pytest.mark.parametrize("order_type", ["Stake", "Redeem"])
def test_order_link_id_is_deterministic_and_valid(order_type):
    a = bet.order_link_id("20260925_184351_2e2396", order_type, "1")
    assert a == bet.order_link_id("20260925_184351_2e2396", order_type, "1")
    assert a != bet.order_link_id("20260925_184351_2e2396", order_type, "2")
    long = bet.order_link_id("20260925_184351_2e2396", order_type, "x" * 40)
    for link in (a, long):
        assert 1 <= len(link) <= 36
        assert all(c.isalnum() or c in "_-" for c in link)


def test_legacy_endpoints_are_gone():
    assert not hasattr(BybitEarnTool, "subscribe_earn_product")
    assert not hasattr(BybitEarnTool, "redeem_earn_product")


# --- T2.4: errors are raised, never silently [] ---------------------------- #

@pytest.mark.parametrize("response", [
    FakeResponse({"retCode": 10002, "retMsg": "invalid request", "result": {}}),
    FakeResponse(None, status=502, text="bad gateway"),
    FakeResponse(None, text="<html>"),
    requests.ConnectionError("down"),
    requests.Timeout("slow"),
    ok({}),  # no list at all: schema changed, not "no positions"
])
def test_positions_errors_raise(response):
    tool, _ = tool_with(response)
    with pytest.raises(BybitAPIError):
        tool.get_earn_positions(coin="USDT")


@pytest.mark.parametrize("method,args", [
    ("get_earn_products", ()), ("get_wallet_balance", ("UNIFIED",)),
    ("get_earn_orders", ()), ("get_earn_apr_history", ()),
])
def test_other_reads_raise_on_retcode(method, args):
    tool, _ = tool_with(FakeResponse({"retCode": 10006, "retMsg": "rate limit", "result": {}}))
    kwargs = {"product_id": "1"} if method == "get_earn_apr_history" else {}
    with pytest.raises(BybitAPIError):
        getattr(tool, method)(*args, **kwargs)


def test_place_order_raises_on_retcode():
    tool, _ = tool_with(FakeResponse({"retCode": 180001, "retMsg": "insufficient", "result": {}}))
    with pytest.raises(BybitAPIError):
        tool.place_order(bet.place_order_request("Stake", "UNIFIED", "USDT", "1", "5", "x"))


def test_signed_call_without_credentials_raises(isolated_paths):
    tool = BybitEarnTool(session=FakeSession())
    with pytest.raises(BybitAPIError):
        tool.get_earn_positions()


def test_positions_parse_list():
    tool, _ = tool_with(ok({"list": [{"productId": "1", "coin": "USDT", "amount": "5"}]}))
    assert tool.get_earn_positions(coin="USDT")[0]["amount"] == "5"


# --- T2.3: testnet switch --------------------------------------------------- #

@pytest.mark.parametrize("value,url", [("1", bet.TESTNET_URL), ("true", bet.TESTNET_URL),
                                       ("", bet.MAINNET_URL), ("0", bet.MAINNET_URL),
                                       (None, bet.MAINNET_URL)])
def test_base_url_from_bybit_testnet(isolated_paths, monkeypatch, value, url):
    if value is None:
        monkeypatch.delenv("BYBIT_TESTNET", raising=False)
    else:
        monkeypatch.setenv("BYBIT_TESTNET", value)
    s = FakeSession(ok({"list": []}))
    tool = BybitEarnTool(session=s)
    assert tool.base_url == url
    tool.get_earn_products()
    assert s.sent[0]["url"].startswith(url + "/v5/earn/product")


def test_testnet_urls():
    assert bet.MAINNET_URL == "https://api.bybit.com"
    assert bet.TESTNET_URL == "https://api-testnet.bybit.com"
