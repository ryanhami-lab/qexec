"""Client OMS tests: exposure invariant, idempotency, no resurrection (engine spec section 4).

Covers T15 (duplicate/late reports idempotent; no resurrection) and the fill-during-cancel
reconciliation path at the OMS level (T14's OMS half).
"""

from __future__ import annotations

import pytest

from qexec.core.messages import ExchangeReport, ReportKind
from qexec.core.tasks import Execution, FillMechanism, Liquidity
from qexec.core.views import ClientOrderState
from qexec.sim.oms import ClientOMS


def _report(
    kind: ReportKind,
    *,
    rid: str,
    cid: str,
    t: int,
    cum: int = 0,
    execution: Execution | None = None,
) -> ExchangeReport:
    return ExchangeReport(
        report_id=rid,
        command_id=cid,
        task_id="T",
        kind=kind,
        exchange_time_ns=t,
        cumulative_executed=cum,
        execution=execution,
    )


def _fill_exec(eid: str, price: int = 100_000_000_000, t: int = 500) -> Execution:
    return Execution(
        execution_id=eid,
        task_id="T",
        quantity=1,
        price_fixed=price,
        fee_fixed=0,
        exchange_time_ns=t,
        liquidity=Liquidity.PASSIVE,
        mechanism=FillMechanism.QUEUE_DEPLETION,
    )


def test_passive_lifecycle_accept_then_fill() -> None:
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    assert oms.state is ClientOrderState.IN_FLIGHT
    assert oms.reserved == 1
    oms.apply(_report(ReportKind.ACCEPTED, rid="r0", cid="c0", t=300))
    assert oms.state is ClientOrderState.WORKING
    assert oms.reserved == 1  # ACCEPTED does not release the exposure reservation
    oms.apply(
        _report(ReportKind.FILL, rid="r1", cid="c0", t=500, cum=1, execution=_fill_exec("e0"))
    )
    assert oms.state is ClientOrderState.FILLED
    assert oms.reported_executed == 1
    assert oms.reserved == 0
    assert oms.completion_time_ns == 500


def test_duplicate_report_id_ignored() -> None:
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    assert oms.apply(_report(ReportKind.ACCEPTED, rid="r0", cid="c0", t=300)) is True
    # Exact same report id delivered again -> ignored, no state change.
    assert oms.apply(_report(ReportKind.ACCEPTED, rid="r0", cid="c0", t=300)) is False
    assert oms.state is ClientOrderState.WORKING


def test_late_accepted_cannot_resurrect_terminal() -> None:
    """T15: a late ACCEPTED after a terminal state does not revive the order."""
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    oms.apply(_report(ReportKind.REJECTED, rid="r0", cid="c0", t=300))
    assert oms.state is ClientOrderState.REJECTED
    # A late ACCEPTED for the same command must not move it back to WORKING.
    oms.apply(_report(ReportKind.ACCEPTED, rid="r1", cid="c0", t=400))
    assert oms.state is ClientOrderState.REJECTED
    assert oms.reserved == 0


def test_duplicate_execution_id_counted_once() -> None:
    """T15: a repeated FILL carrying the same execution id is not counted twice."""
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    oms.apply(_report(ReportKind.ACCEPTED, rid="r0", cid="c0", t=300))
    oms.apply(
        _report(ReportKind.FILL, rid="r1", cid="c0", t=500, cum=1, execution=_fill_exec("e0"))
    )
    assert oms.reported_executed == 1
    # Duplicate FILL, same execution id, different report id -> idempotent.
    oms.apply(
        _report(ReportKind.FILL, rid="r2", cid="c0", t=500, cum=1, execution=_fill_exec("e0"))
    )
    assert oms.reported_executed == 1
    assert len(oms.executions) == 1


def test_second_distinct_execution_asserts_overexecution() -> None:
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    oms.apply(
        _report(ReportKind.FILL, rid="r1", cid="c0", t=500, cum=1, execution=_fill_exec("e0"))
    )
    # A brand-new execution id while already executed is a one-contract ledger violation.
    with pytest.raises(AssertionError):
        oms.apply(
            _report(
                ReportKind.FILL, rid="r2", cid="c0", t=600, cum=1, execution=_fill_exec("e1", t=600)
            )
        )


def test_fill_during_cancel_no_double_exposure() -> None:
    """T14 (OMS half): a FILL arriving while a CANCEL is pending completes without stranding
    the passive reservation, and a later CANCEL_REJECTED_TERMINAL reconciles idempotently."""
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    oms.apply(_report(ReportKind.ACCEPTED, rid="r0", cid="c0", t=300))
    # Controller sends a cancel; it is pending.
    oms.reserve("c1", "CANCEL", is_passive=False)
    assert oms.state is ClientOrderState.CANCEL_PENDING
    assert oms.reserved == 1  # the CANCEL does not add exposure
    assert oms.has_pending_command
    # The passive fills during the cancel.
    oms.apply(
        _report(
            ReportKind.FILL, rid="r1", cid="c0", t=450, cum=1, execution=_fill_exec("e0", t=450)
        )
    )
    assert oms.state is ClientOrderState.FILLED
    assert oms.reserved == 0
    assert not oms.has_pending_command
    # The cancel's terminal response arrives late; reconciles, no second execution.
    oms.apply(_report(ReportKind.CANCEL_REJECTED_TERMINAL, rid="r2", cid="c1", t=500, cum=1))
    assert oms.reported_executed == 1
    assert len(oms.executions) == 1


def test_cancelled_releases_passive_exposure() -> None:
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    oms.apply(_report(ReportKind.ACCEPTED, rid="r0", cid="c0", t=300))
    oms.reserve("c1", "CANCEL", is_passive=False)
    # CANCELLED carries the cancel command's id, not the passive's; exposure must still release.
    oms.apply(_report(ReportKind.CANCELLED, rid="r1", cid="c1", t=500))
    assert oms.state is ClientOrderState.CANCELLED
    assert oms.reserved == 0
    assert not oms.has_pending_command


def test_aggressive_from_flat_reserves_then_unfilled() -> None:
    oms = ClientOMS("T")
    oms.reserve("a0", "AGGRESSIVE", is_passive=False)
    assert oms.reserved == 1
    oms.apply(_report(ReportKind.AGGRESSIVE_UNFILLED, rid="r0", cid="a0", t=400))
    assert oms.state is ClientOrderState.AGGRESSIVE_UNFILLED
    assert oms.reserved == 0


def test_copy_is_independent() -> None:
    oms = ClientOMS("T")
    oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    oms.apply(_report(ReportKind.ACCEPTED, rid="r0", cid="c0", t=300))
    clone = oms.copy()
    oms.apply(
        _report(ReportKind.FILL, rid="r1", cid="c0", t=500, cum=1, execution=_fill_exec("e0"))
    )
    assert oms.state is ClientOrderState.FILLED
    assert clone.state is ClientOrderState.WORKING  # unchanged
    assert clone.reported_executed == 0
