"""Command and report messages exchanged between the client plane and the exchange overlay.

Shared by ``sim.overlay`` (exchange side) and ``sim.oms`` / ``sim.controller`` (client side) so
both planes use one definition (architecture sections 7.2-7.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qexec.core.tasks import Execution
from qexec.core.types import TaskSide


class CommandKind(Enum):
    PASSIVE_LIMIT = "PASSIVE_LIMIT"
    """One-contract limit at a client-chosen price (JOIN_BEST)."""
    AGGRESSIVE = "AGGRESSIVE"
    """SYNTHETIC_DIRECT_TOP: one attempt at the opposite best; never rests, never retries."""
    CANCEL = "CANCEL"
    """Cancel the task's working passive order (``target_command_id``)."""


class ReportKind(Enum):
    ACCEPTED = "ACCEPTED"
    """Passive limit accepted and resting."""
    REJECTED = "REJECTED"
    """Command rejected (invalid status, invalid price, ...). Terminal for that command."""
    FILL = "FILL"
    """Execution of the virtual order. ``execution`` is set. For the one-contract core a FILL
    is terminal for the passive or aggressive command."""
    CANCELLED = "CANCELLED"
    """Working passive order cancelled; terminal."""
    CANCEL_REJECTED_TERMINAL = "CANCEL_REJECTED_TERMINAL"
    """Cancel arrived after the target was already terminal (filled/cancelled/rejected).
    ``cumulative_executed`` states the target's final executed quantity."""
    AGGRESSIVE_UNFILLED = "AGGRESSIVE_UNFILLED"
    """Aggressive attempt found no executable liquidity or trading not permitted; terminal."""
    TECHNICAL_RESET = "TECHNICAL_RESET"
    """Book reset/snapshot while working: queue continuity lost; task technically invalid."""


TERMINAL_REPORTS: frozenset[ReportKind] = frozenset(
    {
        ReportKind.REJECTED,
        ReportKind.FILL,
        ReportKind.CANCELLED,
        ReportKind.CANCEL_REJECTED_TERMINAL,
        ReportKind.AGGRESSIVE_UNFILLED,
        ReportKind.TECHNICAL_RESET,
    }
)


@dataclass(frozen=True, slots=True)
class ExchangeCommand:
    """A command as it arrives at the exchange (after computation + entry delay)."""

    command_id: str
    task_id: str
    kind: CommandKind
    side: TaskSide
    quantity: int
    arrival_time_ns: int
    limit_price_fixed: int | None = None
    target_command_id: str | None = None
    """For CANCEL: the passive command being cancelled."""


@dataclass(frozen=True, slots=True)
class ExchangeReport:
    """A report as generated at the exchange; delivered to the client after response delay.

    ``report_id`` is unique and idempotent. ``cumulative_executed`` is the total executed
    quantity of the *task's virtual exposure* after this report (0 or 1 in the core).
    """

    report_id: str
    command_id: str
    task_id: str
    kind: ReportKind
    exchange_time_ns: int
    cumulative_executed: int
    execution: Execution | None = None
    reason: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_REPORTS
