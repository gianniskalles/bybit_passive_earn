"""carry/snapshot.py — one immutable snapshot per cycle (CARRY_PLAN §6, Phase 2).

take() reads everything a cycle needs and NEVER raises for an API or
parsing failure: each section is either fully read and parsed, or absent
with its reason in `errors`. There is no partial section and no default for
a missing field (rule: unknown is never guessed). Phase 3's decide() works
only from a Snapshot; what is missing blocks new exposure, never an exit.

Sections: market:<SYM>, positions:<SYM>, account, orders, earn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from bybit_earn_tool import BybitAPIError
from carry.client import LINK_PREFIX, is_region_restricted

# R27: funding history may lag the ticker by at most this much after a
# settlement; beyond that the market section is stale.
FUNDING_LAG_SLACK_MS = 10 * 60 * 1000
EARN_CATEGORY = "FlexibleSaving"
PARSE_ERRORS = (BybitAPIError, KeyError, TypeError, ValueError, IndexError)


class Unreadable(ValueError):
    """A field is missing or not what Bybit documents."""


def _f(row: Mapping, key: str, where: str) -> float:
    v = row.get(key) if isinstance(row, Mapping) else None
    if v is None or (isinstance(v, str) and not v.strip()) or isinstance(v, bool):
        raise Unreadable(f"{where}: {key} missing")
    try:
        return float(v)
    except (TypeError, ValueError):
        raise Unreadable(f"{where}: {key}={v!r} is not a number") from None


def _opt_f(row: Mapping, key: str) -> Optional[float]:
    v = row.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _s(row: Mapping, key: str, where: str) -> str:
    v = row.get(key) if isinstance(row, Mapping) else None
    if not isinstance(v, str) or not v.strip():
        raise Unreadable(f"{where}: {key} missing")
    return v.strip()


def _b(row: Mapping, key: str, where: str) -> bool:
    v = row.get(key)
    if isinstance(v, bool):
        return v
    raise Unreadable(f"{where}: {key}={v!r} is not a boolean")


def _pct(v, where: str) -> float:
    """"1.2%" -> 0.012 (Earn APR format)."""
    s = str(v or "").strip()
    if not s.endswith("%"):
        raise Unreadable(f"{where}: APR {v!r} is not a percentage")
    return float(s[:-1]) / 100


def spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 1e4


def basis_bps(perp_bid: float, perp_ask: float, spot_bid: float, spot_ask: float) -> float:
    """Perp mid over spot mid, in bps (positive = perp above spot)."""
    perp, spot = (perp_bid + perp_ask) / 2, (spot_bid + spot_ask) / 2
    return (perp - spot) / spot * 1e4


# ---- sections ------------------------------------------------------------------ #

@dataclass(frozen=True)
class Instrument:
    category: str
    symbol: str
    status: str
    qty_step: float
    min_qty: float
    tick_size: float
    min_notional: Optional[float]           # linear minNotionalValue / spot minOrderAmt
    funding_interval_min: Optional[int]     # linear only

    @property
    def trading(self) -> bool:
        return self.status == "Trading"


@dataclass(frozen=True)
class Market:
    symbol: str
    perp: Instrument
    spot: Instrument
    next_funding_time_ms: int
    predicted_rate: float
    mark_price: float
    index_price: float
    perp_bid: float
    perp_ask: float
    spot_bid: float
    spot_ask: float
    settled: Tuple[Tuple[int, float], ...]  # oldest first

    @property
    def funding_interval_min(self) -> Optional[int]:
        return self.perp.funding_interval_min

    @property
    def settled_rates(self) -> Tuple[float, ...]:
        return tuple(r for _, r in self.settled)

    @property
    def previous_interval_min(self) -> Optional[float]:
        """The interval the last two settlements were apart (R9 change check)."""
        if len(self.settled) < 2:
            return None
        return (self.settled[-1][0] - self.settled[-2][0]) / 60000

    @property
    def basis_bps(self) -> float:
        return basis_bps(self.perp_bid, self.perp_ask, self.spot_bid, self.spot_ask)

    @property
    def spread_bps(self) -> float:
        """The wider of the two legs."""
        return max(spread_bps(self.perp_bid, self.perp_ask), spread_bps(self.spot_bid, self.spot_ask))


@dataclass(frozen=True)
class PerpPosition:
    symbol: str
    side: str                 # "Sell" for our short; "" when flat
    size: float
    avg_price: Optional[float]
    liq_price: Optional[float]
    adl_rank: int

    @property
    def flat(self) -> bool:
        return self.size == 0


@dataclass(frozen=True)
class CoinBalance:
    wallet: float
    equity: float
    borrow: float


@dataclass(frozen=True)
class Collateral:
    ratio: float
    margin_collateral: bool     # Bybit: the coin can be collateral
    collateral_switch: bool     # the account has it switched on

    @property
    def active(self) -> bool:
        return self.margin_collateral and self.collateral_switch


@dataclass(frozen=True)
class Account:
    margin_mode: str
    mm_rate: float
    im_rate: float
    coins: Mapping[str, CoinBalance]
    fees: Mapping[Tuple[str, str], Tuple[float, float]]   # (category, symbol) -> (taker, maker)
    collateral: Mapping[str, Collateral]

    def balance(self, coin: str) -> CoinBalance:
        """A coin absent from a successful wallet read has a zero balance."""
        return self.coins.get(coin, CoinBalance(0.0, 0.0, 0.0))

    @property
    def cross_margin(self) -> bool:
        return self.margin_mode in ("REGULAR_MARGIN", "PORTFOLIO_MARGIN")


@dataclass(frozen=True)
class OpenOrder:
    category: str
    symbol: str
    order_id: str
    order_link_id: str
    side: str
    qty: float
    status: str

    @property
    def ours(self) -> bool:
        return self.order_link_id.startswith(LINK_PREFIX)


EARN_SUCCESS, EARN_FAIL, EARN_PENDING, EARN_UNKNOWN = "SUCCESS", "FAIL", "PENDING", "UNKNOWN"
_EARN_PENDING = ("pending", "processing", "partiallyprocessed")


@dataclass(frozen=True)
class EarnOrder:
    order_id: str
    order_link_id: str
    order_type: str           # "Stake" | "Redeem"
    status: str               # raw
    value: Optional[float]
    created_ms: Optional[int]

    @property
    def state(self) -> str:
        """Only "success" is done. Decision 13.4: never spot without a
        COMPLETED redeem — anything not SUCCESS is not complete."""
        s = self.status.strip().lower()
        if s == "success":
            return EARN_SUCCESS
        if s == "fail":
            return EARN_FAIL
        if s in _EARN_PENDING:
            return EARN_PENDING
        return EARN_UNKNOWN


@dataclass(frozen=True)
class Earn:
    product_id: str
    product_status: str
    apr: float                        # layer A, e.g. 0.0173
    redeem_minutes: Optional[float]
    staked: float                     # USDT in the flexible product
    orders: Tuple[EarnOrder, ...]
    min_stake: Optional[float] = None

    def order(self, order_link_id: str) -> Optional[EarnOrder]:
        for o in self.orders:
            if o.order_link_id == order_link_id:
                return o
        return None

    @property
    def unfinished(self) -> Tuple[EarnOrder, ...]:
        return tuple(o for o in self.orders if o.state in (EARN_PENDING, EARN_UNKNOWN))


@dataclass(frozen=True)
class Snapshot:
    taken_ms: int
    testnet: bool
    symbols: Tuple[str, ...]
    markets: Mapping[str, Market]
    positions: Mapping[str, PerpPosition]
    account: Optional[Account]
    open_orders: Optional[Tuple[OpenOrder, ...]]
    earn: Optional[Earn]
    errors: Mapping[str, str]
    region_restricted: bool = False
    stale: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def foreign_orders(self) -> Tuple[OpenOrder, ...]:
        return tuple(o for o in (self.open_orders or ()) if not o.ours)

    def missing_for_entry(self, symbol: str) -> List[str]:
        """Why new exposure on `symbol` is impossible from this snapshot
        ([] = every input is known). Exits never consult this."""
        why = [f"{k}: {v}" for k, v in self.errors.items()
               if k in (f"market:{symbol}", f"positions:{symbol}", "account", "orders", "earn")]
        if f"market:{symbol}" in self.stale:
            why.append(f"market:{symbol}: stale: {self.stale[f'market:{symbol}']}")
        if self.region_restricted:
            why.append("REGION_RESTRICTED")
        return why


# ---- reading ------------------------------------------------------------------- #

def _instrument(raw: Dict, category: str, symbol: str) -> Instrument:
    where = f"{category} instrument {symbol}"
    lot, price = raw.get("lotSizeFilter"), raw.get("priceFilter")
    if not isinstance(lot, dict) or not isinstance(price, dict):
        raise Unreadable(f"{where}: lotSizeFilter/priceFilter missing")
    if category == "linear":
        interval = raw.get("fundingInterval")
        if isinstance(interval, bool) or not isinstance(interval, (int, str)) or \
                not str(interval).isdigit() or int(interval) <= 0:
            raise Unreadable(f"{where}: fundingInterval={interval!r}")
        return Instrument(category, symbol, _s(raw, "status", where), _f(lot, "qtyStep", where),
                          _f(lot, "minOrderQty", where), _f(price, "tickSize", where),
                          _opt_f(lot, "minNotionalValue"), int(interval))
    return Instrument(category, symbol, _s(raw, "status", where), _f(lot, "basePrecision", where),
                      _f(lot, "minOrderQty", where), _f(price, "tickSize", where),
                      _opt_f(lot, "minOrderAmt"), None)


def read_market(client, symbol: str, history: int) -> Market:
    perp = _instrument(client.get_instrument("linear", symbol), "linear", symbol)
    spot = _instrument(client.get_instrument("spot", symbol), "spot", symbol)
    lt, st = client.get_ticker("linear", symbol), client.get_ticker("spot", symbol)
    w, ws = f"linear ticker {symbol}", f"spot ticker {symbol}"
    nft = _f(lt, "nextFundingTime", w)
    settled = tuple(client.get_recent_funding(symbol, history))
    return Market(symbol, perp, spot, int(nft), _f(lt, "fundingRate", w), _f(lt, "markPrice", w),
                  _f(lt, "indexPrice", w), _f(lt, "bid1Price", w), _f(lt, "ask1Price", w),
                  _f(st, "bid1Price", ws), _f(st, "ask1Price", ws), settled)


def market_staleness(m: Market, now_ms: int) -> Optional[str]:
    if m.next_funding_time_ms <= now_ms:
        return f"nextFundingTime {m.next_funding_time_ms} is not in the future"
    if not m.settled:
        return "no settled funding"
    interval_ms = (m.funding_interval_min or 0) * 60000
    lag = m.next_funding_time_ms - interval_ms - m.settled[-1][0]
    if lag > FUNDING_LAG_SLACK_MS:
        return f"funding history lags the ticker by {lag // 60000} min"
    return None


def read_position(client, symbol: str) -> PerpPosition:
    rows = [r for r in client.get_positions(symbol) if r.get("symbol") == symbol]
    where = f"position {symbol}"
    if not rows:
        return PerpPosition(symbol, "", 0.0, None, None, 0)
    if len(rows) > 1 or str(rows[0].get("positionIdx", 0)) != "0":
        raise Unreadable(f"{where}: hedge-mode rows {rows!r}; one-way mode is required")
    r = rows[0]
    size = _f(r, "size", where)
    side = str(r.get("side") or "").strip()
    if size < 0 or (size > 0 and side not in ("Buy", "Sell")):
        raise Unreadable(f"{where}: size={size} side={side!r}")
    adl = r.get("adlRankIndicator")
    if isinstance(adl, bool) or not isinstance(adl, (int, str)) or not str(adl).isdigit():
        raise Unreadable(f"{where}: adlRankIndicator={adl!r}")
    return PerpPosition(symbol, side if size > 0 else "", size,
                        _opt_f(r, "avgPrice") if size > 0 else None,
                        _opt_f(r, "liqPrice") if size > 0 else None, int(adl))


def read_account(client, symbols: Tuple[str, ...], base_coins: Tuple[str, ...]) -> Account:
    info = client.get_account_info()
    margin_mode = _s(info, "marginMode", "account info")
    wallet = client.get_wallet_balance("UNIFIED")
    acct = wallet["list"][0]
    where = "wallet-balance"
    coins: Dict[str, CoinBalance] = {}
    for c in acct.get("coin") or []:
        name = _s(c, "coin", where).upper()
        wc = f"{where} {name}"
        coins[name] = CoinBalance(_f(c, "walletBalance", wc), _f(c, "equity", wc),
                                  _opt_f(c, "borrowAmount") or 0.0)
    fees: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for sym in symbols:
        for cat in ("linear", "spot"):
            row = client.get_fee_rate(cat, sym)
            wf = f"fee-rate {cat} {sym}"
            fees[(cat, sym)] = (_f(row, "takerFeeRate", wf), _f(row, "makerFeeRate", wf))
    collateral: Dict[str, Collateral] = {}
    for coin in base_coins:
        row = client.get_collateral_info(coin)
        wc = f"collateral-info {coin}"
        collateral[coin] = Collateral(_f(row, "collateralRatio", wc),
                                      _b(row, "marginCollateral", wc), _b(row, "collateralSwitch", wc))
    return Account(margin_mode, _f(acct, "accountMMRate", where), _f(acct, "accountIMRate", where),
                   MappingProxyType(coins), MappingProxyType(fees), MappingProxyType(collateral))


def read_open_orders(client) -> Tuple[OpenOrder, ...]:
    out = []
    for cat in ("linear", "spot"):
        for o in client.get_open_orders(cat):
            w = f"open order {cat}"
            out.append(OpenOrder(cat, _s(o, "symbol", w), _s(o, "orderId", w),
                                 str(o.get("orderLinkId") or ""), _s(o, "side", w),
                                 _f(o, "qty", w), _s(o, "orderStatus", w)))
    return tuple(out)


def read_earn(client, coin: str) -> Earn:
    products = [p for p in client.get_earn_products(coin=coin)
                if str(p.get("coin") or "").upper() == coin]
    if len(products) != 1:
        raise Unreadable(f"earn: expected one {coin} {EARN_CATEGORY} product, got {len(products)}")
    p = products[0]
    where = f"earn product {coin}"
    pid = str(p.get("productId") or "").strip()
    if not pid:
        raise Unreadable(f"{where}: productId missing")
    staked = 0.0
    for pos in client.get_earn_positions(coin=coin):
        if str(pos.get("productId")) == pid:
            staked += _f(pos, "amount", f"earn position {pid}")
    orders = []
    for o in client.get_earn_orders():
        if str(o.get("coin") or "").upper() not in ("", coin):
            continue
        wo = "earn order"
        created = _opt_f(o, "createdAt")
        orders.append(EarnOrder(_s(o, "orderId", wo), str(o.get("orderLinkId") or ""),
                                _s(o, "orderType", wo), _s(o, "status", wo),
                                _opt_f(o, "orderValue"), int(created) if created else None))
    return Earn(pid, _s(p, "status", where), _pct(p.get("estimateApr"), where),
                _opt_f(p, "redeemProcessingMinute"), staked, tuple(orders),
                _opt_f(p, "minStakeAmount"))


def take(client, cfg: Mapping, now_ms: Optional[int] = None, private: bool = True) -> Snapshot:
    """Read one snapshot. private=False reads market data only (the private
    sections are then absent with reason "not read")."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    symbols = tuple(cfg["SYMBOLS"])
    history = min(200, int(cfg["SMOOTHING_SETTLEMENTS"]) + 2)
    errors: Dict[str, str] = {}
    stale: Dict[str, str] = {}
    region = False

    def attempt(section: str, fn: Callable):
        nonlocal region
        try:
            return fn()
        except PARSE_ERRORS as e:
            errors[section] = f"{type(e).__name__}: {e}"
            region = region or is_region_restricted(e)
            return None

    markets: Dict[str, Market] = {}
    for sym in symbols:
        m = attempt(f"market:{sym}", lambda s=sym: read_market(client, s, history))
        if m is not None:
            markets[sym] = m
            why = market_staleness(m, now)
            if why:
                stale[f"market:{sym}"] = why

    positions: Dict[str, PerpPosition] = {}
    account = open_orders = earn = None
    if private:
        for sym in symbols:
            p = attempt(f"positions:{sym}", lambda s=sym: read_position(client, s))
            if p is not None:
                positions[sym] = p
        base = tuple(sorted({s[:-len("USDT")] for s in symbols}))
        account = attempt("account", lambda: read_account(client, symbols, base))
        open_orders = attempt("orders", lambda: read_open_orders(client))
        earn = attempt("earn", lambda: read_earn(client, str(cfg["EARN_COIN"]).upper()))
    else:
        for section in [f"positions:{s}" for s in symbols] + ["account", "orders", "earn"]:
            errors[section] = "not read"

    return Snapshot(now, bool(getattr(client, "testnet", False)), symbols,
                    MappingProxyType(markets), MappingProxyType(positions), account, open_orders,
                    earn, MappingProxyType(errors), region, MappingProxyType(stale))
