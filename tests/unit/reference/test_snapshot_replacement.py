"""Complete normalized snapshots replace state without erasing audit history."""

from __future__ import annotations

from _helpers import TICK, instrument, rec, single_batch

from qexec.core.types import Action, OrderKey, RecordFlag, Side, TradingStatus
from qexec.reference.book import ReferenceBook

BID = 100_000_000_000
ASK = BID + TICK


def _key(order_id: int) -> OrderKey:
    return OrderKey(1, 1, 0, order_id)


def _seed() -> ReferenceBook:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    book.apply_batch(
        single_batch(
            [
                rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=10, order_id=1),
                rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=10, order_id=2),
            ]
        )
    )
    return book


def test_bare_complete_snapshot_replaces_old_keys_and_best_levels() -> None:
    book = _seed()
    # Unknown cancel establishes audit history that reconstruction must retain.
    book.apply_batch(single_batch([rec(2, Action.CANCEL, side=Side.BID, quantity=1, order_id=999)]))
    anomalies = tuple(book.anomalies)
    snap = book.apply_batch(
        single_batch(
            [
                rec(
                    3,
                    Action.ADD,
                    side=Side.BID,
                    price_fixed=BID - 2 * TICK,
                    quantity=5,
                    order_id=11,
                    flags=RecordFlag.SNAPSHOT,
                ),
                rec(
                    4,
                    Action.ADD,
                    side=Side.ASK,
                    price_fixed=ASK + TICK,
                    quantity=6,
                    order_id=12,
                    flags=RecordFlag.SNAPSHOT,
                ),
            ]
        )
    )
    # Old 100/100.25 must disappear, leaving exactly 99.50/100.50, midpoint 100.
    assert snap.mid2() == 200_000_000_000
    assert snap.best_bid is not None and snap.best_bid.price_fixed == BID - 2 * TICK
    assert snap.best_ask is not None and snap.best_ask.price_fixed == ASK + TICK
    assert book.get_order(_key(1)) is None
    assert book.get_order(_key(2)) is None
    assert book.orders_at(Side.BID, BID) == ()
    assert book.orders_at(Side.ASK, ASK) == ()
    assert len(snap.bids) == len(snap.asks) == 1
    assert tuple(book.anomalies) == anomalies
    assert book.validate_invariants() == []


def test_reused_snapshot_keys_receive_fresh_monotone_fifo_priority() -> None:
    book = _seed()
    old = book.get_order(_key(2))
    assert old is not None
    book.apply_batch(
        single_batch(
            [
                rec(
                    2,
                    Action.ADD,
                    side=Side.BID,
                    price_fixed=BID,
                    quantity=3,
                    order_id=1,
                    flags=RecordFlag.SNAPSHOT,
                ),
                rec(
                    3,
                    Action.ADD,
                    side=Side.BID,
                    price_fixed=BID,
                    quantity=7,
                    order_id=3,
                    flags=RecordFlag.SNAPSHOT,
                ),
                rec(
                    4,
                    Action.ADD,
                    side=Side.ASK,
                    price_fixed=ASK,
                    quantity=4,
                    order_id=2,
                    flags=RecordFlag.SNAPSHOT,
                ),
            ]
        )
    )
    bid_orders = book.orders_at(Side.BID, BID)
    assert [o.key.order_id for o in bid_orders] == [1, 3]
    assert [o.quantity for o in bid_orders] == [3, 7]
    assert old.priority_seq < bid_orders[0].priority_seq < bid_orders[1].priority_seq
    assert book.visible_quantity_ahead(_key(3)) == 3
    assert book.anomalies == []


def test_record_driven_begin_batch_matches_apply_batch_and_preserves_status() -> None:
    incremental, atomic = _seed(), _seed()
    incremental.set_status(TradingStatus.HALTED)
    atomic.set_status(TradingStatus.HALTED)
    batch = single_batch(
        [
            rec(2, Action.CLEAR, side=Side.NONE),
            rec(
                3,
                Action.ADD,
                side=Side.BID,
                price_fixed=BID - TICK,
                quantity=2,
                order_id=10,
                flags=RecordFlag.SNAPSHOT,
            ),
            rec(
                4,
                Action.ADD,
                side=Side.ASK,
                price_fixed=ASK,
                quantity=8,
                order_id=11,
                flags=RecordFlag.SNAPSHOT,
            ),
        ]
    )
    incremental.begin_batch(batch)
    assert incremental.best_bid_ask() == (None, None)
    for record in batch.records:
        incremental.apply_record(record)
    assert incremental.commit_batch(batch) == atomic.apply_batch(batch)
    assert incremental.status is TradingStatus.HALTED


def test_begin_live_batch_preserves_resting_state_and_priority() -> None:
    book = _seed()
    old = book.get_order(_key(1))
    batch = single_batch(
        [rec(2, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=3)]
    )
    book.begin_batch(batch)
    assert book.get_order(_key(1)) == old
    assert book.get_order(_key(2)) is not None
    for record in batch.records:
        book.apply_record(record)
    book.commit_batch(batch)
    assert book.get_order(_key(1)) == old
    assert book.visible_quantity_ahead(_key(3)) == 10
