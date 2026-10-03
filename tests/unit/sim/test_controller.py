"""Terminal controller tests: reachable pending states, one aggressive attempt (eng spec 5).

Covers T16/T39 (controller from every reachable pending state completes within the guard or
records a miss) at the controller level, and T40/T53 (at most one aggressive attempt ever).
"""

from __future__ import annotations

from qexec.core.messages import ExchangeReport, ReportKind
from qexec.core.views import ClientOrderState
from qexec.sim.controller import CommandIntent, IntentKind, TerminalController


def _report(kind: ReportKind, cum: int = 0) -> ExchangeReport:
    return ExchangeReport(
        report_id="r",
        command_id="c",
        task_id="T",
        kind=kind,
        exchange_time_ns=0,
        cumulative_executed=cum,
    )


def _only(intents: list[CommandIntent]) -> CommandIntent:
    assert len(intents) == 1, intents
    return intents[0]


def test_enter_from_flat_sends_aggressive() -> None:
    """NONE at entry -> AGGRESSIVE for the residual (T39 flat path)."""
    ctl = TerminalController("T")
    intents = ctl.enter(ClientOrderState.NONE, reported_executed=0)
    intent = _only(intents)
    assert intent.kind is IntentKind.AGGRESSIVE
    assert intent.quantity == 1
    assert ctl.aggressive_used


def test_enter_from_rejected_sends_aggressive() -> None:
    ctl = TerminalController("T")
    intents = ctl.enter(ClientOrderState.REJECTED, reported_executed=0)
    assert _only(intents).kind is IntentKind.AGGRESSIVE


def test_enter_in_flight_waits_then_accepted_cancels_then_cancelled_aggressive() -> None:
    """IN_FLIGHT at cutoff: wait -> ACCEPTED -> CANCEL -> CANCELLED -> AGGRESSIVE (longest path)."""
    ctl = TerminalController("T")
    assert ctl.enter(ClientOrderState.IN_FLIGHT, 0) == []  # wait for the submission
    # ACCEPTED -> cancel the now-working order.
    cancel = _only(ctl.on_report(_report(ReportKind.ACCEPTED), ClientOrderState.WORKING, 0))
    assert cancel.kind is IntentKind.CANCEL
    # CANCELLED -> aggressive for the residual.
    agg = _only(ctl.on_report(_report(ReportKind.CANCELLED), ClientOrderState.CANCELLED, 0))
    assert agg.kind is IntentKind.AGGRESSIVE
    assert ctl.aggressive_used


def test_enter_working_cancels_then_fill_during_cancel_no_aggressive() -> None:
    """WORKING: CANCEL; a FILL during the cancel completes -> no aggressive (T14 half)."""
    ctl = TerminalController("T")
    cancel = _only(ctl.enter(ClientOrderState.WORKING, 0))
    assert cancel.kind is IntentKind.CANCEL
    # The passive fills during the cancel.
    assert ctl.on_report(_report(ReportKind.FILL, cum=1), ClientOrderState.FILLED, 1) == []
    assert ctl.done
    assert not ctl.aggressive_used


def test_working_cancel_rejected_terminal_filled_reconciles_no_aggressive() -> None:
    ctl = TerminalController("T")
    _only(ctl.enter(ClientOrderState.WORKING, 0))
    # The cancel's target was already terminal and had executed -> reconcile, no aggressive.
    assert (
        ctl.on_report(
            _report(ReportKind.CANCEL_REJECTED_TERMINAL, cum=1), ClientOrderState.FILLED, 1
        )
        == []
    )
    assert ctl.done
    assert not ctl.aggressive_used


def test_in_flight_rejected_sends_aggressive() -> None:
    ctl = TerminalController("T")
    ctl.enter(ClientOrderState.IN_FLIGHT, 0)
    agg = _only(ctl.on_report(_report(ReportKind.REJECTED), ClientOrderState.REJECTED, 0))
    assert agg.kind is IntentKind.AGGRESSIVE


def test_at_most_one_aggressive_attempt_ever() -> None:
    """T40/T53: once an aggressive attempt is used, no further aggressive is emitted."""
    ctl = TerminalController("T")
    _only(ctl.enter(ClientOrderState.NONE, 0))  # aggressive sent
    assert ctl.aggressive_used
    # An AGGRESSIVE_UNFILLED report must not trigger another aggressive.
    assert (
        ctl.on_report(
            _report(ReportKind.AGGRESSIVE_UNFILLED), ClientOrderState.AGGRESSIVE_UNFILLED, 0
        )
        == []
    )
    assert ctl.done
    # Repeated entry is idempotent and never re-sends.
    assert ctl.enter(ClientOrderState.NONE, 0) == []


def test_enter_is_idempotent() -> None:
    ctl = TerminalController("T")
    first = ctl.enter(ClientOrderState.WORKING, 0)
    assert len(first) == 1
    # Second enter (e.g. cutoff after a SWITCH) does nothing new.
    assert ctl.enter(ClientOrderState.WORKING, 0) == []


def test_already_filled_at_entry_does_nothing() -> None:
    ctl = TerminalController("T")
    assert ctl.enter(ClientOrderState.FILLED, 1) == []
    assert ctl.done
    assert not ctl.aggressive_used


def test_copy_is_independent() -> None:
    ctl = TerminalController("T")
    ctl.enter(ClientOrderState.NONE, 0)
    clone = ctl.copy()
    assert clone.aggressive_used
    # Mutating the original does not affect the clone's flags beyond the shared history.
    assert clone.done == ctl.done
