"""Passive-limit arrival semantics (T12, T17, invalid-limit/quantity, resting).

The overlay places a one-contract passive limit. At arrival it is marketable, resting,
or rejected. Every expected number is hand-derived in comments.
"""

from __future__ import annotations

from _overlay_helpers import (
    ASK,
    BID,
    TICK,
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


def _trading_book() -> ReferenceBook:
    book = ReferenceBook(instrument())
    book.set_status(TradingStatus.TRADING)
    seed_book(
        book,
        [
            rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
            rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=4, order_id=2),
        ],
    )
    return book


# --------------------------------------------------------------- T12 marketable BUY


def test_marketable_buy_fills_at_opposite_best() -> None:
    """T12: a BUY limit at or above the best ask (100.25) is marketable on arrival ->
    FILL 1 at the ASK price, AGGRESSIVE liquidity and mechanism; does not rest."""
    book = _trading_book()  # best ask 100.25 qty 4
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=5)
    # Limit at ASK (100.25): marketable since limit >= best ask.
    reports = overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=ASK), book)
    assert len(reports) == 1
    r = reports[0]
    assert r.kind is ReportKind.FILL
    assert r.execution is not None
    assert r.execution.price_fixed == ASK  # opposite best, not the limit if higher
    assert r.execution.liquidity is Liquidity.AGGRESSIVE
    assert r.execution.mechanism is FillMechanism.AGGRESSIVE
    assert overlay.executed_quantity == 1
    assert not overlay.is_working  # did not rest


def test_marketable_buy_limit_above_ask_fills_at_ask() -> None:
    """A BUY limit strictly above the ask still fills at the opposite best (the ASK),
    never at the worse (higher) limit price."""
    book = _trading_book()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=0)
    reports = overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=ASK + TICK), book)
    assert reports[0].execution is not None
    assert reports[0].execution.price_fixed == ASK  # 100.25, not 100.50


# --------------------------------------------------------------- T12 marketable SELL


def test_marketable_sell_fills_at_opposite_best() -> None:
    """T12 sell-side mirror: a SELL limit at or below the best bid (100.00) is marketable
    -> FILL 1 at the BID price, AGGRESSIVE."""
    book = _trading_book()  # best bid 100.00 qty 3
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=5)
    reports = overlay.on_command(passive_cmd(side=TaskSide.SELL, limit_price_fixed=BID), book)
    assert len(reports) == 1
    r = reports[0]
    assert r.kind is ReportKind.FILL
    assert r.execution is not None
    assert r.execution.price_fixed == BID  # opposite best (bid)
    assert r.execution.liquidity is Liquidity.AGGRESSIVE
    assert r.execution.mechanism is FillMechanism.AGGRESSIVE
    assert overlay.executed_quantity == 1


# --------------------------------------------------------------- resting (not marketable)


def test_non_marketable_buy_rests_behind_queue() -> None:
    """A BUY limit below the ask rests at its limit; quantity ahead = resting bid qty."""
    book = _trading_book()  # bid 100.00 qty 3 (one order id 1), ask 100.25
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=5)
    # Limit at BID (100.00): not marketable (limit 100.00 < best ask 100.25).
    reports = overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    assert [r.kind for r in reports] == [ReportKind.ACCEPTED]
    assert reports[0].cumulative_executed == 0
    assert overlay.is_working
    assert overlay.true_queue_ahead() == 3  # the single resting bid of qty 3


# --------------------------------------------------------------- T17 halted / status


def test_passive_rejected_when_not_trading() -> None:
    """T17: a passive limit arriving while the book is HALTED is REJECTED (reason STATUS);
    no synthetic fill, no resting order."""
    book = _trading_book()
    book.set_status(TradingStatus.HALTED)
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=5)
    reports = overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID), book)
    assert len(reports) == 1
    assert reports[0].kind is ReportKind.REJECTED
    assert reports[0].reason == "STATUS"
    assert reports[0].cumulative_executed == 0
    assert not overlay.is_working


# --------------------------------------------------------------- invalid limit / quantity


def test_passive_rejected_when_not_tick_aligned() -> None:
    book = _trading_book()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=5)
    # BID + 1 is not a multiple of the tick size (250_000_000).
    reports = overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID + 1), book)
    assert reports[0].kind is ReportKind.REJECTED
    assert reports[0].reason == "INVALID_LIMIT"
    assert not overlay.is_working


def test_passive_rejected_when_quantity_not_one() -> None:
    book = _trading_book()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=5)
    reports = overlay.on_command(
        passive_cmd(side=TaskSide.BUY, limit_price_fixed=BID, quantity=2), book
    )
    assert reports[0].kind is ReportKind.REJECTED
    assert reports[0].reason == "INVALID_QUANTITY"


def test_passive_rejected_when_limit_missing() -> None:
    book = _trading_book()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=5)
    reports = overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=None), book)
    assert reports[0].kind is ReportKind.REJECTED
    assert reports[0].reason == "INVALID_LIMIT"
