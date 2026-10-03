"""Task, outcome, decision, and result-row contracts (product sections 5-8).

Analysis code consumes ``TaskResult`` rows only; it never reruns strategy logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qexec.core.types import TaskSide, TimeNs


class PolicyId(Enum):
    B0 = "B0"
    B1 = "B1"
    B2 = "B2"
    B2_100MS = "B2_100MS"
    B3 = "B3"
    B3_NO_QUEUE = "B3_NO_QUEUE"
    ORACLE = "ORACLE"
    """Explicitly unrealistic upper-information diagnostic; never a realistic comparator."""


class TaskStatus(Enum):
    COMPLETED_ON_TIME = "COMPLETED_ON_TIME"
    DEADLINE_MISS = "DEADLINE_MISS"
    TECHNICALLY_UNEVALUABLE = "TECHNICALLY_UNEVALUABLE"


class CheckpointChoice(Enum):
    HOLD = "HOLD"
    SWITCH = "SWITCH"


class DecisionReason(Enum):
    """Why a checkpoint produced its action (distinct codes, architecture 9.1)."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """Policy has no checkpoint decision (B0, B1)."""
    MODEL_CHOICE = "MODEL_CHOICE"
    REPORTED_COMPLETE = "REPORTED_COMPLETE"
    INELIGIBLE_PENDING_COMMAND = "INELIGIBLE_PENDING_COMMAND"
    INELIGIBLE_REJECTED = "INELIGIBLE_REJECTED"
    INELIGIBLE_NOT_WORKING = "INELIGIBLE_NOT_WORKING"
    INELIGIBLE_PARTIAL = "INELIGIBLE_PARTIAL"
    FALLBACK_NONFINITE = "FALLBACK_NONFINITE"
    FALLBACK_UNSUPPORTED = "FALLBACK_UNSUPPORTED"


class FillMechanism(Enum):
    """Virtual-fill classification (architecture section 8.2)."""

    QUEUE_DEPLETION = "QUEUE_DEPLETION"
    TRADE_THROUGH = "TRADE_THROUGH"
    AGGRESSIVE = "AGGRESSIVE"
    AMBIGUOUS = "AMBIGUOUS"


class Liquidity(Enum):
    PASSIVE = "PASSIVE"
    AGGRESSIVE = "AGGRESSIVE"


@dataclass(frozen=True, slots=True)
class Task:
    """Policy-independent execution task (product section 5)."""

    task_id: str
    session_id: str
    instrument_id: int
    side: TaskSide
    quantity: int
    arrival_time_ns: TimeNs
    deadline_ns: TimeNs
    arrival_reference_mid2: int
    """``m0`` as ``Mid2`` (twice the committed direct-book midpoint at arrival)."""
    eligibility_rule_version: str
    task_manifest_id: str


@dataclass(frozen=True, slots=True)
class Execution:
    """One economic execution of the virtual order (exchange ledger)."""

    execution_id: str
    task_id: str
    quantity: int
    price_fixed: int
    fee_fixed: int
    """Signed fee in 1e-9 currency units; positive is a charge."""
    exchange_time_ns: TimeNs
    liquidity: Liquidity
    mechanism: FillMechanism


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Persisted per-(task, policy, scenario) outcome row."""

    task_id: str
    session_id: str
    policy_id: str
    scenario_id: str
    side: int
    status: TaskStatus
    executed_quantity: int
    residual_quantity: int
    completion_time_ns: int | None
    """Actual simulated execution time of the completing fill, if on time."""
    avg_price_fixed: float | None
    fees_fixed: int
    is_ticks_gross: float | None
    is_ticks_net: float | None
    """Fee-adjusted implementation shortfall vs ``m0``; ``None`` unless completed on time."""
    c_t_ticks: float | None
    """Deadline-horizon value ``C_T`` vs ``z0`` (product section 8); ``None`` if unevaluable."""
    passive_filled: bool
    fill_mechanism: str | None
    checkpoint_eligible: bool
    checkpoint_choice: str | None
    decision_reason: str
    action_count: int
    diagnostic_late_completion_ns: int | None
