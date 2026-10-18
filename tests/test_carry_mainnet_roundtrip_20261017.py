"""Audit of the supervised mainnet round trip, 17-18/10 (ETHUSDT, 0.01 ETH).

Locked on what Bybit actually answered: the response bodies the recorder wrote
(calibration/mainnet_roundtrip_20261017/raw/), the production ledger and the
cycle records of the run. Replays go through the REAL CarryClient and
snapshot.take, served the recorded bodies in the order they were recorded.

The audit itself: calibration/mainnet_roundtrip_20261017/AUDIT.md.

The defects the audit found were fixed after Giannis approved them (18/10;
tests/test_carry_audit_fixes.py). What the RUN recorded stays as it was
(the stored ledger rows and report, the cycle records); the same answers
replayed through the fixed code read and book what the wallet shows.
"""

import json
import re
import urllib.parse
from pathlib import Path

import pytest

from carry import ledger
from carry import snapshot as sn
from carry.client import CarryClient
from carry.execute import ceil_to, fee_in_base, floor_to

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / "calibration" / "mainnet_roundtrip_20261017"
RAW = [json.loads(l) for f in sorted((RUN / "raw").glob("raw_*.jsonl")) for l in f.open()]
LEDGER = [json.loads(l) for f in sorted((RUN / "ledger").glob("*.jsonl")) for l in f.open()]
RECORDS = [json.loads(l) for f in sorted((RUN / "records").glob("*.jsonl")) for l in f.open()]
REPORT = json.loads((RUN / "roundtrip_20261018T002704Z.json").read_text())
STATE = json.loads((RUN / "roundtrip_state.json").read_text())
CFG = {"SYMBOLS": ["ETHUSDT"], "SMOOTHING_SETTLEMENTS": 9, "EARN_COIN": "USDT"}

T_ENTRY = 1792265756717         # the perp orderbook read before the entry
T_HOLD = 1792266060880          # a hold cycle's wallet-balance
T_EXIT = 1792282294049          # the spot orderbook read before the exit
T_SETTLEMENT = 1792281600000


def bodies(path, since=0, until=10 ** 14):
    return [r for r in RAW if r["path"] == path and since <= r["ts_ms"] <= until]


def filled(link):
    """The final state of one of our orders, as /v5/order/realtime returned it."""
    for r in bodies("/v5/order/realtime"):
        for o in r["body"]["result"]["list"]:
            if o["orderLinkId"] == link and o["orderStatus"] == "Filled":
                return o
    raise AssertionError(f"no Filled order {link}")


def wallet_at(ts):
    """The UTA balances of the last wallet-balance read at or before ts."""
    r = [r for r in bodies("/v5/account/wallet-balance") if r["ts_ms"] <= ts][-1]
    return {c["coin"]: float(c["walletBalance"]) for c in r["body"]["result"]["list"][0]["coin"]}


def live_rows():
    """The ledger rows of the round trip (the DRY_RUN timer writes paper
    earn_interest rows into the same files; they carry no live cycle)."""
    from datetime import datetime
    t0 = int(datetime.fromisoformat(REPORT["timeline"][0][1]).timestamp() * 1000)
    return [r for r in ledger.read(RUN, since_ms=t0) if r["type"] != "earn_interest"]


ORDERS = [r for r in LEDGER if r["type"] == "order" and r["leg"] in ("perp", "spot")]
PERP_IN, SPOT_IN, SPOT_OUT, PERP_OUT = ORDERS


# --- replaying one recorded cycle through the real client ------------------------------- #

class _Resp:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class Sequence:
    """Serves the recorded bodies in recorded order; each request must be
    the path that was recorded next (the recorder kept no query string)."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.headers = {}

    def request(self, method, url, **kw):
        row = self.rows.pop(0)
        assert row["path"] == urllib.parse.urlparse(url).path, (row["path"], url)
        return _Resp(row["body"])


def cycle_from(ts):
    starts = [i for i, r in enumerate(RAW) if r["path"] == "/v5/market/instruments-info"
              and r["body"]["result"].get("category") == "linear" and r["ts_ms"] <= ts]
    return RAW[starts[-1]:]


def snapshot_at(ts):
    rows = cycle_from(ts)
    client = CarryClient(api_key="replay", api_secret="replay", testnet=False,
                         session=Sequence(rows))
    return sn.take(client, CFG, now_ms=rows[0]["ts_ms"] + 5000)


# --- the recording ------------------------------------------------------------------------ #

def test_the_recording_is_response_bodies_only():
    text = "".join(f.read_text() for f in (RUN / "raw").glob("raw_*.jsonl"))
    assert not re.search(r"X-BAPI|api_key|apiKey|signature|secret", text, re.I)
    assert all(set(r) == {"ts_ms", "method", "path", "status", "body"} for r in RAW)
    assert "?" not in "".join(r["path"] for r in RAW)               # no query strings


def test_every_response_was_ok():
    """1498 responses, all HTTP 200 / retCode 0: 110017, 110072 and the
    region refusal (R1) were not met and stay unverified (§12)."""
    assert len(RAW) == 1498
    assert {(r["status"], r["body"]["retCode"]) for r in RAW} == {(200, 0)}


# --- orders: the ledger is what Bybit says ------------------------------------------------- #

@pytest.mark.parametrize("row", ORDERS, ids=["perp-in", "spot-in", "spot-out", "perp-out"])
def test_ledger_order_rows_equal_bybit_fills(row):
    o = filled(row["order_link_id"])
    assert row["avg_price"] == float(o["avgPrice"])
    assert row["filled"] == float(o["cumExecQty"])
    assert row["fee"] == float(o["cumExecFee"])
    assert row["fee_detail"] == o["cumFeeDetail"]
    assert row["side"] == o["side"] and row["order_type"] == o["orderType"]


def test_fee_rates_are_the_account_fee_rates_and_the_config():
    """The fills were charged exactly the fee-rate the account reports:
    perp taker 0.055 % (both legs), spot 0.1 % — as config/carry.yaml has it."""
    rates = {json.dumps(r["body"]["result"]["list"]) for r in bodies("/v5/account/fee-rate")}
    assert rates == {
        '[{"symbol": "ETHUSDT", "takerFeeRate": "0.00055", "makerFeeRate": "0.0002", '
        '"rpiMakerFeeRate": ""}]',
        '[{"symbol": "ETHUSDT", "takerFeeRate": "0.001", "makerFeeRate": "0.001", '
        '"rpiMakerFeeRate": "0.001"}]'}
    for row, rate in ((PERP_IN, 0.00055), (PERP_OUT, 0.00055), (SPOT_OUT, 0.001)):
        assert row["fee"] == pytest.approx(row["filled"] * row["avg_price"] * rate, abs=1e-8)
    assert SPOT_IN["fee"] == pytest.approx(SPOT_IN["filled"] * 0.001, abs=1e-12)  # in ETH
    import yaml
    cfg = yaml.safe_load((REPO / "config" / "carry.yaml").read_text())
    assert (cfg["PERP_TAKER_FEE"], cfg["SPOT_TAKER_FEE"]) == (0.00055, 0.001)


def test_r22_the_spot_buy_fee_is_taken_in_the_bought_coin():
    """§12 / R22, closed: a spot BUY is charged in the base coin
    (cumFeeDetail {"ETH": ...}), a spot SELL and both perp legs in USDT."""
    buy, sell = filled(SPOT_IN["order_link_id"]), filled(SPOT_OUT["order_link_id"])
    assert buy["cumFeeDetail"] == {"ETH": "0.00001002"}
    assert sell["cumFeeDetail"] == {"USDT": "0.0239869"}
    assert filled(PERP_IN["order_link_id"])["cumFeeDetail"] == {"USDT": "0.01328674"}
    assert fee_in_base(buy, "ETH") == (0.00001002, True)
    assert fee_in_base(sell, "ETH") == (0.0, True)


def test_the_spot_buy_gross_up_and_the_dust_it_leaves():
    """need 0.01 / (1 - 0.1 %) rounded up to the 0.00001 step = 0.01002;
    received 0.01002 - 0.00001002 = 0.01000998 (the wallet shows exactly
    that); the exit sells the floor, 0.01; 0.00000998 ETH stays, just under
    the spot minimum 0.00001."""
    step = min_qty = 0.00001
    assert ceil_to(0.01 / (1 - 0.001), step) == 0.01002 == SPOT_IN["filled"]
    received = 0.01002 - 0.00001002
    assert wallet_at(T_HOLD)["ETH"] == pytest.approx(received, abs=1e-12) == 0.01000998
    assert floor_to(0.01000998, step) == 0.01 == SPOT_OUT["filled"]
    assert wallet_at(1792282608608)["ETH"] == 0.00000998 < min_qty
    # A second round trip of the same size would leave the same again, above
    # the minimum (AUDIT.md, finding 4) — fixed: the remainder is the book's
    # dust_qty, the next entry buys that much less (test_carry_audit_fixes).
    assert 2 * 0.00000998 > min_qty


def test_entry_and_exit_were_at_the_touch():
    """Every limit price was the best level of the book read just before
    (ref_price = avg_price, slippage 0 by construction); against the mid,
    each leg paid half of a 0.01 spread."""
    books = bodies("/v5/market/orderbook")
    perp_in, spot_in, spot_out = (b["body"]["result"] for b in books)
    assert float(perp_in["b"][0][0]) == PERP_IN["avg_price"] == 2415.77
    assert float(spot_in["a"][0][0]) == SPOT_IN["avg_price"] == 2416.96
    assert float(spot_out["b"][0][0]) == SPOT_OUT["avg_price"] == 2398.69
    lin = [r for r in bodies("/v5/market/tickers", T_EXIT - 10_000, T_EXIT)
           if r["body"]["result"]["category"] == "linear"][-1]["body"]["result"]["list"][0]
    assert float(lin["ask1Price"]) == PERP_OUT["avg_price"] == 2397.41    # Market at the ask
    for row in ORDERS:
        assert row["ref_price"] == row["avg_price"] and row["slippage_bps"] == 0


# --- funding: the transaction log ---------------------------------------------------------- #

SETTLEMENT = [x for r in bodies("/v5/account/transaction-log")
              for x in r["body"]["result"]["list"]]


def test_the_settlement_row_as_bybit_sent_it():
    """§12, closed: type SETTLEMENT, `change` > 0 = received. A short with a
    positive rate (0.00001962 at 00:00) received qty x tradePrice x rate."""
    assert len(SETTLEMENT) == 1
    s = SETTLEMENT[0]
    assert (s["type"], s["symbol"], s["side"], s["size"]) == ("SETTLEMENT", "ETHUSDT", "Sell",
                                                             "-0.01")
    assert int(s["transactionTime"]) == T_SETTLEMENT
    assert s["change"] == s["funding"] == "0.00047121" and s["fee"] == "0"
    assert s["feeRate"] == "-0.00002"           # rounded, opposite sign: never read
    rate = [r for r in bodies("/v5/market/funding/history")][-1]["body"]["result"]["list"][0]
    assert (rate["fundingRate"], int(rate["fundingRateTimestamp"])) == ("0.00001962", T_SETTLEMENT)
    assert round(0.01 * float(s["tradePrice"]) * 0.00001962, 8) == float(s["change"])
    # and the cash balance it reports is the wallet's after it
    assert float(s["cashBalance"]) == wallet_at(1792281643878)["USDT"]


def test_realized_funding_reads_the_real_row_into_the_ledger_row():
    import run_carry_cycle as rc

    class Client:
        def get_transaction_log(self, start_ms, end_ms, currency, type_=None):
            assert (currency, type_) == ("USDT", "SETTLEMENT")
            return SETTLEMENT

    rows = rc.realized_funding_live(Client(), T_SETTLEMENT - 1, T_SETTLEMENT + 60_000)
    led = [r for r in LEDGER if r["type"] == "funding"]
    assert len(led) == 1
    assert rows == [{k: led[0][k] for k in ("type", "symbol", "ts_ms", "amount", "source")}]


# --- the wallet: every USDT accounted for ------------------------------------------------- #

def test_the_wallet_moves_exactly_by_the_fills():
    before, held, after = wallet_at(T_ENTRY), wallet_at(T_HOLD), wallet_at(T_EXIT)
    spot_in = filled(SPOT_IN["order_link_id"])
    # entry: the spot buy's value and the perp fee left the UTA (the perp
    # sale itself moves no cash)
    assert held["USDT"] - before["USDT"] == pytest.approx(
        -float(spot_in["cumExecValue"]) - PERP_IN["fee"], abs=1e-8)
    # exit: spot proceeds - spot fee + realised perp PnL - close fee
    out = wallet_at(1792282608608)
    perp_pnl = (PERP_IN["avg_price"] - PERP_OUT["avg_price"]) * 0.01
    assert out["USDT"] - after["USDT"] == pytest.approx(
        SPOT_OUT["filled"] * SPOT_OUT["avg_price"] - SPOT_OUT["fee"] + perp_pnl - PERP_OUT["fee"],
        abs=1e-8)
    assert out["USDT"] == 29.23265487


def test_earn_orders_and_positions():
    """§12, closed for Flexible Savings: orderType Redeem/Stake, status
    Success at once, orderValue a string with 2 decimals, our orderLinkId
    echoed, createdAt in ms at whole seconds."""
    orders = {o["orderId"]: o for r in bodies("/v5/earn/order")
              for o in r["body"]["result"]["list"]}
    redeem = orders["bdfba02e-2e34-462b-b1b2-b30e6a0ea7dd"]
    stake = orders["90af615b-95cb-4045-9d68-590c5b714fe9"]
    assert (redeem["orderType"], redeem["status"], redeem["orderValue"]) == ("Redeem", "Success",
                                                                           "29.33")
    assert (stake["orderType"], stake["status"], stake["orderValue"]) == ("Stake", "Success",
                                                                        "29.23")
    earn = {r["order_link_id"]: r["filled"] for r in LEDGER if r.get("leg") == "earn"}
    assert earn == {redeem["orderLinkId"]: 29.33, stake["orderLinkId"]: 29.23}
    assert redeem["createdAt"] == redeem["updatedAt"] and redeem["createdAt"].endswith("000")
    amounts = [r["body"]["result"]["list"][0]["amount"] for r in bodies("/v5/earn/position")]
    assert (amounts[0], amounts[-1]) == ("57.2714", "57.1714")
    assert "27.9414" in amounts                                       # 57.2714 - 29.33
    last = bodies("/v5/earn/position")[-1]["body"]["result"]["list"][0]
    assert last["effectiveAmount"] == "27.9414"           # the 29.23 not yet earning


def test_the_whole_cost_of_the_round_trip():
    """Earn -0.1000, UTA USDT +0.00257261, 0.00000998 ETH kept (at the exit
    bid): -0.07348846 USDT, to the 1e-8."""
    eth_after = wallet_at(1792282916416)["ETH"]
    usdt = wallet_at(1792282916416)["USDT"] - wallet_at(T_ENTRY - 400_000)["USDT"]
    change = -0.1 + usdt + eth_after * SPOT_OUT["avg_price"]
    assert round(change, 8) == -0.07348846
    # the same, from the parts
    fees = sum(ledger.fee_usdt(r) for r in ORDERS)
    hedged = (PERP_IN["avg_price"] - PERP_OUT["avg_price"]) * 0.01 + \
        (SPOT_OUT["avg_price"] - SPOT_IN["avg_price"]) * 0.01
    dust = 0.00000998 * (SPOT_OUT["avg_price"] - SPOT_IN["avg_price"])
    funding = float(SETTLEMENT[0]["change"])
    assert round(fees, 8) == 0.07467734
    assert round(hedged, 8) == 0.0009
    assert round(-fees + hedged + dust + funding, 8) == round(change, 8)


# --- the ledger and the report ------------------------------------------------------------ #

def test_the_report_summary_is_the_ledger_summary():
    s = ledger.summary(live_rows())
    for k, v in REPORT["summary"].items():
        assert s[k] == pytest.approx(v, abs=1e-12), k
    rt = [r for r in LEDGER if r["type"] == "round_trip"][0]
    assert rt["pnl"] == pytest.approx(-0.0474392, abs=1e-9) and rt["orders"] == 4


def test_where_the_ledger_net_differs_from_the_wallet():
    """-0.12164533 (ledger) vs -0.07348846 (wallet): the spot buy's ETH fee
    is in `fees` AND, as part of the coin paid for, in round_trip.pnl
    (0.02421794); the 0.00000998 ETH still held counts as spent (0.02393893)."""
    eth_fee_usdt = ledger.fee_usdt(SPOT_IN)
    dust_value = 0.00000998 * SPOT_OUT["avg_price"]
    assert round(eth_fee_usdt, 8) == 0.02421794
    assert round(REPORT["summary"]["net"] + eth_fee_usdt + dust_value, 8) == -0.07348846


def test_the_ledger_net_is_the_wallet_change():
    """Finding 2, fixed: the round trip booked again from the same order rows."""
    rows = [r for r in live_rows() if r["type"] != "round_trip"]
    rt = ledger.round_trip("ETHUSDT", rows, PERP_OUT["ts_ms"])
    assert rt["kept_qty"] == pytest.approx(0.00000998, abs=1e-14)
    assert round(ledger.summary(rows + [rt])["net"], 8) == -0.07348846


# --- the account as the snapshot reads it -------------------------------------------------- #

def test_isolated_margin_and_eth_collateral_off_before_the_ui_change():
    """The --check runs (18:52-19:07) saw ISOLATED_MARGIN and ETH collateral
    off, and refused; from 19:26 on REGULAR_MARGIN and ETH on, to the end."""
    modes = [(r["ts_ms"], r["body"]["result"]["marginMode"]) for r in bodies("/v5/account/info")]
    switch = [(r["ts_ms"], r["body"]["result"]["list"][0]["collateralSwitch"])
              for r in bodies("/v5/account/collateral-info")]
    t = 1792265174875
    assert {m for ts, m in modes if ts < t} == {"ISOLATED_MARGIN"}
    assert {m for ts, m in modes if ts >= t} == {"REGULAR_MARGIN"}
    assert {s for ts, s in switch if ts < t} == {False}
    assert {s for ts, s in switch if ts >= t} == {True}


def test_an_empty_mm_rate_is_isolated_margin():
    """accountMMRate was "" in every read under ISOLATED_MARGIN (18 of 18),
    and never under REGULAR_MARGIN: "0" when flat, "0.0036" with the short."""
    seen, mode = set(), None
    for r in RAW:
        if r["path"] == "/v5/account/info":
            mode = r["body"]["result"]["marginMode"]
        elif r["path"] == "/v5/account/wallet-balance":
            seen.add((mode, r["body"]["result"]["list"][0]["accountMMRate"]))
    assert seen == {("ISOLATED_MARGIN", ""), ("REGULAR_MARGIN", "0"),
                    ("REGULAR_MARGIN", "0.0036")}


def test_the_hold_snapshot_reads_the_account():
    s = snapshot_at(T_HOLD)
    a = s.account
    assert a.cross_margin and a.margin_mode == "REGULAR_MARGIN"
    assert a.mm_rate == 0.0036 and not a.margin_unreported
    assert a.collateral["ETH"].active and a.collateral["ETH"].ratio == 0.95
    assert a.balance("ETH").wallet == 0.01000998
    assert a.fees[("linear", "ETHUSDT")] == (0.00055, 0.0002)
    assert a.fees[("spot", "ETHUSDT")] == (0.001, 0.001)
    assert s.earn.staked == 27.9414


def test_the_hold_position_list_is_a_real_row_and_the_placeholder():
    """With the short open, position/list came in two pages: the real row
    (size 0.01) and the seq -1 placeholder (size 0) of the --check."""
    pages = [r["body"]["result"] for r in bodies("/v5/position/list", T_HOLD - 2000, T_HOLD)]
    assert [p["nextPageCursor"] != "" for p in pages] == [True, False]
    real, placeholder = pages[0]["list"][0], pages[1]["list"][0]
    assert (real["side"], real["size"], real["avgPrice"], real["seq"]) == ("Sell", "0.01",
                                                                          "2415.77", 461374389794)
    assert (placeholder["size"], placeholder["seq"], placeholder["avgPrice"]) == ("0", -1, "")


def test_the_position_was_unreadable_in_every_cycle_it_was_open():
    """What the run did (AUDIT.md finding 1): 55 of the 60 cycles could not
    read the short, and no cycle raised an alert about it."""
    rt = [r for r in RECORDS if r.get("roundtrip")]
    assert len(rt) == 60
    blind = [r for r in rt if "positions:ETHUSDT" in r["snapshot_errors"]]
    assert len(blind) == 55
    assert all("2 one-way rows, with a size" in r["snapshot_errors"]["positions:ETHUSDT"]
               for r in blind)
    assert all(not r["alerts"] for r in rt)
    assert "positions:ETHUSDT" not in snapshot_at(T_HOLD).errors   # the fixed code reads it


def test_the_hold_snapshot_reads_the_short():
    """Finding 1, fixed: the placeholder is ignored."""
    p = snapshot_at(T_HOLD).positions["ETHUSDT"]
    assert (p.side, p.size, p.avg_price, p.adl_rank) == ("Sell", 0.01, 2415.77, 2)


def test_every_hold_cycle_now_reads_the_short_and_plans_no_alarm():
    """Every cycle of the hold replayed: the short reads, the plan with the
    book of the run holds, with no POSITION_UNREADABLE and no exit."""
    from carry.plan import SymbolBook, plan_cycle
    from carry_builders import cfg
    book = {"ETHUSDT": SymbolBook(status="OPEN", entered_ms=STATE["entered_ms"], perp_qty=0.01,
                                  spot_qty=0.01000998, entry_times=(STATE["entered_ms"],),
                                  collateral_ratio=0.95)}
    starts = [r["ts_ms"] for r in RAW if r["path"] == "/v5/market/instruments-info"
              and r["body"]["result"].get("category") == "linear"
              and STATE["entered_ms"] + 60_000 < r["ts_ms"] < T_EXIT - 60_000]
    assert len(starts) >= 50
    for ts in starts[::10]:
        snap = snapshot_at(ts)
        assert snap.positions["ETHUSDT"].size == 0.01, ts
        plan = plan_cycle(snap, cfg(SYMBOLS=["ETHUSDT"]), "NORMAL", book, "replay")
        assert not any("POSITION_UNREADABLE" in a for a in plan.alerts), plan.alerts
        assert not any(a.kind == "EXIT" for a in plan.actions)


def test_layer_a_is_the_rate_at_the_staked_amount():
    """Finding 3, fixed: 27.9414 USDT staked, all in the 6.94 % tier."""
    product = bodies("/v5/earn/product")[0]["body"]["result"]["list"][0]
    assert product["hasTieredApr"] is True and product["estimateApr"] == "1.94%"
    assert product["tierAprDetails"][0] == {"min": "0", "max": "100", "estimateApr": "6.94%"}
    s = snapshot_at(T_HOLD)
    assert s.earn.staked < 100
    assert s.earn.apr == pytest.approx(0.0694)
    assert s.earn.headline_apr == 0.0194 and s.earn.tiered


# --- the run around it --------------------------------------------------------------------- #

def test_no_paper_cycle_ran_during_the_round_trip():
    t0, t1 = STATE["entered_ms"] - 600_000, 1792283224000
    paper = [r for r in RECORDS if r.get("dry_run") and t0 <= r["cycle_ms"] <= t1]
    assert paper == []


def test_the_heartbeat_renewed_at_half_age_once_deployed():
    """PR #4 in production: from its deploy (18:30 on 17/10, one renewal of
    the state the old code had let expire) every renewal was of a FRESH
    state, past half the age limit (15-20 min: the timer runs every 5), and
    no cycle saw a stale state until the timers were stopped for the run.
    Restarted at 07:15 on 18/10 the heartbeat renewed a 416-min state once,
    as it should."""
    reasons = {}
    for r in RECORDS:
        m = r["risk_state_meta"]
        reasons.setdefault(m["ts"], (r["cycle_ms"], m["reason"]))
    new = sorted((ts, why) for ts, (_, why) in reasons.items()
                 if 1792261800000 <= ts < 1792300000000)       # 18:30 -> the timers stopped
    assert new[0][1] == "heartbeat (was stale, 62 min, scanner alive)"
    ages = [re.fullmatch(r"heartbeat \(was fresh, (\d+) min, scanner alive\)", why)
            for _, why in new[1:]]
    assert len(ages) > 10 and all(a and 15 <= int(a.group(1)) < 30 for a in ages), new
    stale = [r for r in RECORDS if r["risk_state_meta"]["code"] == "RISK_STATE_STALE"
             and 1792261815799 < r["cycle_ms"] < 1792307700000]
    assert stale == []
