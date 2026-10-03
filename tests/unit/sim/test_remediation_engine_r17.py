"""R17 (docs/remediation.md): the OMS reconciles ``cumulative_executed`` from a terminal cancel
response.

Defect: ``ClientOMS.apply`` ignored ``cumulative_executed`` on a ``CANCEL_REJECTED_TERMINAL``
report, so if that report was delivered before the target's own ``FILL`` report, the client still
saw ``reported_executed == 0``. The terminal controller then fired a spurious aggressive attempt
for a task that had already executed.

Required fix (remediation R17): reconcile the reported cumulative execution from the terminal
cancel response (no aggressive intent when cumulative == 1); the cost is still counted exactly
once from the exchange ledger (the separate ``FILL`` report's execution).

These are focused OMS/controller unit tests with hand-built reports (no session needed): the
reconciliation and the one-execution-cost invariant are both pure client-plane logic.
"""

from __future__ import annotations

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)

from qexec.core.messages import ExchangeReport, ReportKind
from qexec.core.tasks import Execution, FillMechanism, Liquidity
from qexec.core.types import TimeNs
from qexec.core.views import ClientOrderState
from qexec.sim.controller import TerminalController
from qexec.sim.oms import ClientOMS


def _accepted(cid: str, t: int) -> ExchangeReport:
    return ExchangeReport(
        "acc", cid, "T", ReportKind.ACCEPTED, exchange_time_ns=t, cumulative_executed=0
    )


def _cancel_rejected_terminal(cid: str, t: int, cumulative: int) -> ExchangeReport:
    return ExchangeReport(
        report_id="crt",
        command_id=cid,
        task_id="T",
        kind=ReportKind.CANCEL_REJECTED_TERMINAL,
        exchange_time_ns=t,
        cumulative_executed=cumulative,
        reason="ALREADY_TERMINAL",
    )


def _fill(cid: str, t: int) -> ExchangeReport:
    ex = Execution(
        "e0", "T", 1, H.BID, 0, TimeNs(t), Liquidity.PASSIVE, FillMechanism.QUEUE_DEPLETION
    )
    return ExchangeReport(
        "f0", cid, "T", ReportKind.FILL, exchange_time_ns=t, cumulative_executed=1, execution=ex
    )


def _working_oms() -> ClientOMS:
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    oms.apply(_accepted("c0", 100))  # IN_FLIGHT -> WORKING
    oms.reserve("c1", "CANCEL", is_passive=False)  # the cutoff cancel
    return oms


def test_cancel_rejected_terminal_reconciles_cumulative_executed() -> None:
    """A CANCEL_REJECTED_TERMINAL with cumulative==1 delivered BEFORE the FILL sets the client's
    reported_executed to 1 (reconciliation), so the controller does not fire an aggressive."""
    oms = _working_oms()
    oms.apply(_cancel_rejected_terminal("c1", 200, cumulative=1))
    assert oms.reported_executed == 1  # reconciled from the terminal cancel response
    assert oms.reserved == 0

    # The controller, on this terminal cancel response, must be done (no aggressive).
    ctrl = TerminalController("T")
    ctrl.enter(ClientOrderState.CANCEL_PENDING, 0)  # entered at cutoff while cancel pending
    intents = ctrl.on_report(
        _cancel_rejected_terminal("c1", 200, cumulative=1), oms.state, oms.reported_executed
    )
    assert intents == []
    assert ctrl.done
    assert not ctrl.aggressive_used


def test_cost_counted_once_when_fill_follows_reconciliation() -> None:
    """After reconciliation the later FILL still records its execution (cost) exactly once and
    does not double-count the executed quantity (one-contract ledger invariant)."""
    oms = _working_oms()
    oms.apply(_cancel_rejected_terminal("c1", 200, cumulative=1))
    assert oms.reported_executed == 1
    assert len(oms.executions) == 0  # the cancel response carries no execution

    # The target's own FILL report arrives afterward: cost (the execution) is now recorded once.
    oms.apply(_fill("c0", 150))
    assert oms.reported_executed == 1  # still one unit (not double-counted)
    assert len(oms.executions) == 1
    assert oms.executions[0].execution_id == "e0"
    assert oms.state is ClientOrderState.FILLED


def test_cancel_rejected_terminal_zero_cumulative_unchanged() -> None:
    """A CANCEL_REJECTED_TERMINAL with cumulative==0 (target cancelled/rejected, not filled) does
    not fabricate an execution; reported_executed stays 0 and the controller may still aggressive.
    """
    oms = _working_oms()
    oms.apply(_cancel_rejected_terminal("c1", 200, cumulative=0))
    assert oms.reported_executed == 0
    assert oms.reserved == 0
    ctrl = TerminalController("T")
    ctrl.enter(ClientOrderState.CANCEL_PENDING, 0)
    intents = ctrl.on_report(
        _cancel_rejected_terminal("c1", 200, cumulative=0), oms.state, oms.reported_executed
    )
    # Residual 1 -> a single aggressive attempt (the target did not execute).
    assert len(intents) == 1
    assert ctrl.aggressive_used
