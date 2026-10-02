"""carry/execute.py — runs one cycle's plan against the exchange (CARRY_PLAN
Phase 4, R19-R24, decision 13.14).

Entry, two legs (R19-R22):
  1. perp: Sell Limit IOC at the price that covers the quantity in the
     linear orderbook read now. Book unreadable -> no order at all.
  2. spot: Buy Limit IOC for the quantity the perp FILLED, grossed up for the
     fee the exchange takes in the coin (R22), at the price from the spot
     orderbook read now. An IOC that fills less is retried with a fresh book
     until LEG_TIMEOUT_S; a rejection stops the leg. A fully filled order is
     never topped up: a fee above the assumed one is taken off the perp.
  3. reconcile (R20): the perp is cut to the spot actually received (on the
     perp step, never above it) with a reduceOnly Market buy. No spot at all
     -> the whole perp is closed: ORPHAN_LEG and NO_NEW_POSITIONS.
Exits never depend on a fresh price (decision 13.14): the perp closes with
reduceOnly Market; the spot sells Limit IOC at the price that covers the
quantity in the orderbook read now, and Market (with an alert) when the book
cannot be read or the limit has not sold it all by LEG_TIMEOUT_S.

Every order (R23, R24): a deterministic orderLinkId per cycle, action, leg
and attempt. A send that fails WITHOUT an answer (timeout, HTTP error) is
looked up by orderLinkId before anything else; if it does not exist it is
resent with the SAME orderLinkId (Bybit refuses a duplicate). A spot order
whose fate is still unknown at the deadline is journaled in the book
(pending_spot) and resolved by resolve_pending() next cycle; the plan
refuses entries for the symbol until then. A perp needs no journal: its
position is read from Bybit every cycle.

Only the book's spot quantity is ever sold (decision 13.14: foreign coins
are never traded). DRY_RUN: execute_plan refuses a live exchange, and
LiveExchange refuses every write again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from bybit_earn_tool import BybitAPIError, place_order_request
from carry import state as cs
from carry.client import order_request
from carry.plan import FLAT, OPEN, Plan, SymbolBook, link_id
from carry.preflight import spot_fee
from carry.snapshot import Snapshot

POLL_S = 1.0
TERMINAL = frozenset({"Filled", "PartiallyFilledCanceled", "Cancelled", "Rejected",
                      "Deactivated"})
ESCALATE = "NO_NEW_POSITIONS"
RET_NOTHING_TO_REDUCE = 110017     # Bybit: reduce-only, but the position is zero
RET_DUPLICATE_LINK = 110072        # Bybit: orderLinkId already used


class DryRunRefused(RuntimeError):
    """A write was attempted while DRY_RUN is true."""


class LiveExchange:
    """The real exchange behind execute_plan: CarryClient reads, and writes
    that refuse to leave while DRY_RUN is true."""
    live = True

    def __init__(self, client, dry_run: bool):
        self.client = client
        self.dry_run = dry_run

    def create_order(self, request: Dict) -> Dict:
        if self.dry_run:
            raise DryRunRefused("DRY_RUN: trading order not sent")
        return self.client.create_order(request)

    def place_earn(self, request: Dict) -> Dict:
        if self.dry_run:
            raise DryRunRefused("DRY_RUN: Earn order not sent")
        return self.client.place_order(request)

    def find_order(self, category: str, order_link_id: str) -> Optional[Dict]:
        return self.client.find_order(category, order_link_id)

    def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> Dict:
        return self.client.get_orderbook(category, symbol, limit)

    def get_earn_orders(self, order_link_id: str) -> List[Dict]:
        return self.client.get_earn_orders(order_link_id=order_link_id)


@dataclass
class ExecResult:
    book_updates: Dict[str, SymbolBook]
    alerts: List[str]
    orders: List[Dict]                       # one record per order sent (ledger, Phase 5)
    escalate: Optional[str] = None           # NO_NEW_POSITIONS after orphan protection


@dataclass(frozen=True)
class Outcome:
    state: str                               # done | rejected | unknown
    filled: float = 0.0
    order: Optional[Dict] = None
    error: Optional[str] = None
    ret_code: Optional[int] = None


# ---- small pure helpers ----------------------------------------------------------------

def _d(x) -> Decimal:
    return Decimal(str(x))


def floor_to(x: float, step: float) -> float:
    return float((_d(x) / _d(step)).to_integral_value(rounding=ROUND_FLOOR) * _d(step))


def ceil_to(x: float, step: float) -> float:
    return float((_d(x) / _d(step)).to_integral_value(rounding=ROUND_CEILING) * _d(step))


def sub(a: float, b: float) -> float:
    return float(_d(a) - _d(b))


def add(a: float, b: float) -> float:
    return float(_d(a) + _d(b))


def limit_price(levels, qty: float) -> float:
    """The worst price needed to fill qty, walking the side of the book from
    the best level; the last level when the book is thinner (an IOC then
    fills what is there)."""
    if not levels:
        raise ValueError("empty side of the book")
    cum = 0.0
    price = None
    for lvl in levels:
        price, size = float(lvl[0]), float(lvl[1])
        if price <= 0:
            raise ValueError(f"bad price level {lvl!r}")
        cum += size
        if cum >= qty:
            return price
    return price


def fee_in_base(order: Dict, base: str) -> Tuple[float, bool]:
    """The fee taken in the bought coin (R22) and whether Bybit said so
    (cumFeeDetail). Without the detail the whole cumExecFee is assumed to be
    in the coin: the hedge then errs to a smaller short, never a naked one."""
    detail = order.get("cumFeeDetail")
    if isinstance(detail, dict):
        return float(detail.get(base) or 0.0), True
    return float(order.get("cumExecFee") or 0.0), False


# ---- the run ---------------------------------------------------------------------------

def execute_plan(plan: Plan, snap: Snapshot, cfg: Mapping, risk_state: str,
                 book: Mapping[str, SymbolBook], cycle_id: str, exchange,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> ExecResult:
    if cfg["DRY_RUN"] and getattr(exchange, "live", True):
        raise DryRunRefused("DRY_RUN is true: the live exchange is never used (paper only)")
    run = _Run(snap, cfg, risk_state, book, plan, cycle_id, exchange, clock, sleep)
    for i, a in enumerate(plan.actions):
        if not cs.is_allowed(risk_state, a.kind):
            run.alerts.append(f"CRITICAL: {a.kind} {a.symbol} not allowed in {risk_state}; "
                              f"not executed")
            continue
        try:
            run.dispatch(i, a)
        except DryRunRefused:
            raise
        except Exception as e:                                  # noqa: BLE001
            run.alerts.append(f"CRITICAL: {a.kind} {a.symbol} failed: {type(e).__name__}: {e}")
            run.escalate = ESCALATE
    return ExecResult(run.updates, run.alerts, run.orders, run.escalate)


class _Run:
    def __init__(self, snap, cfg, state, book, plan, cycle_id, exchange, clock, sleep):
        self.snap, self.cfg, self.state, self.cycle_id = snap, cfg, state, cycle_id
        self.x, self.clock, self.sleep = exchange, clock, sleep
        self.timeout = float(cfg["LEG_TIMEOUT_S"])
        self.book: Dict[str, SymbolBook] = dict(book)
        self.book.update(plan.book_updates)
        self.updates: Dict[str, SymbolBook] = dict(plan.book_updates)
        self.alerts: List[str] = []
        self.orders: List[Dict] = []
        self.escalate: Optional[str] = None
        self.now = snap.taken_ms

    # ---- bookkeeping -------------------------------------------------------------------
    def sb(self, sym) -> SymbolBook:
        return self.book.get(sym, SymbolBook())

    def put(self, sym, sb: SymbolBook) -> None:
        self.book[sym] = sb
        self.updates[sym] = sb

    def settle(self, sym, sb: SymbolBook) -> None:
        """Both legs gone -> FLAT, keeping what outlives a position."""
        m = self.snap.markets.get(sym)
        dust = m.spot.min_qty if m else 0.0
        if sb.perp_qty <= 0 and sb.spot_qty < dust and not sb.pending_spot:
            sb = SymbolBook(entry_times=sb.entry_times, collateral_ratio=sb.collateral_ratio)
        self.put(sym, sb)

    def link(self, i: int, tag: str, sym: str, n: int) -> str:
        return link_id(self.cycle_id, f"{i}{tag}{n}", sym)

    # ---- one order ---------------------------------------------------------------------
    def submit(self, req: Dict, action: str, sym: str, leg: str, deadline: float,
               ref_price: Optional[float] = None) -> Outcome:
        body = req["body"]
        rec = lambda out: self._record(action, sym, leg, body, out, ref_price)  # noqa: E731
        cat, link = body["category"], body["orderLinkId"]
        sent = False
        error = None
        try:
            self.x.create_order(req)
            sent = True
        except BybitAPIError as e:
            if e.ret_code is not None:                         # Bybit answered: not placed
                out = Outcome("rejected", error=str(e), ret_code=e.ret_code)
                rec(out)
                return out
            error = str(e)                                     # no answer: look it up (R23)
        while True:
            try:
                o = self.x.find_order(cat, link)
                readable = True
            except BybitAPIError as e:
                o, readable = None, False
                error = str(e)
            if o is not None and o.get("orderStatus") in TERMINAL:
                out = Outcome("done", float(o.get("cumExecQty") or 0.0), o)
                rec(out)
                return out
            if o is None and readable and not sent:
                try:                                           # never arrived: same link (R24)
                    self.x.create_order(req)
                    sent = True
                except BybitAPIError as e:
                    if e.ret_code == RET_DUPLICATE_LINK:                   # duplicate: it does exist
                        sent = True
                    elif e.ret_code is not None:
                        out = Outcome("rejected", error=str(e), ret_code=e.ret_code)
                        rec(out)
                        return out
                    else:
                        error = str(e)
            if self.clock() >= deadline:
                out = Outcome("unknown", error=error)
                rec(out)
                self.alerts.append(f"ORDER_UNKNOWN: {sym} {leg} {link} still unknown after "
                                   f"{self.timeout:.0f} s ({error})")
                return out
            self.sleep(POLL_S)

    def _record(self, action, sym, leg, body, out: Outcome, ref_price=None) -> None:
        """One ledger row per order. slippage_bps: how much worse than the
        reference price (the best level of the book it was priced from, or
        the snapshot's when no book could be read) the fill was; > 0 = worse."""
        o = out.order or {}
        avg = float(o.get("avgPrice") or 0.0)
        slip = None
        if ref_price and avg > 0:
            sign = 1 if body.get("side") == "Sell" else -1
            slip = round(sign * (ref_price - avg) / ref_price * 1e4, 4)
        self.orders.append({"ts_ms": self.now, "action": action, "symbol": sym, "leg": leg,
                            "request": dict(body), "outcome": out.state, "filled": out.filled,
                            "avg_price": avg, "ref_price": ref_price, "slippage_bps": slip,
                            "fee": float(o.get("cumExecFee") or 0.0),
                            "fee_detail": o.get("cumFeeDetail"), "status": o.get("orderStatus"),
                            "error": out.error})

    def book_side(self, cat: str, sym: str, side: str) -> Optional[list]:
        try:
            ob = self.x.get_orderbook(cat, sym)
            lv = ob.get("b" if side == "Sell" else "a")
            return lv if lv else None
        except BybitAPIError:
            return None

    # ---- dispatch ----------------------------------------------------------------------
    def dispatch(self, i: int, a) -> None:
        if a.kind == "ENTER":
            self.enter(i, a)
        elif a.kind in ("EXIT", "REBALANCE_TOWARD_NEUTRAL", "TRIM"):
            self.reduce(i, a)
        elif a.kind in ("EARN_REDEEM_FOR_ENTRY", "EARN_RETURN"):
            self.earn(i, a)
        else:
            self.alerts.append(f"CRITICAL: unknown action {a.kind}; not executed")

    # ---- entry -------------------------------------------------------------------------
    def enter(self, i: int, a) -> None:
        sym = a.symbol
        m = self.snap.markets.get(sym)
        sb = self.sb(sym)
        if m is None or sb.status == OPEN:
            self.alerts.append(f"ENTRY_ABORTED: {sym} no market or already open")
            return
        base = sym[:-len("USDT")]
        fee = spot_fee(self.snap, sym, self.cfg)
        bids = self.book_side("linear", sym, "Sell")
        if bids is None:
            self.alerts.append(f"ENTRY_ABORTED: {sym} linear orderbook unreadable; no order sent")
            return
        qty = floor_to(a.perp_qty, m.perp.qty_step)
        req = order_request("linear", sym, "Sell", "Limit", qty, m.perp.qty_step,
                            self.link(i, "P", sym, 0), price=limit_price(bids, qty),
                            tick=m.perp.tick_size)
        start = self.clock()
        out = self.submit(req, "ENTER", sym, "perp", start + self.timeout,
                          ref_price=float(bids[0][0]))
        if out.state == "rejected":
            self.alerts.append(f"ENTRY_ABORTED: {sym} perp order rejected: {out.error}")
            return
        if out.state == "unknown":
            # Unknown fate: a reduceOnly close covers both cases.
            closed, ok = self.close_perp(i, sym, "Buy", qty, m)
            self.alerts.append(f"ORPHAN_LEG: {sym} perp entry unknown; closed reduceOnly "
                               f"({closed})")
            if not ok:
                self.alerts.append(f"CRITICAL: {sym} perp close after an unknown entry "
                                   f"incomplete")
            self.escalate = ESCALATE
            self.settle(sym, replace(sb, perp_qty=0.0 if ok else qty))
            return
        filled = out.filled
        if filled <= 0:
            return                                             # nothing filled: nothing to hedge
        sb = replace(sb, status=OPEN, entered_ms=self.now, perp_qty=filled,
                     entry_times=sb.entry_times + (self.now,), spot_qty=0.0)
        self.put(sym, sb)

        received, pending = self.buy_spot(i, sym, base, filled, fee, m, start + self.timeout)
        sb = replace(self.sb(sym), spot_qty=received,
                     pending_spot=self.sb(sym).pending_spot + tuple(pending))
        self.put(sym, sb)

        target = min(filled, floor_to(received, m.perp.qty_step)) if not pending else 0.0
        if target < filled:
            excess = sub(filled, target)
            closed, ok = self.close_perp(i, sym, "Buy", excess, m)
            sb = replace(self.sb(sym), perp_qty=sub(filled, closed))
            if received <= 0 or pending:
                self.alerts.append(f"ORPHAN_LEG: {sym} perp {filled} filled, spot "
                                   f"{'unknown' if pending else 'not bought'}; perp closed "
                                   f"reduceOnly ({closed})")
                self.escalate = ESCALATE
            else:
                self.alerts.append(f"PARTIAL_HEDGE: {sym} perp {filled} cut to {sb.perp_qty} "
                                   f"to match spot {received} (R20/R22)")
            if not ok:
                self.alerts.append(f"CRITICAL: {sym} perp close incomplete; short {sb.perp_qty} "
                                   f"against spot {received}")
                self.escalate = ESCALATE
            self.settle(sym, sb)

    def buy_spot(self, i, sym, base, need, fee, m, deadline) -> Tuple[float, List[str]]:
        """Buy until the coin received covers `need` (net of the fee in the
        coin), or the deadline. Returns (received, unknown order links)."""
        received = 0.0
        n = 0
        assumed = False
        while received < need and self.clock() < deadline:
            gross = ceil_to(sub(need, received) / (1 - fee), m.spot.qty_step)
            gross = max(gross, m.spot.min_qty)
            asks = self.book_side("spot", sym, "Buy")
            if asks is None:
                self.sleep(POLL_S)
                continue
            price = limit_price(asks, gross)
            if m.spot.min_notional and gross * price < m.spot.min_notional:
                gross = ceil_to(m.spot.min_notional / price, m.spot.qty_step)
            link = self.link(i, "S", sym, n)
            n += 1
            out = self.submit(order_request("spot", sym, "Buy", "Limit", gross, m.spot.qty_step,
                                            link, price=price, tick=m.spot.tick_size),
                              "ENTER", sym, "spot", deadline, ref_price=float(asks[0][0]))
            if out.state == "unknown":
                return received, [f"Buy:{link}"]
            if out.state == "rejected":
                self.alerts.append(f"SPOT_REJECTED: {sym} spot buy rejected: {out.error}")
                break
            fee_coin, said = fee_in_base(out.order, base)
            assumed = assumed or (out.filled > 0 and not said)
            received = add(received, sub(out.filled, fee_coin))
            if out.filled >= gross:
                break               # filled in full: a fee shortfall is cut from the perp (R22)
            self.sleep(POLL_S)      # the IOC left some: retry with a fresh book
        if assumed:
            self.alerts.append(f"FEE_CURRENCY_ASSUMED: {sym} no cumFeeDetail; spot fee taken "
                               f"as {base} (R22)")
        return received, []

    # ---- exits, rebalancing, trims -------------------------------------------------------
    def reduce(self, i: int, a) -> None:
        sym = a.symbol
        m = self.snap.markets.get(sym)
        if m is None:
            self.alerts.append(f"CRITICAL: {a.kind} {sym} without instruments; not executed")
            self.escalate = ESCALATE
            return
        sb = self.sb(sym)
        pos = self.snap.positions.get(sym)
        for leg in a.legs:
            if leg == "spot":
                want = a.spot_qty or 0.0
                if want > sb.spot_qty + 1e-12:
                    self.alerts.append(f"CRITICAL: {a.kind} {sym} asked to sell {want} spot, the "
                                       f"book holds {sb.spot_qty}; foreign coins are never sold")
                    want = sb.spot_qty
                sold = self.sell_spot(i, sym, want, m)
                sb = replace(self.sb(sym), spot_qty=max(0.0, sub(self.sb(sym).spot_qty, sold)))
                self.put(sym, sb)
            elif leg == "perp":
                side = "Sell" if (pos is not None and pos.side == "Buy") else "Buy"
                if a.perp_qty is not None:
                    qty = a.perp_qty
                elif pos is not None and pos.size > 0:
                    qty = pos.size
                else:
                    qty = sb.perp_qty                           # positions unreadable: the book
                if qty <= 0:
                    continue
                closed, ok = self.close_perp(i, sym, side, qty, m)
                if side == "Buy":
                    known = sb.perp_qty if a.perp_qty is not None or pos is None else pos.size
                    sb = replace(self.sb(sym), perp_qty=max(0.0, sub(known, closed)))
                    self.put(sym, sb)
                if not ok:
                    self.alerts.append(f"CRITICAL: {a.kind} {sym} perp close incomplete")
                    self.escalate = ESCALATE
        if a.kind == "EXIT":
            self.settle(sym, self.sb(sym))

    def close_perp(self, i, sym, side, qty, m) -> Tuple[float, bool]:
        """reduceOnly Market: no price needed (decision 13.14). Retried until
        LEG_TIMEOUT_S; reduceOnly never opens anything, so a retry is safe
        once the previous attempt is known to be final."""
        qty = ceil_to(qty, m.perp.qty_step)
        closed, n = 0.0, 0
        deadline = self.clock() + self.timeout
        while closed < qty and self.clock() < deadline:
            req = order_request("linear", sym, side, "Market", sub(qty, closed), m.perp.qty_step,
                                self.link(i, "C", sym, n), reduce_only=True)
            n += 1
            ref = m.perp_ask if side == "Buy" else m.perp_bid    # the snapshot: no fresh read
            out = self.submit(req, "CLOSE", sym, "perp", deadline, ref_price=ref)
            if out.state == "unknown":
                return closed, False
            if out.state == "rejected":
                if out.ret_code == RET_NOTHING_TO_REDUCE:
                    return qty, True        # the position is already zero: nothing left to close
                self.alerts.append(f"PERP_CLOSE_REJECTED: {sym} {out.error}; retrying")
                self.sleep(POLL_S)
                continue
            closed = add(closed, out.filled)
            if out.filled == 0:
                self.sleep(POLL_S)
        return closed, closed >= qty

    def sell_spot(self, i, sym, qty, m) -> float:
        """Limit IOC at the price that covers the quantity in the book read
        now; Market (alerted) without a book, or for what is left at the
        deadline. Below the spot minimum it is dust and stays."""
        qty = floor_to(qty, m.spot.qty_step)
        if qty < m.spot.min_qty:
            return 0.0
        sold, n = 0.0, 0
        ref = m.spot_bid                                        # the snapshot until a book is read
        deadline = self.clock() + self.timeout
        while sub(qty, sold) >= m.spot.min_qty and self.clock() < deadline:
            left = sub(qty, sold)
            bids = self.book_side("spot", sym, "Sell")
            if bids is not None:
                ref = float(bids[0][0])
            link = self.link(i, "X", sym, n)
            n += 1
            if bids is None:
                self.alerts.append(f"SPOT_MARKET_SELL: {sym} spot orderbook unreadable; "
                                   f"{left} sold at Market")
                req = order_request("spot", sym, "Sell", "Market", left, m.spot.qty_step, link)
            else:
                req = order_request("spot", sym, "Sell", "Limit", left, m.spot.qty_step, link,
                                    price=limit_price(bids, left), tick=m.spot.tick_size)
            out = self.submit(req, "SELL", sym, "spot", deadline, ref_price=ref)
            if out.state == "unknown":
                self._journal(sym, f"Sell:{link}")
                return sold
            if out.state == "rejected":
                self.alerts.append(f"CRITICAL: {sym} spot sell rejected: {out.error}")
                self.escalate = ESCALATE
                return sold
            sold = add(sold, out.filled)
            if sub(qty, sold) >= m.spot.min_qty:
                self.sleep(POLL_S)                              # the rest: a fresh book
        left = sub(qty, sold)
        if left >= m.spot.min_qty:
            self.alerts.append(f"SPOT_MARKET_SELL: {sym} limit sold {sold} of {qty} by "
                               f"LEG_TIMEOUT_S; {left} at Market")
            link = self.link(i, "X", sym, n)
            out = self.submit(order_request("spot", sym, "Sell", "Market", left, m.spot.qty_step,
                                            link), "SELL", sym, "spot",
                              self.clock() + self.timeout, ref_price=ref)
            if out.state == "unknown":
                self._journal(sym, f"Sell:{link}")
            else:
                sold = add(sold, out.filled)
        return sold

    def _journal(self, sym, entry) -> None:
        sb = self.sb(sym)
        self.put(sym, replace(sb, pending_spot=sb.pending_spot + (entry,)))

    # ---- Earn (decisions 13.3-13.5): USDT only -------------------------------------------
    def earn(self, i: int, a) -> None:
        if a.coin != "USDT" or self.snap.earn is None:
            self.alerts.append(f"CRITICAL: {a.kind} with coin {a.coin!r}; only USDT goes to "
                               f"Earn (the spot leg never does)")
            return
        order_type = "Redeem" if a.kind == "EARN_REDEEM_FOR_ENTRY" else "Stake"
        link = a.order_link_id or link_id(self.cycle_id, f"{i}E", "USDT")
        try:
            if self.x.get_earn_orders(link):
                return                                          # already placed (rerun, R24)
        except BybitAPIError as e:
            self.alerts.append(f"EARN_SKIPPED: {a.kind} could not check {link}: {e}")
            return
        req = place_order_request(order_type, self.cfg["ACCOUNT_TYPE"], "USDT",
                                  self.snap.earn.product_id, f"{a.usdt:.2f}", link)
        try:
            self.x.place_earn(req)
        except BybitAPIError as e:
            self.alerts.append(f"EARN_ORDER_FAILED: {a.kind} {link}: {e}")
        self.orders.append({"ts_ms": self.now, "action": a.kind, "symbol": a.symbol, "leg": "earn",
                            "request": dict(req["body"]), "outcome": "sent", "filled": a.usdt})


def resolve_pending(book: Mapping[str, SymbolBook], exchange) -> Tuple[Dict[str, SymbolBook],
                                                                         List[str]]:
    """Run before the cycle's snapshot: settle journaled spot orders whose
    fate was unknown. Filled coins become (or stop being) the book's; an
    order that never existed is dropped; one still unknown stays."""
    updates: Dict[str, SymbolBook] = {}
    alerts: List[str] = []
    for sym, sb in book.items():
        if not sb.pending_spot:
            continue
        base = sym[:-len("USDT")]
        keep: List[str] = []
        spot = sb.spot_qty
        for entry in sb.pending_spot:
            side, link = entry.split(":", 1)
            try:
                o = exchange.find_order("spot", link)
            except BybitAPIError as e:
                keep.append(entry)
                alerts.append(f"ORDER_UNKNOWN: {sym} {link} still unreadable: {e}")
                continue
            if o is None:
                continue
            if o.get("orderStatus") not in TERMINAL:
                keep.append(entry)
                continue
            filled = float(o.get("cumExecQty") or 0.0)
            if side == "Buy":
                fee, _ = fee_in_base(o, base)
                spot = add(spot, sub(filled, fee))
            else:
                spot = max(0.0, sub(spot, filled))
            alerts.append(f"ORDER_RESOLVED: {sym} {entry} {o.get('orderStatus')} {filled}")
        status = sb.status
        if spot > 0 and status != OPEN:
            status = OPEN
        updates[sym] = replace(sb, spot_qty=spot, pending_spot=tuple(keep), status=status)
    return updates, alerts
