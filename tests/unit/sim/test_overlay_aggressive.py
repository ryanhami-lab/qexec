"""Aggressive command SYNTHETIC_DIRECT_TOP (T17, T40, T14).

AGGRESSIVE fills one contract at the opposite best when TRADING and opposite best qty >= 1,
else AGGRESSIVE_UNFILLED with an explicit reason. It never rests, never retries, and never
over-executes. Every expected number is hand-derived in comments.
"""

from __future__ import annotations

from _overlay_helpers import (
    ASK,
    BID,
    aggressive_cmd,
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


def _book(status: TradingStatus = TradingStatus.TRADING, *, with_ask: bool = True) -> ReferenceBook:
    book = ReferenceBook(instrument())
    book.set_status(status)
    recs = [rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1)]
    if with_ask:
        recs.append(rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=4, order_id=2))
    seed_book(book, recs)
    return book


def test_aggressive_buy_fills_at_opposite_best() -> None:
    """Aggressive BUY while TRADING with ask qty 4 -> FILL 1 at the ASK, AGGRESSIVE."""
    book = _book()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=9)
    reports = overlay.on_command(aggressive_cmd(side=TaskSide.BUY), book)
    assert len(reports) == 1
    r = reports[0]
    assert r.kind is ReportKind.FILL
    assert r.execution is not None
    assert r.execution.price_fixed == ASK
    assert r.execution.liquidity is Liquidity.AGGRESSIVE
    assert r.execution.mechanism is FillMechanism.AGGRESSIVE
    assert r.execution.fee_fixed == 9
    assert overlay.executed_quantity == 1


def test_aggressive_sell_fills_at_best_bid() -> None:
    """Sell-side mirror: aggressive SELL fills 1 at the best BID."""
    book = _book()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=0)
    reports = overlay.on_command(aggressive_cmd(side=TaskSide.SELL), book)
    assert reports[0].execution is not None
    assert reports[0].execution.price_fixed == BID
    assert reports[0].execution.mechanism is FillMechanism.AGGRESSIVE


def test_aggressive_halted_is_unfilled() -> None:
    """T17: aggressive while HALTED -> AGGRESSIVE_UNFILLED reason HALTED; no fill."""
    book = _book(TradingStatus.HALTED)
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=9)
    reports = overlay.on_command(aggressive_cmd(side=TaskSide.BUY), book)
    assert reports[0].kind is ReportKind.AGGRESSIVE_UNFILLED
    assert reports[0].reason == "HALTED"
    assert overlay.executed_quantity == 0


def test_aggressive_no_liquidity_is_unfilled() -> None:
    """T17/T40: aggressive BUY while TRADING but no ask depth -> AGGRESSIVE_UNFILLED
    reason NO_LIQUIDITY; never a synthetic fill."""
    book = _book(with_ask=False)  # bids only, no ask
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=9)
    reports = overlay.on_command(aggressive_cmd(side=TaskSide.BUY), book)
    assert reports[0].kind is ReportKind.AGGRESSIVE_UNFILLED
    assert reports[0].reason == "NO_LIQUIDITY"
    assert overlay.executed_quantity == 0


def test_aggressive_after_complete_is_already_complete() -> None:
    """T14: once executed, a later aggressive command -> AGGRESSIVE_UNFILLED reason
    ALREADY_COMPLETE (prevents over-execution; invariant executed <= 1)."""
    book = _book()
    overlay = ExchangeOverlay("T1", instrument(), fee_per_contract_fixed=9)
    # First a marketable passive fills us (executed -> 1).
    overlay.on_command(passive_cmd(side=TaskSide.BUY, limit_price_fixed=ASK), book)
    assert overlay.executed_quantity == 1
    # A subsequent aggressive attempt must not fill again.
    reports = overlay.on_command(
        aggressive_cmd(side=TaskSide.BUY, command_id="a2", arrival_time_ns=500), book
    )
    assert reports[0].kind is ReportKind.AGGRESSIVE_UNFILLED
    assert reports[0].reason == "ALREADY_COMPLETE"
    assert reports[0].cumulative_executed == 1
    assert overlay.executed_quantity == 1
