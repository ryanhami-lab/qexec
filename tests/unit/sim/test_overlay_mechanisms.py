"""Fill-mechanism classification (T58), determinism, copy independence, and the T06
count-once invariant on the sell side.

T58 requires that queue-depletion, trade-through, aggressive, and ambiguous fixtures each
receive the correct ``fill_mechanism``. Every expected number is hand-derived in comments.
"""

from __future__ import annotations

from _overlay_helpers import (
    ASK,
    BID,
    BID2,
    TICK,
    aggressive_cmd,
    drive_batch,
    instrument,
    passive_cmd,
    rec,
    seed_book,
)

from qexec.core.tasks import FillMechanism
from qexec.core.types import Action, Side, TaskSide, TradingStatus
from qexec.reference.book import ReferenceBook
from qexec.sim.evidence import AmbiguityKind
from qexec.sim.overlay import ExchangeOverlay


def _trading_book_with_bids() -> ReferenceBook:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
            rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=4, order_id=50),
        ],
    )
    return book


# --------------------------------------------------------------- T58: four mechanisms


def test_mechanism_queue_depletion() -> None:
    """QUEUE_DEPLETION: ahead cancels, behind order fills with zero quantity ahead."""
    book = _trading_book_with_bids()  # ahead order 1 qty 2
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=1)
    overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    # Behind order 2 joins; ahead order 1 cancels -> ahead 0.
    drive_batch(
        overlay,
        book,
        [
            rec(10, Action.ADD, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
            rec(11, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
        ],
    )
    reports = drive_batch(
        overlay,
        book,
        [
            rec(12, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0),
            rec(13, Action.FILL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
            rec(14, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
        ],
    )
    assert reports[0].execution is not None
    assert reports[0].execution.mechanism is FillMechanism.QUEUE_DEPLETION


def test_mechanism_trade_through() -> None:
    """TRADE_THROUGH: level clears, opposing sell prints below the limit."""
    book = _trading_book_with_bids()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=1)
    overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    # Ahead cancels; add lower support; sell prints at BID2 (< limit).
    reports = drive_batch(
        overlay,
        book,
        [
            rec(10, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
            rec(11, Action.ADD, side=Side.BID, price_fixed=BID2, quantity=3, order_id=3),
            rec(12, Action.TRADE, side=Side.ASK, price_fixed=BID2, quantity=1, order_id=0),
            rec(13, Action.FILL, side=Side.BID, price_fixed=BID2, quantity=1, order_id=3),
            rec(14, Action.CANCEL, side=Side.BID, price_fixed=BID2, quantity=1, order_id=3),
        ],
    )
    assert reports[0].execution is not None
    assert reports[0].execution.mechanism is FillMechanism.TRADE_THROUGH


def test_mechanism_aggressive() -> None:
    """AGGRESSIVE: marketable passive on arrival (and the aggressive command path)."""
    book = _trading_book_with_bids()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=1)
    reports = overlay.on_command(aggressive_cmd(side=TaskSide.BUY), book)
    assert reports[0].execution is not None
    assert reports[0].execution.mechanism is FillMechanism.AGGRESSIVE


def test_mechanism_ambiguous_logged_not_filled() -> None:
    """AMBIGUOUS: behind-order fill while quantity ahead positive. No fill, but the ledger
    entry carries mechanism AMBIGUOUS (T58 ambiguous fixture)."""
    book = _trading_book_with_bids()  # ahead order 1 qty 2
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=1)
    overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    drive_batch(
        overlay,
        book,
        [rec(10, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=2)],
    )
    # Behind order 2 fills while order 1 (qty 2) is still ahead.
    reports = drive_batch(
        overlay,
        book,
        [
            rec(11, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=2, order_id=0),
            rec(12, Action.FILL, side=Side.BID, price_fixed=BID, quantity=2, order_id=2),
            rec(13, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=2),
        ],
    )
    assert reports == []
    assert overlay.ledger.ambiguous[0].mechanism is FillMechanism.AMBIGUOUS
    assert overlay.ledger.ambiguous[0].ambiguity is AmbiguityKind.BEHIND_FILL_WHILE_AHEAD_POSITIVE


# --------------------------------------------------------------- deterministic report ids


def test_report_ids_unique_and_deterministic() -> None:
    """Report ids are f'{task_id}:{n}' with a monotone counter; unique within a run and
    identical across two identical runs (determinism)."""

    def run() -> list[str]:
        book = _trading_book_with_bids()
        ov = ExchangeOverlay("TX", instrument(), fee_per_contract_fixed=1)
        ids = [
            r.report_id
            for r in ov.on_command(
                passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID, task_id="TX"), book
            )
        ]
        # Behind order joins, ahead cancels, behind fills -> a second report (FILL).
        ids += [
            r.report_id
            for r in drive_batch(
                ov,
                book,
                [
                    rec(10, Action.ADD, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
                    rec(11, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
                ],
            )
        ]
        ids += [
            r.report_id
            for r in drive_batch(
                ov,
                book,
                [
                    rec(12, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0),
                    rec(13, Action.FILL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
                    rec(14, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
                ],
            )
        ]
        return ids

    ids1 = run()
    ids2 = run()
    assert ids1 == ids2  # deterministic
    assert len(ids1) == len(set(ids1))  # unique
    assert ids1 == ["TX:0", "TX:1"]  # ACCEPTED then FILL


# --------------------------------------------------------------- copy() independence


def test_copy_is_independent() -> None:
    """copy() yields an independent overlay: driving the clone does not affect the original
    and vice versa (used by World.clone for HOLD/SWITCH branches, T41)."""
    book = _trading_book_with_bids()  # ahead order 1 qty 2
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=1)
    overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    assert overlay.true_queue_ahead() == 2

    clone = overlay.copy()
    clone_book = book.copy()

    # Fill the CLONE only (cancel ahead, behind joins and fills).
    drive_batch(
        clone,
        clone_book,
        [
            rec(10, Action.ADD, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
            rec(11, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=2, order_id=1),
        ],
    )
    drive_batch(
        clone,
        clone_book,
        [
            rec(12, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=1, order_id=0),
            rec(13, Action.FILL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
            rec(14, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=1, order_id=2),
        ],
    )
    assert clone.executed_quantity == 1
    assert not clone.is_working
    # Original is untouched: still working, still 2 ahead, nothing executed.
    assert overlay.executed_quantity == 0
    assert overlay.is_working
    assert overlay.true_queue_ahead() == 2
    assert overlay.ledger.fills == []


# --------------------------------------------------------------- T06 count-once (sell)


def test_sell_side_queue_depletion_counts_reduction_once() -> None:
    """Sell-side mirror + T06: a virtual SELL at ASK rests behind ask id 1 (qty 5). A
    BUY aggressor sweeps 3 (TRADE+FILL+CANCEL of the ahead order). The book reduces the
    ahead order by 3 exactly once; quantity ahead goes 5 -> 2 (not double counted)."""
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=5, order_id=1),
            rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=50),
        ],
    )
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=1)
    overlay.on_command(passive_cmd(side=TaskSide.SELL, limit_price_fixed=ASK), book)
    assert overlay.true_queue_ahead() == 5  # ahead ask order 1

    reports = drive_batch(
        overlay,
        book,
        [
            rec(10, Action.TRADE, side=Side.BID, price_fixed=ASK, quantity=3, order_id=0),
            rec(11, Action.FILL, side=Side.ASK, price_fixed=ASK, quantity=3, order_id=1),
            rec(12, Action.CANCEL, side=Side.ASK, price_fixed=ASK, quantity=3, order_id=1),
        ],
    )
    assert reports == []  # ahead-order fill, no virtual fill (T07)
    # 5 - 3 = 2 ahead, counted once by the book's CANCEL (T06), not 5-3-3.
    assert overlay.true_queue_ahead() == 2
    _ = (BID2, TICK)  # imported for parity with buy-side fixtures
