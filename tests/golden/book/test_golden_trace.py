"""Golden multi-event trace for :class:`ReferenceBook`.

Every expected snapshot below is hand-derived (engineering contract rule 6): the numbers
are computed by hand in the comments, not recorded from the implementation's own output.

Instrument: tick = 0.25 => 250_000_000 fixed units.
Price grid (fixed units):
    99.50 = 99_500_000_000
    99.75 = 99_750_000_000
   100.00 = 100_000_000_000
   100.25 = 100_250_000_000
"""

from __future__ import annotations

from _helpers import instrument, rec, single_batch

from qexec.core.types import Action, OrderKey, RecordFlag, Side, TradingStatus
from qexec.reference.book import ReferenceBook

P_99_50 = 99_500_000_000
P_99_75 = 99_750_000_000
P_100_00 = 100_000_000_000
P_100_25 = 100_250_000_000


def test_golden_multi_event_trace() -> None:
    book = ReferenceBook(instrument())

    # ------------------------------------------------------------------ Batch 0 (INIT)
    # Snapshot initialization. Resting state after this event, by hand:
    #   BID 100.00 : order 1 qty 10            -> level qty 10, 1 order
    #   BID  99.75 : order 2 qty 20            -> level qty 20, 1 order
    #   ASK 100.25 : order 3 qty 15            -> level qty 15, 1 order
    #   ASK 100.50 : order 4 qty 25            -> level qty 25, 1 order
    # Best bid 100.00 (10), best ask 100.25 (15). mid2 = 100.00 + 100.25 (fixed).
    init = single_batch(
        [
            rec(
                0,
                Action.ADD,
                side=Side.BID,
                price_fixed=P_100_00,
                quantity=10,
                order_id=1,
                flags=RecordFlag.SNAPSHOT,
            ),
            rec(
                1,
                Action.ADD,
                side=Side.BID,
                price_fixed=P_99_75,
                quantity=20,
                order_id=2,
                flags=RecordFlag.SNAPSHOT,
            ),
            rec(
                2,
                Action.ADD,
                side=Side.ASK,
                price_fixed=P_100_25,
                quantity=15,
                order_id=3,
                flags=RecordFlag.SNAPSHOT,
            ),
            rec(
                3,
                Action.ADD,
                side=Side.ASK,
                price_fixed=P_100_25 + 250_000_000,
                quantity=25,
                order_id=4,
                flags=RecordFlag.SNAPSHOT,
            ),
        ]
    )
    book.set_status(TradingStatus.TRADING)
    snap0 = book.apply_batch(init)
    # Hand-derived expected snapshot 0:
    assert [(lv.price_fixed, lv.quantity, lv.order_count) for lv in snap0.bids] == [
        (P_100_00, 10, 1),
        (P_99_75, 20, 1),
    ]
    assert [(lv.price_fixed, lv.quantity, lv.order_count) for lv in snap0.asks] == [
        (P_100_25, 15, 1),
        (P_100_25 + 250_000_000, 25, 1),
    ]
    assert snap0.mid2() == P_100_00 + P_100_25  # = 200_250_000_000

    # ------------------------------------------------------------------ Batch 1 (LIVE add)
    # Two new bids join at 100.00 behind order 1:
    #   order 5 qty 4  (priority after 1)
    #   order 6 qty 6  (priority after 5)
    # BID 100.00 now: 1(10), 5(4), 6(6) -> total 20, 3 orders. Priority order [1,5,6].
    add = single_batch(
        [
            rec(4, Action.ADD, side=Side.BID, price_fixed=P_100_00, quantity=4, order_id=5),
            rec(5, Action.ADD, side=Side.BID, price_fixed=P_100_00, quantity=6, order_id=6),
        ]
    )
    snap1 = book.apply_batch(add)
    assert snap1.bids[0].price_fixed == P_100_00
    assert snap1.bids[0].quantity == 20  # 10 + 4 + 6
    assert snap1.bids[0].order_count == 3
    assert [o.key.order_id for o in book.orders_at(Side.BID, P_100_00)] == [1, 5, 6]
    # Quantity ahead of order 6 = 10 (order 1) + 4 (order 5) = 14.
    assert book.visible_quantity_ahead(OrderKey(1, 1, 0, 6)) == 14

    # ------------------------------------------------------------------ Batch 2 (execution)
    # Aggressive SELL sweeps the touch for 10 contracts, consuming order 1 fully.
    # Emission: TRADE(aggressor ASK, 100.00, qty 10), then FILL(resting BID order 1, 10),
    # then CANCEL(order 1, 10). Only the CANCEL mutates.
    # After: BID 100.00 = 5(4), 6(6) -> total 10, 2 orders. Priority [5,6].
    execution = single_batch(
        [
            rec(6, Action.TRADE, side=Side.ASK, price_fixed=P_100_00, quantity=10, order_id=0),
            rec(7, Action.FILL, side=Side.BID, price_fixed=P_100_00, quantity=10, order_id=1),
            rec(8, Action.CANCEL, side=Side.BID, price_fixed=P_100_00, quantity=10, order_id=1),
        ]
    )
    snap2 = book.apply_batch(execution)
    assert snap2.bids[0].price_fixed == P_100_00
    assert snap2.bids[0].quantity == 10  # 4 + 6
    assert snap2.bids[0].order_count == 2
    assert [o.key.order_id for o in book.orders_at(Side.BID, P_100_00)] == [5, 6]
    assert book.get_order(OrderKey(1, 1, 0, 1)) is None

    # ------------------------------------------------------------------ Batch 3 (modify)
    # Order 3 (ASK 100.25, qty 15) modifies to price 100.00? No: a SELL cannot cross; keep
    # it on the ask side but drop its size 15 -> 8 (pure decrease, retains priority), and
    # move order 5 (BID) up in size 4 -> 12 (size increase, loses priority -> back of level).
    # After order 5 size-increase: BID 100.00 priority becomes [6, 5], total 6 + 12 = 18.
    # ASK 100.25 qty becomes 8 (order 3), still 1 order.
    modify = single_batch(
        [
            rec(9, Action.MODIFY, side=Side.ASK, price_fixed=P_100_25, quantity=8, order_id=3),
            rec(10, Action.MODIFY, side=Side.BID, price_fixed=P_100_00, quantity=12, order_id=5),
        ]
    )
    snap3 = book.apply_batch(modify)
    # ASK best level 100.25 reduced to 8.
    assert snap3.asks[0].price_fixed == P_100_25
    assert snap3.asks[0].quantity == 8
    assert snap3.asks[0].order_count == 1
    # BID 100.00: order 5 lost priority, now behind order 6. Total 6 + 12 = 18.
    assert [o.key.order_id for o in book.orders_at(Side.BID, P_100_00)] == [6, 5]
    assert snap3.bids[0].quantity == 18
    # Ahead of order 5 is now order 6's 6.
    assert book.visible_quantity_ahead(OrderKey(1, 1, 0, 5)) == 6

    # ------------------------------------------------------------------ Batch 4 (clear)
    # CLEAR removes all orders. Snapshot is empty on both sides; mid2 undefined.
    clear = single_batch([rec(11, Action.CLEAR, side=Side.NONE)])
    snap4 = book.apply_batch(clear)
    assert snap4.bids == ()
    assert snap4.asks == ()
    assert snap4.mid2() is None
    assert book.best_bid_ask() == (None, None)

    # No anomalies anywhere in this clean trace.
    assert book.anomalies == []
