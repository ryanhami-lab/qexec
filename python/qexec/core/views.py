"""Client-plane decision view (architecture section 7.1).

A ``DecisionView`` contains only information delivered to the client by ``now_ns``. It never
contains the evaluator's arrival benchmark ``m0``, simulator-truth queue position, or
unreported executions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from qexec.core.types import BookLevel, TaskSide, TradingStatus


class ClientOrderState(Enum):
    """Client OMS view of the task's exposure (architecture section 7.2)."""

    NONE = "NONE"
    """No command created yet (or B0 before its aggressive command)."""
    IN_FLIGHT = "IN_FLIGHT"
    """A command has been created; no definitive report received yet."""
    WORKING = "WORKING"
    """Passive order acknowledged as resting and not reported filled."""
    CANCEL_PENDING = "CANCEL_PENDING"
    FILLED = "FILLED"
    """Client has received the fill report (task complete from the client's view)."""
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    AGGRESSIVE_UNFILLED = "AGGRESSIVE_UNFILLED"
    TECHNICAL = "TECHNICAL"


@dataclass(frozen=True, slots=True)
class DecisionView:
    now_ns: int
    task_id: str
    session_id: str
    side: TaskSide
    horizon_ns: int
    deadline_ns: int
    time_remaining_ns: int
    client_status: TradingStatus
    best_bid: BookLevel | None
    best_ask: BookLevel | None
    client_mid2: int | None
    client_quote_age_ns: int | None
    """``now - observation_time`` of the most recently delivered batch."""
    order_state: ClientOrderState
    pending_command: bool
    reported_executed: int
    own_limit_price_fixed: int | None
    order_age_ns: int | None
    """Time since the passive command was created, if any."""
    in_controller: bool
    market_features: Mapping[str, float] = field(default_factory=dict)
    queue_features: Mapping[str, float] = field(default_factory=dict)
    price_signal: Mapping[str, float] = field(default_factory=dict)
