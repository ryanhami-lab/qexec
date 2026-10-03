"""Terminal cancel-and-complete controller (architecture section 7.3, engine spec section 5).

``SWITCH_TO_TAKER`` (or the terminal cutoff) enters an absorbing, idempotent control mode. The
controller observes the *client* OMS state and emits abstract command intents; the engine turns
each intent into a real :class:`~qexec.core.messages.ExchangeCommand` scheduled with the
scenario's computation + entry delays, and feeds every delivered report back via
:meth:`on_report`. The controller never talks to the exchange directly and never inspects
simulator truth.

Longest path (architecture 7.3): resolve an in-flight submission -> its ACCEPTED -> CANCEL ->
terminal response -> residual AGGRESSIVE. The guard ``G = 3(c+e) + 2r + 1`` bounds it.

Behavior by client state at entry and on each subsequent report (engine spec section 5):

* ``NONE`` / ``REJECTED`` (no live exposure, aggressive attempt unused): send ``AGGRESSIVE``
  (qty ``1 - reported_executed``).
* ``IN_FLIGHT`` passive: wait; on ``ACCEPTED`` -> send ``CANCEL``; on ``FILL`` -> done;
  on ``REJECTED`` -> ``AGGRESSIVE``.
* ``WORKING``: send ``CANCEL``; on ``CANCELLED`` -> ``AGGRESSIVE`` for the residual; on ``FILL``
  during the cancel -> wait for the cancel's terminal response, reconcile, and send no
  aggressive if the residual is 0.
* ``FILLED``: nothing.
* ``AGGRESSIVE_UNFILLED`` / aggressive already used: nothing (no retries).
* ``TECHNICAL`` (technical reset): nothing (the task is technically unevaluable).

At most **one** aggressive attempt per world, ever (T40, T53): once an aggressive command is
emitted the controller sets ``aggressive_used`` and will never emit another, even on repeated
:meth:`enter` or late reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qexec.core.messages import ExchangeReport, ReportKind
from qexec.core.views import ClientOrderState


class IntentKind(Enum):
    """An abstract command the controller asks the engine to send."""

    CANCEL = "CANCEL"
    AGGRESSIVE = "AGGRESSIVE"


@dataclass(frozen=True, slots=True)
class CommandIntent:
    """A command the engine should create (computation + entry delays applied by the engine)."""

    kind: IntentKind
    quantity: int
    target_command_id: str | None = None
    """For CANCEL: the passive command being cancelled."""


class TerminalController:
    """Absorbing, idempotent terminal controller for one world (one aggressive attempt)."""

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id
        self._active = False
        self._aggressive_used = False
        self._cancel_sent = False
        self._done = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def aggressive_used(self) -> bool:
        return self._aggressive_used

    @property
    def done(self) -> bool:
        """True once the controller has no further action to take."""
        return self._done

    # -- entry -----------------------------------------------------------------------

    def enter(self, oms_state: ClientOrderState, reported_executed: int) -> list[CommandIntent]:
        """Enter the controller (idempotent). Return the command intents to send now.

        Repeated calls after the first entry return no new intents unless the controller is
        still waiting for a report that it has not yet acted on (the engine drives subsequent
        steps through :meth:`on_report`, so ``enter`` only acts once at the initial transition).
        """
        if self._active:
            # Idempotent: a second enter (e.g. SWITCH then the cutoff) does nothing.
            return []
        self._active = True
        return self._decide(oms_state, reported_executed)

    def on_report(
        self, report: ExchangeReport, oms_state: ClientOrderState, reported_executed: int
    ) -> list[CommandIntent]:
        """React to a delivered report while active; return any follow-up command intents."""
        if not self._active or self._done:
            return []
        kind = report.kind
        if kind is ReportKind.ACCEPTED:
            # A previously in-flight passive is now working -> cancel it.
            return self._maybe_cancel(oms_state)
        if kind is ReportKind.CANCELLED:
            # Working exposure cancelled -> aggressive for the residual.
            return self._maybe_aggressive(reported_executed)
        if kind is ReportKind.REJECTED:
            # Passive rejected while in flight -> aggressive (the passive attempt is consumed).
            return self._maybe_aggressive(reported_executed)
        if kind is ReportKind.CANCEL_REJECTED_TERMINAL:
            # The target was already terminal (filled during the cancel, or already cancelled).
            # Reconcile: if executed, we are done; else send aggressive for the residual.
            if reported_executed >= 1:
                self._done = True
                return []
            return self._maybe_aggressive(reported_executed)
        if kind is ReportKind.FILL:
            # Filled (passively, or by our aggressive) -> done.
            self._done = True
            return []
        # AGGRESSIVE_UNFILLED / TECHNICAL_RESET (and any other terminal) -> nothing further.
        self._done = True
        return []

    # -- decision helpers ------------------------------------------------------------

    def _decide(self, oms_state: ClientOrderState, reported_executed: int) -> list[CommandIntent]:
        if reported_executed >= 1 or oms_state is ClientOrderState.FILLED:
            self._done = True
            return []
        if oms_state in (ClientOrderState.NONE, ClientOrderState.REJECTED):
            return self._maybe_aggressive(reported_executed)
        if oms_state is ClientOrderState.IN_FLIGHT:
            # Wait for the submission to resolve (ACCEPTED/FILL/REJECTED) before acting.
            return []
        if oms_state is ClientOrderState.WORKING:
            return self._maybe_cancel(oms_state)
        if oms_state is ClientOrderState.CANCEL_PENDING:
            # Already cancelling; wait for the terminal response.
            return []
        # AGGRESSIVE_UNFILLED / CANCELLED / TECHNICAL: nothing further.
        self._done = True
        return []

    def _maybe_cancel(self, oms_state: ClientOrderState) -> list[CommandIntent]:
        if self._cancel_sent:
            return []
        if oms_state is not ClientOrderState.WORKING:
            return []
        self._cancel_sent = True
        return [CommandIntent(IntentKind.CANCEL, quantity=1, target_command_id=None)]

    def _maybe_aggressive(self, reported_executed: int) -> list[CommandIntent]:
        if self._aggressive_used:
            return []
        residual = 1 - reported_executed
        if residual <= 0:
            self._done = True
            return []
        self._aggressive_used = True
        return [CommandIntent(IntentKind.AGGRESSIVE, quantity=residual)]

    # -- copy ------------------------------------------------------------------------

    def copy(self) -> TerminalController:
        """Independent copy for :meth:`World.clone` (T41)."""
        clone = TerminalController(self._task_id)
        clone._active = self._active
        clone._aggressive_used = self._aggressive_used
        clone._cancel_sent = self._cancel_sent
        clone._done = self._done
        return clone
