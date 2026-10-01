"""CARRY_PLAN §7, Phase 4 acceptance: every failure point simulated; no
scenario leaves an orphan leg after LEG_TIMEOUT_S.

Hypothesis draws a fault script for every call the execution makes (each
order kind, the lookups, the orderbooks): rejections, lost sends, timeouts
after execution, partial and empty fills, unreadable lookups and books.
Invariants, on the exchange's truth:
  ENTER  the short never exceeds the spot bought; the book matches the
         exchange (or the spot order is journaled as unknown); any orphan
         protection escalates to NO_NEW_POSITIONS; all within the timeouts.
  EXIT   the short is closed and the book's spot sold (or the run escalates);
         foreign coins are never touched.
"""

from types import MappingProxyType

from hypothesis import given, settings
from hypothesis import strategies as st

from carry import execute as ex
from carry.plan import Action, Plan, SymbolBook
from carry_builders import NOW, account, cfg, short, snapshot
from fake_exchange import FakeClock, FakeExchange

T = 30
ORDER_FAULTS = st.sampled_from(["reject", "lost", "timeout", ("partial", 0.5), ("partial", 0.0)])
CFG = cfg()


@st.composite
def faults(draw, order_kinds):
    script = {}
    for kind in order_kinds:
        script[kind] = draw(st.lists(ORDER_FAULTS, max_size=3))
    for kind in ("find:linear", "find:spot"):
        script[kind] = draw(st.lists(st.sampled_from(["error", "hidden"]), max_size=3))
    for kind in ("book:linear", "book:spot"):
        script[kind] = draw(st.lists(st.just("error"), max_size=2))
    return script


def plan_of(a):
    return Plan((a,), (), MappingProxyType({}), MappingProxyType({}))


SETTINGS = settings(max_examples=300, deadline=None, derandomize=True)


@SETTINGS
@given(faults(["linear:Sell", "spot:Buy", "linear:Buy"]), st.sampled_from([0.0, 0.5]),
       st.sampled_from([True, False]))
def test_entry_never_leaves_an_orphan(script, foreign, fee_detail):
    clock = FakeClock()
    x = FakeExchange(clock, spot=foreign, fee_detail=fee_detail)
    for k, v in script.items():
        x.script(k, *v)
    a = Action("ENTER", "ETHUSDT", "t", legs=("perp", "spot"), perp_qty=0.03, spot_qty=0.03004,
               usdt=75.1)
    start = clock()
    r = ex.execute_plan(plan_of(a), snapshot(acct=account(usdt=100.0)), CFG, "NORMAL", {}, "c1", x,
                        clock=clock, sleep=clock.sleep)
    ours = x.spot - foreign
    assert x.short <= ours + 1e-9, (x.short, ours, r.alerts)            # never a naked short
    assert clock() - start <= 3 * T + 5, clock() - start
    b = r.book_updates.get("ETHUSDT", SymbolBook())
    assert b.perp_qty == x.short or r.escalate == ex.ESCALATE, (b, x.short, r.alerts)
    if not b.pending_spot:
        assert abs(b.spot_qty - ours) < 1e-9, (b, ours)
    else:
        assert b.spot_qty <= ours + 1e-9
        assert x.short == 0
    if any(al.startswith("ORPHAN_LEG") for al in r.alerts):
        assert r.escalate == ex.ESCALATE
    if x.short > 0:
        # hedged within one perp step: the leftover spot is the book's
        assert ours - x.short < 0.01 + 1e-9 or abs(b.spot_qty - ours) < 1e-9


@SETTINGS
@given(faults(["spot:Sell", "linear:Buy"]), st.sampled_from([0.0, 0.5]),
       st.sampled_from([("spot", "perp"), ("perp", "spot")]))
def test_exit_closes_everything_ours_and_nothing_else(script, foreign, legs):
    clock = FakeClock()
    x = FakeExchange(clock, short=0.03, spot=0.03 + foreign)
    for k, v in script.items():
        x.script(k, *v)
    book = {"ETHUSDT": SymbolBook(status="OPEN", entered_ms=NOW, perp_qty=0.03, spot_qty=0.03)}
    snap = snapshot(positions={"ETHUSDT": short(size=0.03)},
                    acct=account(usdt=16.0, coins={"ETH": 0.03 + foreign}))
    a = Action("EXIT", "ETHUSDT", "t", legs=legs, spot_qty=0.03)
    start = clock()
    r = ex.execute_plan(plan_of(a), snap, CFG, "UNWIND", book, "c1", x, clock=clock,
                        sleep=clock.sleep)
    assert x.spot >= foreign - 1e-9                                    # foreign coins untouched
    assert clock() - start <= 4 * T + 5
    b = r.book_updates["ETHUSDT"]
    if r.escalate is None:
        assert x.short == 0, r.alerts
        assert abs(x.spot - foreign) < 1e-9 or b.pending_spot, (x.spot, r.alerts)
    if not b.pending_spot:
        assert abs(b.spot_qty - (x.spot - foreign)) < 1e-9, (b, x.spot)
