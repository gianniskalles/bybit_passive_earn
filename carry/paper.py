"""carry/paper.py — paper trading for DRY_RUN (CARRY_PLAN §3.8, Phase 5).

The whole cycle runs on real market data; only the account is simulated.
PaperExchange is the exchange execute.py talks to while DRY_RUN is true
(live = False, so execute_plan accepts it): orders fill at the top of the
book read now (the real public orderbook when a client is given, else the
snapshot's prices), with the account's taker fees — the spot buy fee taken
in the coin (R22), the sell fee and the perp fee in USDT. Earn orders
succeed at once.

overlay(snapshot) replaces the snapshot's account, positions, open orders
and Earn with the paper ones, so the plan sees the paper position exactly
as it would see a real one. accrue(snapshot) books what happened between
two cycles: the funding of every settlement passed while short (a positive
rate pays the short) and the Easy Earn interest on the staked USDT.

The state is plain JSON (to_json/from_json), kept between cycles by the
runner. Test hooks: adl(symbol, qty) and liquidate(symbol) change the
paper position the way Bybit would, without an order of ours.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Dict, List, Optional

from bybit_earn_tool import BybitAPIError
from carry.snapshot import (Account, CoinBalance, Collateral, Earn, EarnOrder, PerpPosition,
                            Snapshot)

YEAR_MS = 365 * 24 * 3600 * 1000
PERP_MM_RATE = 0.005          # paper maintenance margin per unit of perp notional
DEPTH = 1e9                   # the paper book never runs out


@dataclass
class PaperState:
    usdt: float = 0.0
    earn_staked: float = 0.0
    coins: Dict[str, float] = field(default_factory=dict)
    shorts: Dict[str, float] = field(default_factory=dict)
    short_avg: Dict[str, float] = field(default_factory=dict)
    orders: Dict[str, Dict] = field(default_factory=dict)
    earn_orders: Dict[str, Dict] = field(default_factory=dict)
    last_ms: Optional[int] = None
    seq: int = 0


class PaperExchange:
    live = False

    def __init__(self, state: PaperState, cfg, public=None):
        self.s = state
        self.cfg = cfg
        self.public = public                 # a CarryPublicClient for real orderbooks
        self.snap: Optional[Snapshot] = None
        self.events: List[Dict] = []

    # ---- persistence ---------------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(self.s.__dict__, sort_keys=True)

    @classmethod
    def from_json(cls, text: str, cfg, public=None) -> "PaperExchange":
        return cls(PaperState(**json.loads(text)), cfg, public)

    @classmethod
    def load(cls, path: Path, cfg, public=None, initial_usdt: float = 0.0,
             initial_earn: float = 0.0) -> "PaperExchange":
        try:
            return cls.from_json(Path(path).read_text(), cfg, public)
        except FileNotFoundError:
            return cls(PaperState(usdt=initial_usdt, earn_staked=initial_earn), cfg, public)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json())
        os.replace(tmp, path)

    # ---- the account the plan sees -------------------------------------------------
    def overlay(self, snap: Snapshot, apr: Optional[float] = None,
                product_id: str = "paper") -> Snapshot:
        self.snap = snap
        positions = {}
        for sym in snap.symbols:
            q = self.s.shorts.get(sym, 0.0)
            positions[sym] = PerpPosition(sym, "Sell" if q > 0 else "", q,
                                          self.s.short_avg.get(sym) if q > 0 else None, None, 1)
        coins = {"USDT": CoinBalance(self.s.usdt, self.s.usdt, 0.0)}
        for c, q in self.s.coins.items():
            coins[c] = CoinBalance(q, q, 0.0)
        notional = sum(q * snap.markets[s].mark_price for s, q in self.s.shorts.items()
                       if q > 0 and s in snap.markets)
        equity = self.s.usdt + sum(q * snap.markets[f"{c}USDT"].spot_bid
                                   for c, q in self.s.coins.items() if f"{c}USDT" in snap.markets)
        mm = (notional * PERP_MM_RATE / equity) if equity > 0 else (1.0 if notional else 0.0)
        real = snap.account
        fees = dict(real.fees) if real is not None else {}
        for sym in snap.symbols:
            fees.setdefault(("linear", sym), (float(self.cfg["PERP_TAKER_FEE"]), 0.0))
            fees.setdefault(("spot", sym), (float(self.cfg["SPOT_TAKER_FEE"]), 0.0))
        coll = dict(real.collateral) if real is not None else {
            s[:-4]: Collateral(1.0, True, True) for s in snap.symbols}
        account = Account("REGULAR_MARGIN", mm, mm * 2, MappingProxyType(coins),
                          MappingProxyType(fees), MappingProxyType(coll))
        rate = apr if apr is not None else (snap.earn.apr if snap.earn is not None else None)
        earn = None
        if rate is not None:
            orders = tuple(EarnOrder(o["orderId"], link, o["orderType"], o["status"],
                                     o.get("amount"), o.get("ts_ms"))
                           for link, o in self.s.earn_orders.items())
            earn = Earn(snap.earn.product_id if snap.earn else product_id, "Available", rate,
                        0.0, self.s.earn_staked, orders,
                        snap.earn.min_stake if snap.earn else 1.0)
        errors = {k: v for k, v in snap.errors.items()
                  if not (k.startswith("positions:") or k in ("account", "orders", "earn"))}
        if earn is None:
            errors["earn"] = "paper: layer A APR unknown"
        return replace(snap, positions=MappingProxyType(positions), account=account,
                       open_orders=(), earn=earn, errors=MappingProxyType(errors))

    # ---- between cycles --------------------------------------------------------------
    def accrue(self, snap: Snapshot, apr: Optional[float]) -> List[Dict]:
        """Funding of the settlements passed and Earn interest since last_ms."""
        events: List[Dict] = []
        last, now = self.s.last_ms, snap.taken_ms
        if last is not None and now > last:
            for sym, q in self.s.shorts.items():
                m = snap.markets.get(sym)
                if q <= 0 or m is None:
                    continue
                for ts, rate in m.settled:
                    if last < ts <= now:
                        amt = q * m.mark_price * rate
                        self.s.usdt += amt
                        events.append({"type": "funding", "symbol": sym, "ts_ms": ts, "rate": rate,
                                       "qty": q, "mark": m.mark_price, "amount": amt})
            if apr and self.s.earn_staked > 0:
                interest = self.s.earn_staked * apr * (now - last) / YEAR_MS
                self.s.earn_staked += interest
                events.append({"type": "earn_interest", "ts_ms": now, "apr": apr,
                               "staked": self.s.earn_staked, "amount": interest})
        self.s.last_ms = now
        return events

    # ---- what Bybit can do to a position without us ---------------------------------
    def adl(self, symbol: str, qty: float) -> None:
        self.s.shorts[symbol] = max(0.0, round(self.s.shorts.get(symbol, 0.0) - qty, 12))

    def liquidate(self, symbol: str) -> None:
        self.s.shorts[symbol] = 0.0

    # ---- the exchange interface of execute.py ---------------------------------------
    def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> Dict:
        if self.public is not None:
            return self.public.get_orderbook(category, symbol, limit)
        m = self.snap.markets.get(symbol) if self.snap else None
        if m is None:
            raise BybitAPIError(f"paper: no market for {symbol}")
        bid, ask = (m.perp_bid, m.perp_ask) if category == "linear" else (m.spot_bid, m.spot_ask)
        return {"s": symbol, "b": [[repr(bid), repr(DEPTH)]], "a": [[repr(ask), repr(DEPTH)]]}

    def _fee(self, category: str, symbol: str) -> float:
        acct = self.snap.account if self.snap else None
        if acct is not None and (category, symbol) in acct.fees:
            return acct.fees[(category, symbol)][0]
        return float(self.cfg["PERP_TAKER_FEE" if category == "linear" else "SPOT_TAKER_FEE"])

    def create_order(self, request: Dict) -> Dict:
        b = request["body"]
        link = b["orderLinkId"]
        if link in self.s.orders:
            raise BybitAPIError("OrderLinkedID is duplicate", ret_code=110072)
        cat, sym, side, qty = b["category"], b["symbol"], b["side"], float(b["qty"])
        book = self.get_orderbook(cat, sym)
        price = float(book["b"][0][0] if side == "Sell" else book["a"][0][0])
        if b["orderType"] == "Limit":
            lim = float(b["price"])
            if (side == "Sell" and lim > price) or (side == "Buy" and lim < price):
                return self._store(b, 0.0, 0.0, 0.0, None)
        base = sym[:-len("USDT")]
        fee_rate = self._fee(cat, sym)
        fee_detail = None
        if cat == "linear":
            short = self.s.shorts.get(sym, 0.0)
            if b.get("reduceOnly"):
                if side != "Buy" or short <= 0:
                    raise BybitAPIError("current position is zero, cannot fix reduce-only order qty",
                                        ret_code=110017)
                fill = min(qty, short)
                avg = self.s.short_avg.get(sym, price)
                self.s.usdt += (avg - price) * fill
                self.s.shorts[sym] = round(short - fill, 12)
            else:
                if side != "Sell":
                    raise BybitAPIError("paper: a long perp is never opened", ret_code=110007)
                fill = qty
                self.s.short_avg[sym] = ((self.s.short_avg.get(sym, price) * short + price * fill)
                                         / (short + fill))
                self.s.shorts[sym] = round(short + fill, 12)
            fee = fill * price * fee_rate
            self.s.usdt -= fee
        else:
            if side == "Buy":
                cost = qty * price
                if cost > self.s.usdt + 1e-9:
                    raise BybitAPIError("paper: insufficient USDT", ret_code=170131)
                fill = qty
                fee = fill * fee_rate                       # in the coin (R22)
                self.s.usdt -= cost
                self.s.coins[base] = round(self.s.coins.get(base, 0.0) + fill - fee, 12)
                fee_detail = {base: repr(fee)}
            else:
                fill = min(qty, self.s.coins.get(base, 0.0))
                fee = fill * price * fee_rate               # in USDT
                self.s.coins[base] = round(self.s.coins.get(base, 0.0) - fill, 12)
                self.s.usdt += fill * price - fee
                fee_detail = {"USDT": repr(fee)}
        return self._store(b, fill, price, fee, fee_detail)

    def _store(self, b, fill, price, fee, fee_detail) -> Dict:
        self.s.seq += 1
        qty = float(b["qty"])
        status = "Filled" if fill >= qty - 1e-12 else ("PartiallyFilledCanceled" if fill > 0
                                                      else "Cancelled")
        o = {"orderId": f"paper-{self.s.seq}", "orderLinkId": b["orderLinkId"],
             "symbol": b["symbol"], "side": b["side"], "orderType": b["orderType"],
             "orderStatus": status, "qty": b["qty"], "cumExecQty": repr(fill),
             "cumExecFee": repr(fee), "avgPrice": repr(price if fill else 0.0)}
        if fee_detail:
            o["cumFeeDetail"] = fee_detail
        self.s.orders[b["orderLinkId"]] = o
        return {"orderId": o["orderId"], "orderLinkId": b["orderLinkId"]}

    def find_order(self, category: str, order_link_id: str) -> Optional[Dict]:
        return self.s.orders.get(order_link_id)

    def get_earn_orders(self, order_link_id: str) -> List[Dict]:
        o = self.s.earn_orders.get(order_link_id)
        return [o] if o else []

    def place_earn(self, request: Dict) -> Dict:
        b = request["body"]
        link, amount = b["orderLinkId"], float(b["amount"])
        ok = True
        if b["orderType"] == "Redeem":
            ok = amount <= self.s.earn_staked + 1e-9
            if ok:
                self.s.earn_staked -= amount
                self.s.usdt += amount
        else:
            ok = amount <= self.s.usdt + 1e-9
            if ok:
                self.s.usdt -= amount
                self.s.earn_staked += amount
        self.s.seq += 1
        self.s.earn_orders[link] = {"orderId": f"paper-earn-{self.s.seq}", "orderLinkId": link,
                                    "orderType": b["orderType"],
                                    "status": "Success" if ok else "Fail", "amount": amount,
                                    "ts_ms": self.snap.taken_ms if self.snap else None}
        return {"orderId": self.s.earn_orders[link]["orderId"], "orderLinkId": link}
