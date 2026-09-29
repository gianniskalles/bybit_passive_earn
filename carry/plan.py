"""carry/plan.py — one cycle's plan (CARRY_PLAN Phase 3). PURE: snapshot +
config + risk state + book -> Plan. No IO, no clock (now = snapshot time).

Per symbol, in this order:
  1. Reconcile what Bybit shows with the book: an untracked short is adopted
     (UNTRACKED_POSITION); a long perp is never ours and is closed; an orphan
     leg (spot without short = liquidation/ADL, or short without spot) is
     closed in this cycle (ORPHAN_LEG); a short smaller than we left it is ADL
     (ADL_DETECTED) and the excess spot is sold (R13, R14).
  2. Risk exits, allowed in every state and inside the settlement window:
     UNWIND (spot, then perp reduceOnly), MMR >= MMR_EMERGENCY (perp first),
     symbol not Trading (R28), spot coin no longer active collateral (R12).
  3. Risk reductions: hedge drift > MAX_HEDGE_DRIFT_PCT -> rebalance TOWARD
     neutral; ADL rank >= ADL_RANK_REDUCE or MMR >= MMR_REDUCE -> TRIM both
     legs by TRIM_FRACTION (R13, R15).
  4. The funding decision (carry.decide.decide_symbol): EXIT / HOLD / ENTER /
     STAY_OUT, with its settlement window and hysteresis.
  5. Entry (decision 13.4): if the UTA lacks the USDT, EARN_REDEEM_FOR_ENTRY
     only; the legs wait for a Success redeem in a later cycle. A redeem that
     fails, outlives REDEEM_TIMEOUT_HOURS (EARN_REDEEM_STUCK) or whose entry
     conditions are gone is abandoned; its USDT goes back to Earn.
  6. Account level (decision 13.5): idle USDT back to Earn — never the
     USDT_BUFFER_USD while a position is open, never a non-USDT coin, never in
     a cycle that also trades or waits for a redeem.
Every action is checked against carry/state.is_allowed before it is returned.

Book transitions that need no exchange confirmation (REDEEMING started,
abandoned, adopted, closed) are returned in book_updates; transitions that
depend on fills (ENTER -> OPEN, EXIT -> FLAT) belong to execute.py (Phase 4).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Tuple

from carry import state as cs
from carry.config import notional_cap, to_params
from carry.decide import Decision, FundingView, PositionView, decide_symbol
from carry.preflight import spot_fee, tradable_symbols
from carry.snapshot import EARN_FAIL, EARN_SUCCESS, Snapshot

TRIM_FRACTION = 0.5
ENTRY_PRICE_SLACK = 0.005          # USDT kept above the spot cost for price moves
EARN_RETURN_MIN_USD = 1.0
DAY_MS = 86_400_000
FLAT, REDEEMING, OPEN = "FLAT", "REDEEMING", "OPEN"


@dataclass(frozen=True)
class SymbolBook:
    """What the system remembers about a symbol between cycles."""
    status: str = FLAT
    redeem_link: Optional[str] = None
    redeem_started_ms: Optional[int] = None
    redeem_amount: Optional[float] = None
    entered_ms: Optional[int] = None
    perp_qty: float = 0.0                        # short size we left it at (ADL check)
    entry_times: Tuple[int, ...] = ()            # for MAX_ROUND_TRIPS_PER_30D
    collateral_ratio: Optional[float] = None     # last seen (R16)


@dataclass(frozen=True)
class Action:
    kind: str                                    # carry/state.py action names
    symbol: Optional[str]
    reason: str
    legs: Tuple[str, ...] = ()                   # order of the legs
    perp_qty: Optional[float] = None             # None on an exit = all, reduceOnly
    spot_qty: Optional[float] = None
    usdt: Optional[float] = None
    coin: Optional[str] = None                   # Earn actions: always USDT
    order_link_id: Optional[str] = None


@dataclass(frozen=True)
class Plan:
    actions: Tuple[Action, ...]
    alerts: Tuple[str, ...]
    decisions: Mapping[str, Decision]
    book_updates: Mapping[str, SymbolBook]
    no_entry: Mapping[str, Tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))


def _dec(x) -> Decimal:
    return Decimal(str(x))


def _diff(a: float, b: float) -> float:
    """a - b without binary rounding (0.03 - 0.01 is 0.02, not 0.019999)."""
    return float(_dec(a) - _dec(b))


def _floor_to(x: float, step: float) -> float:
    return float((_dec(x) / _dec(step)).to_integral_value(rounding=ROUND_FLOOR) * _dec(step))


def _ceil_to(x: float, step: float) -> float:
    return float((_dec(x) / _dec(step)).to_integral_value(rounding=ROUND_CEILING) * _dec(step))


def link_id(cycle_id: str, tag: str, symbol: str) -> str:
    raw = f"{cycle_id}-{tag}-{symbol}"
    clean = "cy-" + re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return clean if len(clean) <= 36 else "cy-" + hashlib.sha256(raw.encode()).hexdigest()[:33]


def plan_cycle(snap: Snapshot, cfg: Mapping, risk_state: str, book: Mapping[str, SymbolBook],
               cycle_id: str) -> Plan:
    now = snap.taken_ms
    params = to_params(cfg)
    alerts: List[str] = []
    actions: List[Action] = []
    decisions: Dict[str, Decision] = {}
    updates: Dict[str, SymbolBook] = {}
    no_entry: Dict[str, Tuple[str, ...]] = {}

    state = risk_state if risk_state in cs.STATES else "NO_NEW_POSITIONS"
    acct, earn = snap.account, snap.earn
    buffer = float(cfg["USDT_BUFFER_USD"])

    # ---- account-wide gates -------------------------------------------------
    block: List[str] = []
    if state != "NORMAL":
        block.append(f"risk_state {state}")
    if snap.region_restricted:
        alerts.append("REGION_RESTRICTED: Bybit refused a request for regional reasons")
        block.append("region restricted")
    if snap.foreign_orders:
        alerts.append(f"FOREIGN_ACTIVITY: {len(snap.foreign_orders)} open order(s) without our "
                      f"orderLinkId")
        block.append("foreign activity")
    emergency = reduce = False
    if acct is None:
        block.append("account unreadable")
    else:
        if not acct.cross_margin:
            alerts.append(f"MARGIN_MODE: {acct.margin_mode}, cross/portfolio required (R18)")
            block.append("margin mode")
        borrow = acct.balance("USDT").borrow
        if borrow > 0:
            alerts.append(f"USDT_BORROW: {borrow} USDT borrowed")
            if borrow > float(cfg["MAX_USDT_BORROW_USD"]):
                alerts.append(f"USDT_BORROW_LIMIT: {borrow} > MAX_USDT_BORROW_USD "
                              f"{cfg['MAX_USDT_BORROW_USD']}")
                block.append("USDT borrow over the limit")
        mmr = acct.mm_rate
        if mmr >= cfg["MMR_EMERGENCY"]:
            emergency = True
            alerts.append(f"MARGIN_EMERGENCY: accountMMRate {mmr} >= {cfg['MMR_EMERGENCY']}")
        elif mmr >= cfg["MMR_REDUCE"]:
            reduce = True
            alerts.append(f"MARGIN_REDUCE: accountMMRate {mmr} >= {cfg['MMR_REDUCE']}")
        elif mmr >= cfg["MMR_WARN"]:
            alerts.append(f"MARGIN_WARN: accountMMRate {mmr} >= {cfg['MMR_WARN']}")
        if emergency or reduce:
            block.append("margin")
    if earn is None:
        block.append("earn unreadable")
    if snap.open_orders is None:
        block.append("orders unreadable")
    tradable, pf_alerts = tradable_symbols(snap, cfg)
    alerts.extend(pf_alerts)

    symbols = list(dict.fromkeys(list(cfg["SYMBOLS"]) +
                                 [s for s, b in book.items() if b.status != FLAT]))
    open_notional = 0.0
    for sym in symbols:
        pos = snap.positions.get(sym)
        m = snap.markets.get(sym)
        if pos is not None and pos.side == "Sell" and m is not None:
            open_notional += pos.size * m.mark_price
    wallet_usdt = acct.balance("USDT").wallet if acct else 0.0
    committed_usdt = 0.0            # USDT this plan already spends on entries

    for sym in symbols:
        sb = book.get(sym, SymbolBook())
        m = snap.markets.get(sym)
        usable = m if (m is not None and f"market:{sym}" not in snap.stale) else None
        pos = snap.positions.get(sym)
        base = sym[:-len("USDT")]
        spot = acct.balance(base).wallet if acct else None
        dust = m.spot.min_qty if m else 0.0

        if acct is not None and base in acct.collateral:
            ratio = acct.collateral[base].ratio
            if sb.collateral_ratio and ratio < sb.collateral_ratio * (1 - cfg["CVR_DROP_ALERT"]):
                alerts.append(f"CVR_DROP: {base} collateral ratio {sb.collateral_ratio} -> {ratio}")
                no_entry.setdefault(sym, ())
                no_entry[sym] += ("collateral ratio dropped",)
            elif ratio != sb.collateral_ratio and sb.status == FLAT and sym not in updates:
                updates[sym] = replace(sb, collateral_ratio=ratio)

        # ---- 1. reconcile -----------------------------------------------------------
        if pos is not None and pos.side == "Buy" and pos.size > 0:
            alerts.append(f"UNTRACKED_POSITION: {sym} long perp {pos.size} is never ours")
            actions.append(Action("EXIT", sym, "long perp is never ours", legs=("perp",)))
            continue
        short_qty = (pos.size if pos is not None else None)
        in_pos = sb.status == OPEN or bool(short_qty)
        if sb.status != OPEN and short_qty:
            alerts.append(f"UNTRACKED_POSITION: {sym} short {short_qty} not in the book; adopted")
            sb = replace(sb, status=OPEN, entered_ms=None, perp_qty=short_qty,
                         redeem_link=None, redeem_started_ms=None, redeem_amount=None)
            updates[sym] = sb
        known = short_qty is not None and spot is not None
        if sb.status == OPEN and known:
            if short_qty == 0 and spot > dust:
                alerts.append(f"ORPHAN_LEG: {sym} spot {spot} without a short (liquidation/ADL)")
                actions.append(Action("EXIT", sym, "orphan spot", legs=("spot",), spot_qty=spot))
                continue
            if short_qty > 0 and spot < dust:
                alerts.append(f"ORPHAN_LEG: {sym} short {short_qty} without spot")
                actions.append(Action("EXIT", sym, "orphan short", legs=("perp",)))
                continue
            if short_qty == 0:
                updates[sym] = SymbolBook(entry_times=sb.entry_times,
                                          collateral_ratio=sb.collateral_ratio)
                sb, in_pos = updates[sym], False

        # ---- 2. risk exits ------------------------------------------------------------
        if in_pos:
            why = None
            legs = ("spot", "perp")
            if state == "UNWIND":
                why = "risk_state UNWIND"
            if emergency:
                why, legs = "margin emergency", ("perp", "spot")
            if why is None and m is not None and not (m.perp.trading and m.spot.trading):
                why = f"not Trading (perp {m.perp.status}, spot {m.spot.status})"
            if why is None and acct is not None and base in acct.collateral \
                    and not acct.collateral[base].active:
                why = f"{base} is not active UTA collateral (R12)"
            if why:
                actions.append(Action("EXIT", sym, why, legs=legs,
                                      spot_qty=spot if spot else None))
                continue

            # ---- 3. risk reductions -------------------------------------------------
            if known and m is not None:
                if sb.perp_qty and short_qty < sb.perp_qty - m.perp.qty_step / 2:
                    alerts.append(f"ADL_DETECTED: {sym} short {sb.perp_qty} -> {short_qty} "
                                  f"without our order")
                drift = (spot - short_qty) / short_qty * 100
                if abs(drift) > cfg["MAX_HEDGE_DRIFT_PCT"]:
                    if spot > short_qty:
                        qty = _floor_to(_diff(spot, short_qty), m.spot.qty_step)
                        actions.append(Action("REBALANCE_TOWARD_NEUTRAL", sym,
                                              f"spot {spot} over short {short_qty}",
                                              legs=("spot",), spot_qty=qty))
                    else:
                        qty = _floor_to(_diff(short_qty, spot), m.perp.qty_step)
                        actions.append(Action("REBALANCE_TOWARD_NEUTRAL", sym,
                                              f"short {short_qty} over spot {spot}",
                                              legs=("perp",), perp_qty=qty))
                    continue
                adl_high = pos is not None and pos.adl_rank >= cfg["ADL_RANK_REDUCE"]
                if adl_high or reduce:
                    perp_trim = _floor_to(short_qty * TRIM_FRACTION, m.perp.qty_step)
                    if perp_trim > 0:
                        actions.append(Action(
                            "TRIM", sym, "ADL rank high" if adl_high else "margin reduce",
                            legs=("spot", "perp"), perp_qty=perp_trim,
                            spot_qty=_floor_to(perp_trim, m.spot.qty_step)))
                        continue

        # ---- 4. funding decision -------------------------------------------------------
        if usable is None:
            no_entry[sym] = no_entry.get(sym, ()) + ("market unreadable or stale",)
            if sb.status == REDEEMING:
                _redeem_step(sym, sb, None, False, now, cfg, earn, alerts, actions, updates)
            continue
        recent = sum(1 for t in sb.entry_times if now - 30 * DAY_MS < t <= now)
        view = FundingView(sym, now, usable.next_funding_time_ms, usable.funding_interval_min,
                           usable.previous_interval_min, usable.predicted_rate,
                           usable.settled_rates, earn.apr if earn else None,
                           usable.basis_bps, usable.spread_bps)
        d = decide_symbol(view, PositionView(in_pos, sb.entered_ms, recent), params, risk_state=state)
        decisions[sym] = d
        if in_pos:
            if d.action == "EXIT":
                actions.append(Action("EXIT", sym, d.reason, legs=("spot", "perp"),
                                      spot_qty=spot if spot else None))
            continue

        # ---- 5. entry ---------------------------------------------------------------------
        why_not = list(block) + snap.missing_for_entry(sym) + list(no_entry.get(sym, ()))
        if sym not in tradable:
            why_not.append("preflight: minimum position does not fit or market unusable")
        if acct is not None and base in acct.collateral and not acct.collateral[base].active:
            why_not.append(f"{base} not active collateral")
        no_entry[sym] = tuple(why_not)
        wants = d.action == "ENTER" and not why_not
        size = _entry_size(usable, cfg, spot_fee(snap, sym, cfg)) if wants else None
        if size is not None:
            perp_qty, spot_qty, cost = size
            room = float(cfg["TOTAL_CAPITAL_CAP_USD"]) - buffer - open_notional
            if cost > room:
                no_entry[sym] += (f"capital: {cost:.2f} > room {room:.2f}",)
                size = None
        if sb.status == REDEEMING:
            enter = _redeem_step(sym, sb, d, bool(size), now, cfg, earn, alerts, actions, updates)
            if enter and size is not None:
                need = _required(size[2], open_notional, buffer)
                if wallet_usdt - committed_usdt >= need:
                    actions.append(Action("ENTER", sym, d.reason, legs=("perp", "spot"),
                                          perp_qty=size[0], spot_qty=size[1], usdt=size[2]))
                    committed_usdt += need
                    open_notional += size[2]
            continue
        if size is None:
            continue
        need = _required(size[2], open_notional, buffer)
        free = wallet_usdt - committed_usdt
        if free >= need:
            actions.append(Action("ENTER", sym, d.reason, legs=("perp", "spot"),
                                  perp_qty=size[0], spot_qty=size[1], usdt=size[2]))
            committed_usdt += need
            open_notional += size[2]
            continue
        amount = _ceil_to(need - max(free, 0.0), 0.01)
        if earn is None or earn.staked < amount:
            no_entry[sym] += (f"not enough USDT in Earn ({earn.staked if earn else '?'} < {amount})",)
            continue
        link = link_id(cycle_id, "R", sym)
        actions.append(Action("EARN_REDEEM_FOR_ENTRY", sym, d.reason, usdt=amount, coin="USDT",
                              order_link_id=link))
        updates[sym] = replace(sb, status=REDEEMING, redeem_link=link, redeem_started_ms=now,
                               redeem_amount=amount)

    # ---- 6. idle USDT back to Earn ------------------------------------------------------------
    final = {s: updates.get(s, book.get(s, SymbolBook())) for s in set(book) | set(updates)}
    busy = any(a.kind != "EARN_RETURN" for a in actions)
    waiting = any(b.status == REDEEMING for b in final.values())
    if acct is not None and earn is not None and not busy and not waiting and not earn.unfinished \
            and acct.balance("USDT").borrow == 0:
        any_open = any(b.status == OPEN for b in final.values()) or any(
            p.size > 0 for p in snap.positions.values())
        keep = buffer if any_open else 0.0
        amount = _floor_to(min(wallet_usdt, acct.balance("USDT").equity) - keep, 0.01)
        if amount >= max(EARN_RETURN_MIN_USD, earn.min_stake or 0.0):
            actions.append(Action("EARN_RETURN", None, "idle USDT back to Easy Earn", usdt=amount,
                                  coin="USDT"))

    permitted = []
    for a in actions:
        if cs.is_allowed(state, a.kind):
            permitted.append(a)
        else:
            alerts.append(f"CRITICAL: {a.kind} {a.symbol} planned in {state}; dropped")
    return Plan(tuple(permitted), tuple(alerts), MappingProxyType(decisions),
                MappingProxyType(updates), MappingProxyType(no_entry))


def _required(cost: float, open_notional: float, buffer: float) -> float:
    """USDT the UTA must hold for this entry: the spot cost plus slack, and
    the buffer if it is the first position (the buffer is account-wide)."""
    return cost * (1 + ENTRY_PRICE_SLACK) + (buffer if open_notional == 0 else 0.0)


def _entry_size(m, cfg: Mapping, fee: float) -> Optional[Tuple[float, float, float]]:
    """(perp_qty, spot_buy_qty, spot_cost_usdt) within the symbol's notional
    limit, or None when not even the minimum fits."""
    from carry.preflight import min_position
    limit = notional_cap(cfg, m.symbol)
    ask = m.spot_ask
    perp_qty = _floor_to(limit * (1 - fee) / ask, m.perp.qty_step)
    mp = min_position(m, fee)
    if perp_qty < mp.perp_qty:
        return None
    for _ in range(1000):
        spot_qty = _ceil_to(perp_qty / (1 - fee), m.spot.qty_step)
        cost = spot_qty * ask
        if cost <= limit:
            return perp_qty, spot_qty, cost
        perp_qty = _floor_to(_diff(perp_qty, m.perp.qty_step), m.perp.qty_step)
        if perp_qty < mp.perp_qty:
            return None
    return None


def _redeem_step(sym, sb: SymbolBook, d: Optional[Decision], can_enter: bool, now: int, cfg,
                 earn, alerts, actions, updates) -> bool:
    """One cycle of an entry waiting for its redeem. True = enter now."""
    order = earn.order(sb.redeem_link) if (earn and sb.redeem_link) else None
    st = order.state if order else None
    elapsed = now - (sb.redeem_started_ms or now)
    timeout = float(cfg["REDEEM_TIMEOUT_HOURS"]) * 3_600_000

    def abandon(reason):
        updates[sym] = SymbolBook(entry_times=sb.entry_times, collateral_ratio=sb.collateral_ratio)
        return False

    if st == EARN_FAIL:
        alerts.append(f"EARN_REDEEM_FAILED: {sym} redeem {sb.redeem_link} failed; entry abandoned")
        return abandon("failed")
    if st == EARN_SUCCESS:
        if d is not None and d.action == "ENTER" and can_enter:
            updates[sym] = replace(sb, status=FLAT, redeem_link=None, redeem_started_ms=None,
                                   redeem_amount=None)
            return True
        waiting_window = d is not None and "settlement window" in d.reason
        if waiting_window and elapsed < timeout:
            return False
        return abandon("conditions gone")
    if elapsed >= timeout:
        alerts.append(f"EARN_REDEEM_STUCK: {sym} redeem {sb.redeem_link} not complete after "
                      f"{elapsed / 3_600_000:.1f} h; entry abandoned")
        return abandon("timeout")
    if d is not None and d.action != "ENTER" and "settlement window" not in d.reason:
        return abandon("conditions gone")
    return False
