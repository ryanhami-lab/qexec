"""Queue-depletion mechanics (T06, T07, T08, T09, T10, T36).

Every expected number is hand-derived in comments. The overlay rests a BUY one-contract
limit at ``BID`` (100.00). "Ahead" orders are those resting at ``BID`` on the bid side at
the moment the passive order is accepted; "behind" orders arrive at ``BID`` afterward.
"""

from __future__ import annotations

from _overlay_helpers import (
    BID,
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
from qexec.sim.evidence import AmbiguityKind
from qexec.sim.overlay import ExchangeOverlay


def _working_overlay_5_ahead() -> tuple[ExchangeOverlay, ReferenceBook]:
    """Build the mandatory fixture: five contracts ahead at BID, our BUY resting behind.

    Ahead orders at BID (bid side): id 1 qty 2, id 2 qty 2, id 3 qty 1  -> total 5 ahead.
    An ASK exists at a higher price so our BID limit is not marketable.
    """
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
            rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=2),
            rec(2, Action.ADD, side=Side.BID, price_fixed=BID, quantity=1, order_id=3),
            # An ask well above BID so the BUY limit at BID is not marketable.
            rec(
                3,
                Action.ADD,
                side=Side.ASK,
                price_fixed=BID + 4 * 250_000_000,
                quantity=5,
                order_id=50,
            ),
        ],
    )
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=7)
    reports = overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    # Accepted and resting; five contracts ahead (2+2+1).
    assert [r.kind for r in reports] == [ReportKind.ACCEPTED]
    assert overlay.is_working
    assert overlay.true_queue_ahead() == 5  # 2 + 2 + 1
    return overlay, book


# --------------------------------------------------------------- T07 (+T06 count-once)


def test_five_ahead_execute_three_leaves_two_ahead_unfilled() -> None:
    """Mandatory fixture: 5 ahead, a 3-contract execution (TRADE+FILL+CANCEL) hits AHEAD
    orders -> 2 remain ahead, virtual order UNFILLED (T07). The CANCEL reduces the book
    exactly once, so the overlay must not also subtract (T06)."""
    overlay, book = _working_overlay_5_ahead()

    # A SELL aggressor sweeps 3 contracts off the bid at BID: it fills ahead order 1 (2)
    # and part of ahead order 2 (1). Engine emits one TRADE (aggressor ASK), then per
    # resting order a FILL followed by the CANCEL that reduces it.
    reports = drive_batch(
        overlay,
        book,
        [
            # Aggressor SELL print for 3 at BID (not through our limit; equal to it).
            rec(10, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=3, order_id=0),
            # Fill + cancel of ahead order 1 (all 2).
            rec(11, Action.FILL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
            rec(12, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
            # Fill + cancel of 1 from ahead order 2 (2 -> 1).
            rec(13, Action.FILL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
            rec(14, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
        ],
    )
    # No virtual fill: all fills were of AHEAD orders (ids 1 and 2 were in the ahead set).
    assert reports == []
    assert overlay.executed_quantity == 0
    assert overlay.is_working
    # Ahead now: order 2 has 1 left, order 3 has 1 -> 2 contracts ahead (not 5-3 by our own
    # subtraction, but by reading the current book quantities: counted once by the book).
    assert overlay.true_queue_ahead() == 2  # 1 (order 2 remainder) + 1 (order 3)


# --------------------------------------------------------------- T08 + QUEUE_DEPLETION


def test_exact_exhaustion_then_behind_fill_depletes_queue() -> None:
    """T08: exactly exhausting the quantity ahead gives NO fill. A later same-price
    execution hitting a BEHIND order (ahead now zero) -> QUEUE_DEPLETION fill at our limit."""
    overlay, book = _working_overlay_5_ahead()  # 5 ahead (ids 1,2,3)

    # First: a behind order joins at BID AFTER we rested (id 20, qty 4). It is behind us.
    drive_batch(
        overlay,
        book,
        [rec(20, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=20)],
    )
    assert overlay.true_queue_ahead() == 5  # behind order does not change quantity ahead

    # Now a SELL sweep takes exactly the 5 ahead (orders 1:2, 2:2, 3:1). Exact exhaustion.
    reports = drive_batch(
        overlay,
        book,
        [
            rec(30, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=5, order_id=0),
            rec(31, Action.FILL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
            rec(32, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
            rec(33, Action.FILL, side=Side.BID, price_fixed=BID, quantity=2, order_id=2),
            rec(34, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=2),
            rec(35, Action.FILL, side=Side.BID, price_fixed=BID, quantity=1, order_id=3),
            rec(36, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=1, order_id=3),
        ],
    )
    # All fills were of AHEAD orders; exact exhaustion is NOT itself a fill (T08).
    assert reports == []
    assert overlay.executed_quantity == 0
    assert overlay.true_queue_ahead() == 0  # queue ahead now empty, order 20 (behind) remains

    # A FURTHER execution now hits the BEHIND order 20 while quantity ahead is 0 ->
    # QUEUE_DEPLETION virtual fill at our limit (BID).
    reports = drive_batch(
        overlay,
        book,
        [
            rec(40, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0),
            rec(41, Action.FILL, side=Side.BID, price_fixed=BID, quantity=1, order_id=20),
            rec(42, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=1, order_id=20),
        ],
    )
    assert len(reports) == 1
    fill = reports[0]
    assert fill.kind is ReportKind.FILL
    assert fill.cumulative_executed == 1
    assert fill.execution is not None
    assert fill.execution.price_fixed == BID  # our own limit, not a worse print
    assert fill.execution.liquidity is Liquidity.PASSIVE
    assert fill.execution.mechanism is FillMechanism.QUEUE_DEPLETION
    assert fill.execution.fee_fixed == 7
    assert overlay.executed_quantity == 1
    assert not overlay.is_working  # terminal after the single fill
    # Ledger: one granted QUEUE_DEPLETION entry displacing behind order 20's fill.
    led = overlay.ledger.fills
    assert len(led) == 1
    assert led[0].mechanism is FillMechanism.QUEUE_DEPLETION
    assert led[0].displaced_allocation is not None
    assert led[0].displaced_allocation.order_id == 20


# --------------------------------------------------------------- T09


def test_cancellation_ahead_reduces_queue_behind_does_not() -> None:
    """T09: a cancellation of an AHEAD order reduces quantity ahead; a cancellation of a
    BEHIND order does not; neither fabricates a fill."""
    overlay, book = _working_overlay_5_ahead()  # ahead ids 1:2, 2:2, 3:1 => 5

    # A behind order joins (id 21, qty 3).
    drive_batch(
        overlay,
        book,
        [rec(50, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=21)],
    )
    assert overlay.true_queue_ahead() == 5

    # Cancel AHEAD order 1 entirely (pure cancel, no trade/fill). Quantity ahead 5 -> 3.
    reports = drive_batch(
        overlay,
        book,
        [rec(51, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1)],
    )
    assert reports == []  # a cancellation is never a fill
    assert overlay.true_queue_ahead() == 3  # 2 (order 2) + 1 (order 3)

    # Cancel BEHIND order 21 entirely. Quantity ahead unchanged at 3.
    reports = drive_batch(
        overlay,
        book,
        [rec(52, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=21)],
    )
    assert reports == []
    assert overlay.true_queue_ahead() == 3
    assert overlay.executed_quantity == 0


# --------------------------------------------------------------- T10


def test_quote_touch_does_not_fill() -> None:
    """T10: the opposite quote touching our limit (a new ASK appears at BID) is not a fill;
    neither is any add/modify quote move."""
    overlay, book = _working_overlay_5_ahead()

    # An ASK appears exactly at our BID limit (a touch). No TRADE/FILL evidence.
    reports = drive_batch(
        overlay,
        book,
        [rec(60, Action.ADD, side=Side.ASK, price_fixed=BID, quantity=4, order_id=60)],
    )
    assert reports == []
    assert overlay.executed_quantity == 0
    assert overlay.is_working


# --------------------------------------------------------------- T36 (ambiguity)


def test_behind_fill_while_ahead_positive_is_ambiguous_no_fill() -> None:
    """T36: a BEHIND order fills while quantity ahead is still > 0. This is inconsistent
    with strict FIFO: under AMBIGUITY_FILL=False no virtual fill is granted, but an
    AMBIGUOUS evidence entry is logged either way."""
    overlay, book = _working_overlay_5_ahead()  # 5 ahead

    # Behind order 22 joins at BID (qty 2).
    drive_batch(
        overlay,
        book,
        [rec(70, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=22)],
    )
    assert overlay.true_queue_ahead() == 5

    # A FILL of BEHIND order 22 arrives while 5 are still ahead (an out-of-FIFO print).
    reports = drive_batch(
        overlay,
        book,
        [
            rec(71, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=2, order_id=0),
            rec(72, Action.FILL, side=Side.BID, price_fixed=BID, quantity=2, order_id=22),
            rec(73, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=22),
        ],
    )
    # No fill (conservative frozen rule).
    assert reports == []
    assert overlay.executed_quantity == 0
    assert overlay.is_working
    # Exactly one AMBIGUOUS entry logged, not granted, recording quantity ahead = 5.
    amb = overlay.ledger.ambiguous
    assert len(amb) == 1
    assert amb[0].ambiguity is AmbiguityKind.BEHIND_FILL_WHILE_AHEAD_POSITIVE
    assert amb[0].granted_fill is False
    assert amb[0].mechanism is FillMechanism.AMBIGUOUS
    assert amb[0].quantity_ahead_before == 5
    assert amb[0].displaced_allocation is not None
    assert amb[0].displaced_allocation.order_id == 22


# --------------------------------------------------------------- priority MODIFY cases


def test_priority_losing_modify_removes_ahead_member() -> None:
    """An AHEAD order that MODIFYs to a different price leaves the ahead set permanently;
    its quantity no longer counts even though no execution occurred."""
    overlay, book = _working_overlay_5_ahead()  # ahead ids 1:2, 2:2, 3:1 => 5

    # Ahead order 2 modifies price UP to a different level (BID + tick): leaves ahead set.
    drive_batch(
        overlay,
        book,
        [
            rec(
                80,
                Action.MODIFY,
                side=Side.BID,
                price_fixed=BID + 250_000_000,
                quantity=2,
                order_id=2,
            )
        ],
    )
    # Quantity ahead now 2 (order 1) + 1 (order 3) = 3; order 2's 2 contracts gone.
    assert overlay.true_queue_ahead() == 3


def test_size_increase_modify_removes_ahead_member() -> None:
    """A size INCREASE on an ahead order loses priority (fresh priority_seq) even though
    the price is unchanged -> leaves the ahead set permanently."""
    overlay, book = _working_overlay_5_ahead()  # ahead ids 1:2, 2:2, 3:1 => 5

    # Ahead order 1 increases size 2 -> 6 at the same price: loses priority -> behind us.
    drive_batch(
        overlay,
        book,
        [rec(81, Action.MODIFY, side=Side.BID, price_fixed=BID, quantity=6, order_id=1)],
    )
    # Quantity ahead now 2 (order 2) + 1 (order 3) = 3; order 1 is now behind us.
    assert overlay.true_queue_ahead() == 3


def test_size_decrease_modify_keeps_ahead_member() -> None:
    """A pure size DECREASE retains priority: the member stays ahead with reduced qty."""
    overlay, book = _working_overlay_5_ahead()  # ahead ids 1:2, 2:2, 3:1 => 5

    # Ahead order 2 decreases size 2 -> 1 (retains priority).
    drive_batch(
        overlay,
        book,
        [rec(82, Action.MODIFY, side=Side.BID, price_fixed=BID, quantity=1, order_id=2)],
    )
    # Quantity ahead now 2 (order 1) + 1 (order 2 reduced) + 1 (order 3) = 4.
    assert overlay.true_queue_ahead() == 4
