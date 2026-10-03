"""Trade-through fills (T55).

A resting virtual order can fill when identifiable opposing aggressive execution prints
*through* its limit, even after the historical level it joined has disappeared (the tape
never shows the hypothetical order). A quote move alone, with no execution evidence, gives
no fill. Every expected number is hand-derived in comments.
"""

from __future__ import annotations

from _overlay_helpers import (
    ASK,
    BID,
    BID2,
    BID3,
    TICK,
    drive_batch,
    instrument,
    passive_cmd,
    rec,
    seed_book,
)

from qexec.core.messages import ReportKind
from qexec.core.tasks import FillMechanism, Liquidity
from qexec.core.types import Action, Side, TaskSide, TradingStatus
from qexec.reference.book import ReferenceBook
from qexec.sim.overlay import ExchangeOverlay


def test_trade_through_fills_at_own_limit_after_level_disappears() -> None:
    """T55: all historical quantity ahead of a virtual BID at BID cancels, the best bid
    falls below the limit, then a supported direct SELL execution prints BELOW the limit.
    The virtual buy fills at ITS OWN limit (BID), TRADE_THROUGH."""
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    # Ahead at BID: one order id 1 qty 3. Lower support at BID2 (id 2). Ask up high.
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
            rec(1, Action.ADD, side=Side.BID, price_fixed=BID2, quantity=5, order_id=2),
            rec(2, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=4, order_id=50),
        ],
    )
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=3)
    overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    assert overlay.true_queue_ahead() == 3  # order 1

    # All quantity ahead cancels (order 1 fully). Best bid falls to BID2 (below our limit).
    drive_batch(
        overlay,
        book,
        [rec(10, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1)],
    )
    assert overlay.true_queue_ahead() == 0  # queue empty, still working, best bid now BID2
    assert overlay.is_working

    # A supported SELL (aggressor ASK) prints at BID2 which is BELOW our limit BID ->
    # opposing demand passed our priority position -> TRADE_THROUGH fill at OUR limit (BID).
    reports = drive_batch(
        overlay,
        book,
        [
            rec(20, Action.TRADE, side=Side.ASK, price_fixed=BID2, quantity=2, order_id=0),
            rec(21, Action.FILL, side=Side.BID, price_fixed=BID2, quantity=2, order_id=2),
            rec(22, Action.CANCEL, side=Side.BID, price_fixed=BID2, quantity=2, order_id=2),
        ],
    )
    assert len(reports) == 1
    r = reports[0]
    assert r.kind is ReportKind.FILL
    assert r.execution is not None
    assert r.execution.price_fixed == BID  # OUR limit, not the worse print at BID2
    assert r.execution.liquidity is Liquidity.PASSIVE
    assert r.execution.mechanism is FillMechanism.TRADE_THROUGH
    assert overlay.executed_quantity == 1
    # Ledger links the print and the actual resting allocation supporting its volume.
    led = overlay.ledger.fills
    assert len(led) == 1
    assert led[0].mechanism is FillMechanism.TRADE_THROUGH
    assert led[0].trade_price_fixed == BID2
    assert led[0].displaced_allocation is not None
    assert led[0].displaced_allocation.order_id == 2
    assert led[0].source_group_ordinal == 20
    assert led[0].eligible_volume == 2


def test_quote_move_alone_does_not_fill() -> None:
    """T55 companion: after the level disappears, a mere quote move (new bids/asks, no
    opposing execution through the limit) gives NO fill."""
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
            rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=4, order_id=50),
        ],
    )
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=3)
    overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)

    # Order 1 cancels; a new, lower bid appears and the ask drops a tick. No TRADE through.
    reports = drive_batch(
        overlay,
        book,
        [
            rec(10, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
            rec(11, Action.ADD, side=Side.BID, price_fixed=BID3, quantity=2, order_id=3),
            rec(12, Action.ADD, side=Side.ASK, price_fixed=ASK - TICK, quantity=1, order_id=4),
        ],
    )
    assert reports == []
    assert overlay.executed_quantity == 0
    assert overlay.is_working


def test_trade_at_limit_not_through_does_not_trade_through() -> None:
    """A SELL print exactly AT our BID limit (not strictly below) is not a trade-through;
    only its resting-fill path can produce a queue-depletion fill, not trade-through."""
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
            rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=4, order_id=50),
        ],
    )
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=3)
    overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    # TRADE at BID (equal, not strictly through) with no FILL evidence of a behind order.
    reports = drive_batch(
        overlay,
        book,
        [rec(10, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0)],
    )
    assert reports == []  # not a trade-through (price == limit, not < limit)
    assert overlay.executed_quantity == 0


def test_trade_through_sell_side_mirror() -> None:
    """Sell-side mirror of T55: a virtual SELL at ASK; after its level clears, a BUY
    aggressor (aggressor BID) prints ABOVE ASK -> TRADE_THROUGH fill at our ASK limit."""
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    # Ahead at ASK: id 1 qty 3. Higher support at ASK+tick (id 2). Bid down low.
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=3, order_id=1),
            rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK + TICK, quantity=5, order_id=2),
            rec(2, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=50),
        ],
    )
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=3)
    overlay.on_command(passive_cmd(side=TaskSide.SELL, limit_price_fixed=ASK), book)
    assert overlay.true_queue_ahead() == 3

    # Ahead order 1 cancels; best ask rises to ASK+tick (above our limit).
    drive_batch(
        overlay,
        book,
        [rec(10, Action.CANCEL, side=Side.ASK, price_fixed=ASK, quantity=3, order_id=1)],
    )
    assert overlay.true_queue_ahead() == 0

    # BUY aggressor (aggressor BID) prints at ASK+tick, ABOVE our ASK limit -> TRADE_THROUGH.
    reports = drive_batch(
        overlay,
        book,
        [
            rec(20, Action.TRADE, side=Side.BID, price_fixed=ASK + TICK, quantity=2, order_id=0),
            rec(21, Action.FILL, side=Side.ASK, price_fixed=ASK + TICK, quantity=2, order_id=2),
            rec(22, Action.CANCEL, side=Side.ASK, price_fixed=ASK + TICK, quantity=2, order_id=2),
        ],
    )
    assert len(reports) == 1
    assert reports[0].execution is not None
    assert reports[0].execution.price_fixed == ASK  # our own limit
    assert reports[0].execution.mechanism is FillMechanism.TRADE_THROUGH
    assert overlay.executed_quantity == 1
