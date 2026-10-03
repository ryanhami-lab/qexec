"""Tests for :class:`qexec.features.market.MarketFeatureState` with hand-computed values.

Covers: imbalance, spread, trade-flow sign, cancel-vs-execution reduction distinction, window
eviction, copy independence, quote age, mid volatility, and the T22 leakage property.
"""

from __future__ import annotations

import math

from _fhelpers import TICK, instrument, make_batch, rec

from qexec.core.types import Action, BatchKind, Side
from qexec.features.market import MarketFeatureState
from qexec.reference.book import ReferenceBook

BID = 100_000_000_000  # 100.0
ASK = 100_250_000_000  # 100.25 (one tick above)

S = 1_000_000_000
MS = 1_000_000


def _fresh() -> tuple[MarketFeatureState, ReferenceBook]:
    inst = instrument()
    return MarketFeatureState(inst), ReferenceBook(inst)


def _init_book(book: ReferenceBook) -> None:
    """Seed a simple two-sided book: bid 100.0 x5 (two orders), ask 100.25 x3."""
    recs = [
        rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
        rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=2),
        rec(2, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=3, order_id=3),
    ]
    for r in recs:
        book.apply_record(r)


# --------------------------------------------------------------------------- imbalance / spread


def test_imbalance_and_spread_hand_values() -> None:
    state, book = _fresh()
    _init_book(book)
    # bid_qty_1 = 5, ask_qty_1 = 3 -> imbalance = (5-3)/(5+3) = 0.25.
    f = state.features(now_ns=10 * S, client_book=book)
    assert f["bid_qty_1"] == 5.0
    assert f["ask_qty_1"] == 3.0
    assert f["imbalance_1"] == 0.25
    # spread = (ASK - BID)/tick = one tick.
    assert f["spread_ticks"] == 1.0


def test_imbalance_zero_when_both_sides_empty() -> None:
    state, book = _fresh()
    f = state.features(now_ns=10 * S, client_book=book)
    assert f["imbalance_1"] == 0.0
    assert f["imbalance_3"] == 0.0
    # Spread sentinel is 0.0 when a side is missing (NaN disallowed by contract).
    assert f["spread_ticks"] == 0.0
    assert math.isfinite(f["spread_ticks"])


def test_depth3_and_imbalance3() -> None:
    state, book = _fresh()
    # Bids at three levels: 100.00 x4, 99.75 x3, 99.50 x2 -> depth_bid_3 = 9.
    # Asks: 100.25 x1, 100.50 x1 -> depth_ask_3 = 2.
    for i, (px, qty) in enumerate([(BID, 4), (BID - TICK, 3), (BID - 2 * TICK, 2)]):
        book.apply_record(
            rec(i, Action.ADD, side=Side.BID, price_fixed=px, quantity=qty, order_id=10 + i)
        )
    book.apply_record(rec(5, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=1, order_id=20))
    book.apply_record(
        rec(6, Action.ADD, side=Side.ASK, price_fixed=ASK + TICK, quantity=1, order_id=21)
    )
    f = state.features(now_ns=10 * S, client_book=book)
    assert f["depth_bid_3"] == 9.0
    assert f["depth_ask_3"] == 2.0
    # imbalance_3 = (9-2)/(9+2) = 7/11.
    assert math.isclose(f["imbalance_3"], 7.0 / 11.0)


# --------------------------------------------------------------------------- trade flow sign


def test_trade_flow_sign_buy_positive_sell_negative() -> None:
    state, book = _fresh()
    _init_book(book)
    # Aggressor BID (buyer) qty 4 at t=1s observation.
    b1 = make_batch(
        [rec(10, Action.TRADE, side=Side.BID, price_fixed=ASK, quantity=4, order_id=0)],
        batch_id=1,
    )
    state.on_delivered(b1, book, observation_time_ns=1 * S)
    # Aggressor ASK (seller) qty 1 at t=1.5s.
    b2 = make_batch(
        [rec(11, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0)],
        batch_id=2,
    )
    state.on_delivered(b2, book, observation_time_ns=S + 500 * MS)
    # At now = 2s both samples are within the 1s window? window is (now-1s, now].
    # now=2s -> lo=1s; sample at 1s excluded (obs>lo strict), sample at 1.5s included -> -1.
    f = state.features(now_ns=2 * S, client_book=book)
    assert f["trade_flow_signed_1s"] == -1.0
    assert f["trade_count_1s"] == 1.0
    # At now = 1.5s both inside (lo=0.5s): +4 -1 = +3, two trades.
    f2 = state.features(now_ns=S + 500 * MS, client_book=book)
    assert f2["trade_flow_signed_1s"] == 3.0
    assert f2["trade_count_1s"] == 2.0


# --------------------------------------------------------------------------- cancel vs execution


def test_cancel_vs_execution_reduction_distinction() -> None:
    state, book = _fresh()
    _init_book(book)
    # Build a LIVE batch holding BOTH a pure cancel and an execution triplet on the bid side.
    # Pure cancel: CANCEL of order 2 (bid) qty 2 (true cancellation) -> counts.
    # Execution: TRADE (aggressor ask) + FILL(order 1 bid) + CANCEL(order 1 bid qty 3) -> the
    # trailing CANCEL is execution-driven and must NOT count as cancel flow; it is counted once
    # via the TRADE's signed flow.
    recs = [
        rec(20, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=2),
        rec(21, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=3, order_id=0),
        rec(22, Action.FILL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
        rec(23, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
    ]
    batch = make_batch(recs, batch_id=1)
    state.on_delivered(batch, book, observation_time_ns=500 * MS)
    f = state.features(now_ns=600 * MS, client_book=book)
    # Only the pure cancel of qty 2 counts on the bid; the execution-driven cancel of qty 3
    # does not.
    assert f["cancel_qty_bid_1s"] == 2.0
    assert f["cancel_qty_ask_1s"] == 0.0
    # The execution shows up once as signed trade flow: aggressor ASK qty 3 -> -3.
    assert f["trade_flow_signed_1s"] == -3.0
    assert f["trade_count_1s"] == 1.0


def test_add_qty_bucketed_by_side() -> None:
    state, book = _fresh()
    recs = [
        rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1),
        rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=7, order_id=2),
    ]
    batch = make_batch(recs, batch_id=1)
    state.on_delivered(batch, book, observation_time_ns=100 * MS)
    f = state.features(now_ns=200 * MS, client_book=book)
    assert f["add_qty_bid_1s"] == 5.0
    assert f["add_qty_ask_1s"] == 7.0


def test_initialization_batch_updates_no_flow() -> None:
    state, book = _fresh()
    # Force an INITIALIZATION batch containing an ADD and a TRADE; none should count.
    recs = [
        rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1),
        rec(1, Action.TRADE, side=Side.BID, price_fixed=ASK, quantity=9, order_id=0),
    ]
    for r in recs:
        book.apply_record(r)
    batch = make_batch(recs, batch_id=0, kind=BatchKind.INITIALIZATION)
    state.on_delivered(batch, book, observation_time_ns=100 * MS)
    f = state.features(now_ns=200 * MS, client_book=book)
    assert f["add_qty_bid_1s"] == 0.0
    assert f["trade_flow_signed_1s"] == 0.0
    assert f["trade_count_1s"] == 0.0
    # No quote-age change was recorded from an INITIALIZATION batch.
    assert f["quote_age_ms"] == 0.0


# --------------------------------------------------------------------------- window eviction


def test_window_eviction_drops_old_flow() -> None:
    state, book = _fresh()
    _init_book(book)
    old = make_batch(
        [rec(10, Action.TRADE, side=Side.BID, price_fixed=ASK, quantity=5, order_id=0)],
        batch_id=1,
    )
    state.on_delivered(old, book, observation_time_ns=1 * S)
    # At now=5s the 1s trade sample is far outside the 1s flow window -> flow 0.
    f = state.features(now_ns=5 * S, client_book=book)
    assert f["trade_flow_signed_1s"] == 0.0
    assert f["trade_count_1s"] == 0.0


# --------------------------------------------------------------------------- quote age


def test_quote_age_tracks_last_quote_change() -> None:
    state, book = _fresh()
    _init_book(book)
    # A delivered batch that changes the best bid size (adds qty) at obs=2s.
    book.apply_record(rec(30, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=99))
    batch = make_batch(
        [rec(30, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=99)],
        batch_id=1,
    )
    state.on_delivered(batch, book, observation_time_ns=2 * S)
    # now = 2.5s -> quote age = 500 ms.
    f = state.features(now_ns=2 * S + 500 * MS, client_book=book)
    assert f["quote_age_ms"] == 500.0


# --------------------------------------------------------------------------- mid volatility


def test_mid_vol_population_std_of_tick_changes() -> None:
    state, book = _fresh()
    inst = instrument()
    # Deliver three LIVE batches producing committed mids that move the mid by +1 then -1 tick.
    # Start: bid 100.00, ask 100.25 -> mid2 = BID+ASK. Then raise bid to 100.25? that crosses.
    # Instead move the ask up a tick, then back, keeping an uncrossed book.
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=2))
    b0 = make_batch(
        [rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1)], batch_id=1
    )
    state.on_delivered(b0, book, observation_time_ns=0)
    mid0 = book.snapshot(depth=1).mid2()

    # Move best ask up one tick: cancel order 2, add new ask at ASK+TICK.
    book.apply_record(rec(2, Action.CANCEL, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=2))
    book.apply_record(
        rec(3, Action.ADD, side=Side.ASK, price_fixed=ASK + TICK, quantity=5, order_id=3)
    )
    b1 = make_batch(
        [rec(3, Action.ADD, side=Side.ASK, price_fixed=ASK + TICK, quantity=5, order_id=3)],
        batch_id=2,
    )
    state.on_delivered(b1, book, observation_time_ns=1 * S)
    mid1 = book.snapshot(depth=1).mid2()

    # Move best ask back down one tick.
    book.apply_record(
        rec(4, Action.CANCEL, side=Side.ASK, price_fixed=ASK + TICK, quantity=5, order_id=3)
    )
    book.apply_record(rec(5, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=4))
    b2 = make_batch(
        [rec(5, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=4)], batch_id=3
    )
    state.on_delivered(b2, book, observation_time_ns=2 * S)
    mid2 = book.snapshot(depth=1).mid2()

    assert mid0 is not None and mid1 is not None and mid2 is not None
    # The best ask moved up one tick then back one tick, so the MID moved by half a tick each
    # time (moving one side by one tick moves the midpoint by half a tick). Mid changes in ticks:
    # +0.5 then -0.5. Population std of [+0.5, -0.5]: mean 0, var = (0.25 + 0.25)/2 = 0.25,
    # std = 0.5.
    f = state.features(now_ns=2 * S, client_book=book)
    assert math.isclose(f["mid_vol_5s"], 0.5)
    # mid_change over the 1s window at now=2s: samples with obs in (1s, 2s] = only the 2s sample
    # -> fewer than 2 -> 0.0.
    assert f["mid_change_1s_ticks"] == 0.0
    _ = inst


def test_mid_vol_zero_with_fewer_than_two_samples() -> None:
    state, book = _fresh()
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=2))
    b0 = make_batch(
        [rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1)], batch_id=1
    )
    state.on_delivered(b0, book, observation_time_ns=0)
    f = state.features(now_ns=1 * S, client_book=book)
    assert f["mid_vol_5s"] == 0.0


# --------------------------------------------------------------------------- copy independence


def test_copy_is_independent() -> None:
    state, book = _fresh()
    _init_book(book)
    b1 = make_batch(
        [rec(10, Action.TRADE, side=Side.BID, price_fixed=ASK, quantity=4, order_id=0)],
        batch_id=1,
    )
    state.on_delivered(b1, book, observation_time_ns=500 * MS)
    clone = state.copy()
    # Mutate the clone with a new trade; the original must be unaffected.
    b2 = make_batch(
        [rec(11, Action.TRADE, side=Side.BID, price_fixed=ASK, quantity=7, order_id=0)],
        batch_id=2,
    )
    clone.on_delivered(b2, book, observation_time_ns=600 * MS)
    f_orig = state.features(now_ns=700 * MS, client_book=book)
    f_clone = clone.features(now_ns=700 * MS, client_book=book)
    assert f_orig["trade_flow_signed_1s"] == 4.0
    assert f_clone["trade_flow_signed_1s"] == 11.0


# --------------------------------------------------------------------------- T22 leakage


def test_t22_future_perturbation_leaves_earlier_features_unchanged() -> None:
    """Two delivered-batch streams identical up to time t and different after; features at
    times <= t are identical."""
    inst = instrument()
    t = 2 * S

    def build(diverge: bool) -> tuple[MarketFeatureState, ReferenceBook]:
        state = MarketFeatureState(inst)
        book = ReferenceBook(inst)
        _init_book(book)
        # Common prefix: a trade at obs=1s (<= t).
        common = make_batch(
            [rec(10, Action.TRADE, side=Side.BID, price_fixed=ASK, quantity=3, order_id=0)],
            batch_id=1,
        )
        state.on_delivered(common, book.copy(), observation_time_ns=1 * S)
        # Also a common batch exactly at t.
        at_t = make_batch(
            [rec(11, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=2, order_id=0)],
            batch_id=2,
        )
        state.on_delivered(at_t, book.copy(), observation_time_ns=t)
        # Divergent future (obs strictly after t) only in one stream.
        if diverge:
            future = make_batch(
                [rec(12, Action.TRADE, side=Side.BID, price_fixed=ASK, quantity=50, order_id=0)],
                batch_id=3,
            )
            state.on_delivered(future, book.copy(), observation_time_ns=t + 100 * MS)
        return state, book

    s_a, book_a = build(diverge=False)
    s_b, book_b = build(diverge=True)

    # Features at every time <= t must match exactly despite the future divergence.
    for now in (500 * MS, 1 * S, S + 500 * MS, t):
        fa = s_a.features(now_ns=now, client_book=book_a)
        fb = s_b.features(now_ns=now, client_book=book_b)
        assert fa == fb, f"leakage at now={now}"
