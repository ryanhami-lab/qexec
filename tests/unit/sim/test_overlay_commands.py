"""Cancel and reset semantics (T37, cancel race, technical reset).

Covers: cancel of a working order -> CANCELLED with no later fills; a fill-then-cancel
race -> CANCEL_REJECTED_TERMINAL with cumulative 1; cancel of an unknown target ->
CANCEL_REJECTED_TERMINAL reason UNKNOWN_TARGET; CLEAR record and INITIALIZATION snapshot
batch while working -> TECHNICAL_RESET. Every expected number is hand-derived in comments.
"""

from __future__ import annotations

from _overlay_helpers import (
    ASK,
    BID,
    cancel_cmd,
    drive_batch,
    instrument,
    make_batch,
    passive_cmd,
    rec,
    seed_book,
)

from qexec.core.messages import ReportKind
from qexec.core.types import Action, RecordFlag, Side, TaskSide, TradingStatus
from qexec.reference.book import ReferenceBook
from qexec.sim.overlay import ExchangeOverlay


def _resting_buy() -> tuple[ExchangeOverlay, ReferenceBook]:
    """A working BUY at BID resting behind one bid (id 1 qty 3)."""
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
            rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=4, order_id=50),
        ],
    )
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=5)
    reports = overlay.on_command(
        passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID, command_id="c1"), book
    )
    assert reports[0].kind is ReportKind.ACCEPTED
    return overlay, book


def test_cancel_working_order_is_cancelled_and_no_later_fills() -> None:
    """Cancel of a working order -> CANCELLED (terminal). A later behind-order fill that
    would otherwise be QUEUE_DEPLETION produces nothing (the order is terminal)."""
    overlay, book = _resting_buy()
    reports = overlay.on_command(
        cancel_cmd(side=TaskSide.BUY, target_command_id="c1", command_id="x1"), book
    )
    assert len(reports) == 1
    assert reports[0].kind is ReportKind.CANCELLED
    assert reports[0].cumulative_executed == 0
    assert not overlay.is_working

    # A would-be depletion event after cancellation yields no fill.
    later = drive_batch(
        overlay,
        book,
        [
            rec(10, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
            rec(11, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0),
        ],
    )
    assert later == []
    assert overlay.executed_quantity == 0


def test_fill_then_cancel_race_is_cancel_rejected_terminal_with_cumulative_one() -> None:
    """Cancel race: the order fills (QUEUE_DEPLETION) first; a later CANCEL for the same
    target -> CANCEL_REJECTED_TERMINAL with cumulative_executed == 1 (T14/T15 flavor)."""
    overlay, book = _resting_buy()  # ahead = order 1 qty 3
    # Deplete the ahead order (its own fill) then a BEHIND order joins and fills.
    # Simpler: cancel the ahead order, then a behind order fills with ahead 0.
    drive_batch(
        overlay,
        book,
        [
            # Behind order 2 joins at BID.
            rec(10, Action.ADD, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
            # Ahead order 1 fully cancels -> ahead becomes 0.
            rec(11, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
        ],
    )
    assert overlay.true_queue_ahead() == 0
    # Behind order 2 fills -> QUEUE_DEPLETION fill (executed -> 1, terminal).
    fill_reports = drive_batch(
        overlay,
        book,
        [
            rec(12, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0),
            rec(13, Action.FILL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
            rec(14, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
        ],
    )
    assert len(fill_reports) == 1 and fill_reports[0].kind is ReportKind.FILL
    assert overlay.executed_quantity == 1

    # Now a late CANCEL arrives for the (already terminal) passive order.
    reports = overlay.on_command(
        cancel_cmd(side=TaskSide.BUY, target_command_id="c1", command_id="x1"), book
    )
    assert reports[0].kind is ReportKind.CANCEL_REJECTED_TERMINAL
    assert reports[0].cumulative_executed == 1
    assert reports[0].reason == "ALREADY_TERMINAL"


def test_cancel_unknown_target_is_rejected_terminal() -> None:
    """A CANCEL whose target does not match the passive command -> CANCEL_REJECTED_TERMINAL
    reason UNKNOWN_TARGET (surfaced, not silently accepted)."""
    overlay, book = _resting_buy()
    reports = overlay.on_command(
        cancel_cmd(side=TaskSide.BUY, target_command_id="does-not-exist", command_id="x1"),
        book,
    )
    assert reports[0].kind is ReportKind.CANCEL_REJECTED_TERMINAL
    assert reports[0].reason == "UNKNOWN_TARGET"
    # The passive order itself remains working (the stray cancel did not terminate it).
    assert overlay.is_working


# --------------------------------------------------------------- T37 technical reset


def test_clear_record_while_working_is_technical_reset() -> None:
    """T37: a CLEAR record while working -> TECHNICAL_RESET; overlay technically_invalid;
    no further fills."""
    overlay, book = _resting_buy()
    reports = drive_batch(overlay, book, [rec(10, Action.CLEAR, side=Side.NONE)])
    assert len(reports) == 1
    assert reports[0].kind is ReportKind.TECHNICAL_RESET
    assert reports[0].reason == "TECHNICAL_RESET"
    assert overlay.technically_invalid
    assert not overlay.is_working
    assert overlay.executed_quantity == 0


def test_initialization_snapshot_batch_while_working_is_technical_reset() -> None:
    """T37: an INITIALIZATION (all-SNAPSHOT) batch committed while working -> TECHNICAL_RESET.

    The batch here is a snapshot rebuild (SNAPSHOT-flagged adds), which the batch assembler
    classifies as INITIALIZATION; on_batch_committed must emit the reset."""
    overlay, book = _resting_buy()
    # Build a snapshot (INITIALIZATION) batch of SNAPSHOT-flagged adds and drive it fully.
    snap = make_batch(
        [
            rec(
                10,
                Action.ADD,
                side=Side.BID,
                price_fixed=BID,
                quantity=9,
                order_id=100,
                flags=RecordFlag.SNAPSHOT,
            ),
            rec(
                11,
                Action.ADD,
                side=Side.ASK,
                price_fixed=ASK,
                quantity=9,
                order_id=101,
                flags=RecordFlag.SNAPSHOT,
            ),
        ]
    )
    reports = []
    for r in snap.records:
        reports.extend(overlay.on_record(r, book))
        book.apply_record(r)
    book.commit_batch(snap)
    reports.extend(overlay.on_batch_committed(snap, book))

    assert any(rp.kind is ReportKind.TECHNICAL_RESET for rp in reports)
    assert overlay.technically_invalid
    assert not overlay.is_working
    assert overlay.executed_quantity == 0
