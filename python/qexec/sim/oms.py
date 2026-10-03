"""Client-side order management (architecture section 7.2, engine spec section 4).

The :class:`ClientOMS` is the client plane's view of a single task's exposure. It consumes
:class:`~qexec.core.messages.ExchangeReport` objects *as they are delivered* (after the response
delay) and maintains:

* the client order state (:class:`~qexec.core.views.ClientOrderState`),
* the reported cumulative executed quantity (0 or 1 for a one-contract task),
* an exposure reservation held from command creation until a definitive report reconciles it,
* idempotent report ids (a duplicate ``report_id`` is ignored), and
* execution ids (each :class:`~qexec.core.tasks.Execution` counted at most once).

Invariants (asserted on every applied report):

* ``reported_executed + reserved <= 1`` -- one potentially live child at a time.
* a terminal order cannot be resurrected: once the client state is terminal
  (``FILLED`` / ``REJECTED`` / ``CANCELLED`` / ``AGGRESSIVE_UNFILLED`` / ``TECHNICAL``) a late
  ``ACCEPTED`` (or any non-fill report) cannot move it back to a live state. A late ``FILL`` of
  an *already executed* order is idempotent (same execution id ignored); a FILL carrying a new
  execution id while already executed violates the one-contract ledger and asserts.

A **reservation** is placed when a command is created (``reserve``) and released only by a
definitive report for that command: ``FILL`` (which also records the execution),
``REJECTED``, ``CANCELLED``, ``CANCEL_REJECTED_TERMINAL``, ``AGGRESSIVE_UNFILLED``, or
``TECHNICAL_RESET``. ``ACCEPTED`` does not release the reservation (the order is now working and
still reserved); it transitions ``IN_FLIGHT`` -> ``WORKING``.
"""

from __future__ import annotations

from dataclasses import dataclass

from qexec.core.messages import ExchangeReport, ReportKind
from qexec.core.tasks import Execution
from qexec.core.views import ClientOrderState

# Reports that release the per-command exposure reservation (engine spec section 4).
_RESERVATION_RELEASING: frozenset[ReportKind] = frozenset(
    {
        ReportKind.FILL,
        ReportKind.REJECTED,
        ReportKind.CANCELLED,
        ReportKind.CANCEL_REJECTED_TERMINAL,
        ReportKind.AGGRESSIVE_UNFILLED,
        ReportKind.TECHNICAL_RESET,
    }
)

_TERMINAL_CLIENT_STATES: frozenset[ClientOrderState] = frozenset(
    {
        ClientOrderState.FILLED,
        ClientOrderState.REJECTED,
        ClientOrderState.CANCELLED,
        ClientOrderState.AGGRESSIVE_UNFILLED,
        ClientOrderState.TECHNICAL,
    }
)


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """A command the client created, tracking whether it is unresolved and holds exposure.

    ``pending`` is True while no definitive report has resolved the command (used for the
    checkpoint eligibility ``INELIGIBLE_PENDING_COMMAND`` and for drain completion). ``reserves``
    is True only for commands that can create exposure (PASSIVE_LIMIT / AGGRESSIVE); a CANCEL
    never adds exposure, so it is pending but does not consume the ``reserved`` budget
    (architecture 7.2: the reservation bounds *potentially live child orders*).
    """

    command_id: str
    kind: str
    pending: bool
    reserves: bool


class ClientOMS:
    """Per-task client order manager. Deterministic; driven by delivered reports only."""

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id
        self._state: ClientOrderState = ClientOrderState.NONE
        self._reported_executed: int = 0
        self._reserved: int = 0
        # R17: True once the executed quantity was reconciled from a terminal cancel response's
        # cumulative_executed without a delivered Execution. A later FILL then records the cost
        # (its Execution) exactly once without re-incrementing the reconciled count.
        self._reconciled_without_execution: bool = False
        self._commands: dict[str, CommandRecord] = {}
        self._seen_report_ids: set[str] = set()
        self._seen_execution_ids: set[str] = set()
        self._passive_command_id: str | None = None
        self._executions: list[Execution] = []
        self._completion_time_ns: int | None = None

    # -- read-only state -------------------------------------------------------------

    @property
    def state(self) -> ClientOrderState:
        return self._state

    @property
    def reported_executed(self) -> int:
        return self._reported_executed

    @property
    def reserved(self) -> int:
        return self._reserved

    @property
    def passive_command_id(self) -> str | None:
        return self._passive_command_id

    @property
    def has_pending_command(self) -> bool:
        """True iff any created command is still unresolved (no definitive report)."""
        return any(c.pending for c in self._commands.values())

    @property
    def executions(self) -> list[Execution]:
        return list(self._executions)

    @property
    def completion_time_ns(self) -> int | None:
        """Exchange time of the executing fill as the client learned it (its exchange time)."""
        return self._completion_time_ns

    @property
    def is_terminal(self) -> bool:
        return self._state in _TERMINAL_CLIENT_STATES

    # -- command creation ------------------------------------------------------------

    def reserve(self, command_id: str, kind: str, *, is_passive: bool) -> None:
        """Record a newly created command.

        PASSIVE_LIMIT / AGGRESSIVE commands reserve one unit of exposure (held from decision
        time through the scheduled computation period, architecture 7.2); a CANCEL is tracked as
        pending but reserves no exposure. Enforces ``reported_executed + reserved <= 1``.
        """
        if command_id in self._commands:
            raise ValueError(f"duplicate command_id {command_id!r}")
        reserves = kind in ("PASSIVE_LIMIT", "AGGRESSIVE")
        if reserves:
            self._reserved += 1
        self._commands[command_id] = CommandRecord(
            command_id, kind, pending=True, reserves=reserves
        )
        if is_passive:
            self._passive_command_id = command_id
        if kind == "CANCEL":
            if self._state is ClientOrderState.WORKING:
                self._state = ClientOrderState.CANCEL_PENDING
        else:
            self._state = ClientOrderState.IN_FLIGHT
        assert self._reported_executed + self._reserved <= 1, (
            "exposure invariant violated on reserve"
        )

    def _release(self, command_id: str) -> None:
        rec = self._commands.get(command_id)
        if rec is not None and rec.pending:
            self._commands[command_id] = CommandRecord(
                rec.command_id, rec.kind, pending=False, reserves=rec.reserves
            )
            if rec.reserves:
                self._reserved -= 1
        assert self._reserved >= 0, "negative reservation"

    def _mark_accepted(self, command_id: str) -> None:
        """Mark a command as no longer *pending* while retaining its exposure reservation.

        An ACCEPTED passive order has resolved into a resting (working) order: it is no longer a
        pending command for checkpoint-eligibility purposes, but it still holds its exposure
        reservation until a definitive terminal report (FILL / CANCELLED / ...). This lets a
        working, unfilled order with no pending command be checkpoint-eligible (product 6.1).
        """
        rec = self._commands.get(command_id)
        if rec is not None and rec.pending:
            self._commands[command_id] = CommandRecord(
                rec.command_id, rec.kind, pending=False, reserves=rec.reserves
            )

    def _release_all_exposure(self) -> None:
        """Resolve the single live order: clear every pending flag and all exposure.

        Called when any report makes the task's exposure terminal (FILL, CANCELLED, REJECTED,
        AGGRESSIVE_UNFILLED, TECHNICAL_RESET, or a CANCEL_REJECTED_TERMINAL reconciliation). The
        CANCELLED report that terminates a working passive carries the cancel's command_id, not
        the passive's, and an ACCEPTED has already cleared the passive command's *pending* flag,
        so releasing by command_id (or by pending flag) alone would strand the passive's exposure.
        This clears exposure for every reserving command and marks every command resolved so
        ``reserved`` returns to 0 and ``has_pending_command`` becomes False.
        """
        for cid, rec in list(self._commands.items()):
            if rec.pending or rec.reserves:
                self._commands[cid] = CommandRecord(
                    rec.command_id, rec.kind, pending=False, reserves=False
                )
        self._reserved = 0

    # -- report application ----------------------------------------------------------

    def apply(self, report: ExchangeReport) -> bool:
        """Apply a delivered ``report``; return ``True`` if it changed state (not a duplicate).

        Idempotent on ``report_id``: a duplicate report id is ignored and returns ``False``.
        A terminal client state cannot be resurrected by a late non-terminal report.
        """
        if report.report_id in self._seen_report_ids:
            return False
        self._seen_report_ids.add(report.report_id)

        kind = report.kind

        # A late ACCEPTED (or any non-fill) cannot resurrect a terminal order (T15).
        if self.is_terminal and kind is not ReportKind.FILL:
            # Still release any reservation the command might (defensively) hold.
            self._release(report.command_id)
            self._assert_invariant()
            return True

        if kind is ReportKind.ACCEPTED:
            # Working now; the submission command is resolved (no longer a *pending* command) but
            # its exposure reservation is retained (the order is live). Do not override terminal.
            if self._state in (ClientOrderState.IN_FLIGHT, ClientOrderState.NONE):
                self._state = ClientOrderState.WORKING
            self._mark_accepted(report.command_id)
        elif kind is ReportKind.FILL:
            self._apply_fill(report)
        elif kind is ReportKind.REJECTED:
            self._release_all_exposure()
            self._state = ClientOrderState.REJECTED
        elif kind is ReportKind.CANCELLED:
            self._release_all_exposure()
            self._state = ClientOrderState.CANCELLED
        elif kind is ReportKind.CANCEL_REJECTED_TERMINAL:
            # The cancel's target was already terminal; reconcile. Release the cancel's own
            # pending flag and any stranded exposure (the target order is terminal).
            self._release_all_exposure()
            # R17: reconcile the reported cumulative execution from the terminal cancel response.
            # If the target had already executed (cumulative_executed >= 1) but the client has not
            # yet received the target's own FILL report, mark the task executed now so the
            # terminal controller does not fire a spurious aggressive attempt. The *cost* is still
            # counted exactly once from the exchange ledger: a later FILL report records its
            # Execution (see _apply_fill), which does not re-increment the already-reconciled
            # count. A separate FILL that is never delivered leaves no Execution, so a reconciled
            # fill without a delivered execution has reported_executed == 1 and no cost row (the
            # exchange ledger -- the recorded execution rows the engine reads for outcomes -- is
            # the single source of cost, so this cannot double-count). cumulative == 0 means the
            # target was cancelled/rejected without executing: nothing to reconcile.
            if report.cumulative_executed >= 1 and self._reported_executed == 0:
                self._reported_executed = 1
                self._reconciled_without_execution = True
                self._state = ClientOrderState.FILLED
        elif kind is ReportKind.AGGRESSIVE_UNFILLED:
            self._release_all_exposure()
            self._state = ClientOrderState.AGGRESSIVE_UNFILLED
        elif kind is ReportKind.TECHNICAL_RESET:
            self._release_all_exposure()
            self._state = ClientOrderState.TECHNICAL

        self._assert_invariant()
        return True

    def _apply_fill(self, report: ExchangeReport) -> None:
        execution = report.execution
        # A FILL must carry an execution; idempotency keyed on the execution id.
        if execution is not None:
            if execution.execution_id in self._seen_execution_ids:
                # Duplicate execution detail: release remaining exposure but do not count the
                # execution twice (T15).
                self._release_all_exposure()
                return
            if self._reconciled_without_execution:
                # R17: the executed quantity was already reconciled from a terminal cancel
                # response; record this (first) Execution for cost but do not re-increment the
                # count (it is already 1). This is the single cost row for the task.
                self._seen_execution_ids.add(execution.execution_id)
                self._executions.append(execution)
                self._completion_time_ns = execution.exchange_time_ns
                self._reconciled_without_execution = False
            else:
                assert self._reported_executed == 0, (
                    "second distinct execution on a one-contract task (ledger violation)"
                )
                self._seen_execution_ids.add(execution.execution_id)
                self._executions.append(execution)
                self._reported_executed += 1
                self._completion_time_ns = execution.exchange_time_ns
        self._release_all_exposure()
        self._state = ClientOrderState.FILLED

    def _assert_invariant(self) -> None:
        assert self._reported_executed + self._reserved <= 1, (
            f"exposure invariant violated: executed={self._reported_executed} "
            f"reserved={self._reserved}"
        )

    # -- copy ------------------------------------------------------------------------

    def copy(self) -> ClientOMS:
        """Independent copy for :meth:`World.clone` (T41)."""
        clone = ClientOMS(self._task_id)
        clone._state = self._state
        clone._reported_executed = self._reported_executed
        clone._reserved = self._reserved
        clone._reconciled_without_execution = self._reconciled_without_execution
        clone._commands = dict(self._commands)
        clone._seen_report_ids = set(self._seen_report_ids)
        clone._seen_execution_ids = set(self._seen_execution_ids)
        clone._passive_command_id = self._passive_command_id
        clone._executions = list(self._executions)
        clone._completion_time_ns = self._completion_time_ns
        return clone
