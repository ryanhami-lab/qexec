"""Unit tests for :class:`qexec.reference.book.ReferenceBook`.

Covers validation matrix:
* T01 — add/cancel/modify/reset produce correct visible state and priority.
* T04 — snapshot initialization yields state without false order-flow features.
* T34 — size/price modification: correct priority loss/retention and queue relation.
* T36 — refresh/ambiguity (MODIFY of unknown order) tagged, not fabricated.

Plus required extra behaviors: TRADE/FILL/CANCEL triplet mutates once (only CANCEL),
copy() independence, invariants detect a crossed committed book, MODIFY-unknown ->
anomaly (and -> add when modify_unknown_as_add=True), CLEAR then snapshot re-init.
"""

from __future__ import annotations

from _helpers import TICK, instrument, rec, single_batch

from qexec.core.types import (
    Action,
    BatchKind,
    OrderKey,
    RecordFlag,
    Side,
    TradingStatus,
)
from qexec.reference.book import ReferenceBook

BID = 100_000_000_000  # 100.0
BID2 = BID - TICK  # 99.75
ASK = 100_250_000_000  # 100.25


def _key(order_id: int) -> OrderKey:
    return OrderKey(1, 1, 0, order_id)


# --------------------------------------------------------------------------- T01


def test_add_cancel_modify_reset_visible_state_and_priority() -> None:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)

    # Three bids at the same price arrive in order 10, 11, 12.
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=10))
    book.apply_record(rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=11))
    book.apply_record(rec(2, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=12))

    # Priority order is insertion order: 10, 11, 12. Total 5+3+2 = 10.
    at = book.orders_at(Side.BID, BID)
    assert [o.key.order_id for o in at] == [10, 11, 12]
    best_bid, best_ask = book.best_bid_ask()
    assert best_bid is not None and best_bid.quantity == 10 and best_bid.order_count == 3
    assert best_ask is None

    # Quantity ahead of order 12 is 5 + 3 = 8; ahead of 10 is 0.
    assert book.visible_quantity_ahead(_key(12)) == 8
    assert book.visible_quantity_ahead(_key(10)) == 0

    # Partial cancel of order 10: remove 2 of 5, retains priority (still at front).
    book.apply_record(
        rec(3, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=10)
    )
    assert book.get_order(_key(10)) is not None
    assert book.get_order(_key(10)).quantity == 3  # type: ignore[union-attr]
    assert [o.key.order_id for o in book.orders_at(Side.BID, BID)] == [10, 11, 12]
    # Ahead of 12 is now 3 + 3 = 6.
    assert book.visible_quantity_ahead(_key(12)) == 6

    # Full cancel of order 11 (remove all 3) removes it.
    book.apply_record(
        rec(4, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=11)
    )
    assert book.get_order(_key(11)) is None
    assert [o.key.order_id for o in book.orders_at(Side.BID, BID)] == [10, 12]

    # CLEAR (reset) removes everything.
    book.apply_record(rec(5, Action.CLEAR, side=Side.NONE))
    assert book.best_bid_ask() == (None, None)
    assert book.orders_at(Side.BID, BID) == ()


# --------------------------------------------------------------------------- T04


def test_snapshot_initialization_no_order_flow_features() -> None:
    book = ReferenceBook(instrument())
    # Snapshot-flagged adds in priority order close the init event with LAST.
    recs = [
        rec(
            0,
            Action.ADD,
            side=Side.BID,
            price_fixed=BID,
            quantity=4,
            order_id=1,
            flags=RecordFlag.SNAPSHOT,
        ),
        rec(
            1,
            Action.ADD,
            side=Side.ASK,
            price_fixed=ASK,
            quantity=6,
            order_id=2,
            flags=RecordFlag.SNAPSHOT,
        ),
    ]
    batch = single_batch(recs)
    # Kind is INITIALIZATION: callers must not treat these as economic order flow.
    assert batch.kind is BatchKind.INITIALIZATION
    book.set_status(TradingStatus.TRADING)
    snap = book.apply_batch(batch)
    assert snap.best_bid is not None and snap.best_bid.quantity == 4
    assert snap.best_ask is not None and snap.best_ask.quantity == 6
    # mid2 = bid + ask exactly.
    assert snap.mid2() == BID + ASK


# --------------------------------------------------------------------------- T34


def test_modify_price_change_loses_priority() -> None:
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID2, quantity=5, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.BID, price_fixed=BID2, quantity=5, order_id=2))
    seq1_before = book.get_order(_key(1)).priority_seq  # type: ignore[union-attr]

    # Order 1 modifies price up to BID (a different level) -> loses priority, back of BID.
    book.apply_record(rec(2, Action.MODIFY, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    moved = book.get_order(_key(1))
    assert moved is not None and moved.price_fixed == BID
    assert moved.priority_seq > seq1_before  # fresh (larger) priority

    # Add order 3 at BID before 1 was moved? No: 1 moved to BID first and is alone there.
    book.apply_record(rec(3, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=3))
    # At BID, order 1 (moved) is ahead of order 3.
    assert [o.key.order_id for o in book.orders_at(Side.BID, BID)] == [1, 3]


def test_modify_size_increase_loses_priority_decrease_retains() -> None:
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=2))
    seq1 = book.get_order(_key(1)).priority_seq  # type: ignore[union-attr]

    # Size DECREASE on order 1 (5 -> 3): retains priority (same seq, still front).
    book.apply_record(rec(2, Action.MODIFY, side=Side.ASK, price_fixed=ASK, quantity=3, order_id=1))
    assert book.get_order(_key(1)).priority_seq == seq1  # type: ignore[union-attr]
    assert book.get_order(_key(1)).quantity == 3  # type: ignore[union-attr]
    assert [o.key.order_id for o in book.orders_at(Side.ASK, ASK)] == [1, 2]
    assert book.visible_quantity_ahead(_key(2)) == 3

    # Size INCREASE on order 1 (3 -> 9): loses priority, moves behind order 2.
    book.apply_record(rec(3, Action.MODIFY, side=Side.ASK, price_fixed=ASK, quantity=9, order_id=1))
    assert book.get_order(_key(1)).priority_seq > seq1  # type: ignore[union-attr]
    assert [o.key.order_id for o in book.orders_at(Side.ASK, ASK)] == [2, 1]
    # Ahead of order 1 is now order 2's full 5.
    assert book.visible_quantity_ahead(_key(1)) == 5


# --------------------------------------------------------------------------- T36


def test_modify_unknown_order_is_anomaly_not_add() -> None:
    book = ReferenceBook(instrument())  # modify_unknown_as_add defaults to False
    book.apply_record(
        rec(0, Action.MODIFY, side=Side.BID, price_fixed=BID, quantity=5, order_id=99)
    )
    assert book.get_order(_key(99)) is None
    assert len(book.anomalies) == 1
    assert book.anomalies[0].kind == "MODIFY_UNKNOWN"
    assert book.anomalies[0].source_ordinal == 0


def test_modify_unknown_as_add_when_enabled() -> None:
    book = ReferenceBook(instrument(), modify_unknown_as_add=True)
    book.apply_record(
        rec(0, Action.MODIFY, side=Side.BID, price_fixed=BID, quantity=5, order_id=99)
    )
    added = book.get_order(_key(99))
    assert added is not None and added.quantity == 5 and added.price_fixed == BID
    assert book.anomalies == []


# -------------------------------------------------- TRADE/FILL/CANCEL triplet


def test_trade_fill_cancel_triplet_mutates_once() -> None:
    """An aggressive execution emits TRADE, FILL, CANCEL. Only CANCEL mutates the book."""
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    assert book.best_bid_ask()[0].quantity == 5  # type: ignore[union-attr]

    # Aggressor SELL hits resting bid for 3: TRADE (aggressor ASK), FILL (resting BID), CANCEL.
    book.apply_record(rec(1, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=3, order_id=0))
    assert book.best_bid_ask()[0].quantity == 5  # TRADE did not mutate # type: ignore[union-attr]
    book.apply_record(rec(2, Action.FILL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1))
    assert book.best_bid_ask()[0].quantity == 5  # FILL did not mutate # type: ignore[union-attr]
    book.apply_record(rec(3, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1))
    # Only the CANCEL mutated: 5 - 3 = 2 remain.
    assert book.best_bid_ask()[0].quantity == 2  # type: ignore[union-attr]
    assert book.get_order(_key(1)).quantity == 2  # type: ignore[union-attr]


# -------------------------------------------------- copy() independence


def test_copy_is_independent() -> None:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    clone = book.copy()

    # Mutate clone only.
    clone.apply_record(
        rec(1, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=5, order_id=1)
    )
    assert clone.get_order(_key(1)) is None
    assert book.get_order(_key(1)) is not None  # original unaffected
    assert book.best_bid_ask()[0].quantity == 5  # type: ignore[union-attr]

    # Mutate original only; clone unaffected.
    book.apply_record(rec(2, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=7, order_id=2))
    assert book.get_order(_key(2)) is not None
    assert clone.get_order(_key(2)) is None


# -------------------------------------------------- invariants: crossed book


def test_invariants_detect_crossed_committed_book() -> None:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    # Bid at 100.25 and ask at 100.00 -> crossed (bid >= ask).
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=ASK, quantity=1, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.ASK, price_fixed=BID, quantity=1, order_id=2))
    problems = book.validate_invariants()
    assert any("crossed book" in p for p in problems)

    # Not flagged when not TRADING (crossing only assessed in eligible trading state).
    book.set_status(TradingStatus.HALTED)
    assert not any("crossed book" in p for p in book.validate_invariants())


def test_commit_batch_records_invariant_anomaly_for_crossed_book() -> None:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    batch = single_batch(
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=ASK, quantity=1, order_id=1),
            rec(1, Action.ADD, side=Side.ASK, price_fixed=BID, quantity=1, order_id=2),
        ]
    )
    book.apply_batch(batch)
    assert any(a.kind == "INVARIANT_VIOLATION" for a in book.anomalies)


# -------------------------------------------------- CLEAR then snapshot re-init


def test_clear_then_snapshot_reinit() -> None:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=2))

    # CLEAR + snapshot adds rebuild the book from scratch.
    reinit = single_batch(
        [
            rec(2, Action.CLEAR, side=Side.NONE),
            rec(
                3,
                Action.ADD,
                side=Side.BID,
                price_fixed=BID,
                quantity=8,
                order_id=10,
                flags=RecordFlag.SNAPSHOT,
            ),
            rec(
                4,
                Action.ADD,
                side=Side.ASK,
                price_fixed=ASK,
                quantity=9,
                order_id=11,
                flags=RecordFlag.SNAPSHOT,
            ),
        ]
    )
    snap = book.apply_batch(reinit)
    assert book.get_order(_key(1)) is None  # old orders gone
    assert book.get_order(_key(2)) is None
    assert snap.best_bid is not None and snap.best_bid.quantity == 8
    assert snap.best_ask is not None and snap.best_ask.quantity == 9


# -------------------------------------------------- snapshot depth and status


def test_snapshot_depth_limit_and_status() -> None:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    # Three bid levels: 100.00, 99.75, 99.50.
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=1, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.BID, price_fixed=BID2, quantity=2, order_id=2))
    book.apply_record(
        rec(2, Action.ADD, side=Side.BID, price_fixed=BID2 - TICK, quantity=3, order_id=3)
    )
    snap = book.snapshot(depth=2)
    # Best-first (descending for bids), limited to 2 levels.
    assert [lv.price_fixed for lv in snap.bids] == [BID, BID2]
    assert snap.status is TradingStatus.TRADING


def test_non_mutating_records_are_noops() -> None:
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.NONE, side=Side.NONE))
    book.apply_record(rec(1, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=5))
    book.apply_record(rec(2, Action.FILL, side=Side.BID, price_fixed=BID, quantity=5, order_id=7))
    assert book.best_bid_ask() == (None, None)
    assert book.anomalies == []
