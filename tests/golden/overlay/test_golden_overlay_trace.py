"""Golden multi-event overlay trace (hand-derived).

A single working BUY order at BID (100.00) is driven through a scripted multi-batch
session. Every batch's effect on quantity-ahead and the full report sequence is computed
by hand in the comments below and asserted exactly. This is the overlay analogue of the
WP1-BOOK golden trace: it pins the end-to-end behaviour, not just unit branches.

Scenario (BUY, limit 100.00, fee 2 per contract)
=================================================
Priming (committed, no overlay):
  bid 100.00: id 1 qty 4, id 2 qty 2   (ahead candidates)
  ask 100.25: id 50 qty 5              (keeps our 100.00 limit non-marketable)

Command: PASSIVE_LIMIT BUY @ 100.00
  -> ACCEPTED, ahead = {1:4, 2:2}, quantity ahead = 6.   report TASK:0 ACCEPTED

Batch A (pure cancellations, no execution):
  CANCEL id 1 by 2 (4 -> 2, retains priority)
  -> quantity ahead = 2 (id1) + 2 (id2) = 4. No report.

Batch B (ahead order 2 price-modifies away -> leaves ahead set):
  MODIFY id 2 to 99.75 (different level) -> leaves ahead set permanently.
  -> quantity ahead = 2 (only id 1). No report.

Batch C (a behind order joins, then ahead order 1 cancels fully):
  ADD id 3 at 100.00 qty 3 (behind us)
  CANCEL id 1 by 2 (fully removed) -> quantity ahead = 0.
  No report (cancellation is never a fill).

Batch D (opposing SELL execution hits the BEHIND order 3 with zero ahead):
  TRADE aggressor ASK @ 100.00 qty 1
  FILL id 3 @ 100.00 qty 1   (behind us, ahead == 0)  -> QUEUE_DEPLETION fill at 100.00
  CANCEL id 3 by 1
  -> report TASK:1 FILL, cumulative 1, price 100.00, PASSIVE/QUEUE_DEPLETION, fee 2.
  Order is terminal; executed_quantity == 1.
"""

from __future__ import annotations

from _overlay_helpers import (
    ASK,
    BID,
    BID2,
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


def test_golden_overlay_trace_buy_queue_depletion() -> None:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=1),
            rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=2),
            rec(2, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=50),
        ],
    )
    overlay = ExchangeOverlay("TASK", instrument(), fee_per_contract_fixed=2)

    # Command: ACCEPTED, 6 ahead.
    accepted = overlay.on_command(
        passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID, task_id="TASK", command_id="c1"),
        book,
    )
    assert [r.report_id for r in accepted] == ["TASK:0"]
    assert accepted[0].kind is ReportKind.ACCEPTED
    assert overlay.true_queue_ahead() == 6  # 4 + 2

    # Batch A: partial cancel of ahead order 1 (4 -> 2). Quantity ahead 6 -> 4.
    rA = drive_batch(
        overlay,
        book,
        [rec(10, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1)],
    )
    assert rA == []
    assert overlay.true_queue_ahead() == 4  # 2 (id1) + 2 (id2)

    # Batch B: ahead order 2 modifies to 99.75 (leaves ahead set). Quantity ahead 4 -> 2.
    rB = drive_batch(
        overlay,
        book,
        [rec(11, Action.MODIFY, side=Side.BID, price_fixed=BID2, quantity=2, order_id=2)],
    )
    assert rB == []
    assert overlay.true_queue_ahead() == 2  # only id 1 (qty 2) remains ahead

    # Batch C: behind order 3 joins; ahead order 1 fully cancels. Quantity ahead 2 -> 0.
    rC = drive_batch(
        overlay,
        book,
        [
            rec(12, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=3),
            rec(13, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
        ],
    )
    assert rC == []
    assert overlay.true_queue_ahead() == 0
    assert overlay.is_working

    # Batch D: SELL execution hits behind order 3 with zero ahead -> QUEUE_DEPLETION fill.
    rD = drive_batch(
        overlay,
        book,
        [
            rec(14, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0),
            rec(15, Action.FILL, side=Side.BID, price_fixed=BID, quantity=1, order_id=3),
            rec(16, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=1, order_id=3),
        ],
    )
    assert [r.report_id for r in rD] == ["TASK:1"]
    fill = rD[0]
    assert fill.kind is ReportKind.FILL
    assert fill.cumulative_executed == 1
    assert fill.execution is not None
    assert fill.execution.execution_id == "TASK:exec:0"
    assert fill.execution.price_fixed == BID
    assert fill.execution.fee_fixed == 2
    assert fill.execution.liquidity is Liquidity.PASSIVE
    assert fill.execution.mechanism is FillMechanism.QUEUE_DEPLETION
    assert overlay.executed_quantity == 1
    assert not overlay.is_working
    assert overlay.true_queue_ahead() is None  # terminal

    # Evidence ledger: exactly one granted QUEUE_DEPLETION entry displacing order 3.
    assert len(overlay.ledger.entries) == 1
    ev = overlay.ledger.entries[0]
    assert ev.granted_fill is True
    assert ev.mechanism is FillMechanism.QUEUE_DEPLETION
    assert ev.displaced_allocation is not None and ev.displaced_allocation.order_id == 3
