"""Experiment configuration contracts (product section 6.3, architecture sections 6-7).

All values resolve to integer nanoseconds. Unknown keys are rejected by
``ExperimentConfig.from_dict``. Values here are proposed defaults, not measured facts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any

from qexec.core.types import TaskSide, TimeNs

US = 1_000
MS = 1_000_000
S = 1_000_000_000


@dataclass(frozen=True, slots=True)
class LatencyScenario:
    """Constant nonnegative delays in integer nanoseconds (architecture section 6.2)."""

    scenario_id: str
    added_delivery_ns: int
    computation_ns: int
    entry_ns: int
    response_ns: int

    def __post_init__(self) -> None:
        for name in ("added_delivery_ns", "computation_ns", "entry_ns", "response_ns"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer, got {value!r}")

    @property
    def guard_ns(self) -> int:
        """Conservative terminal guard ``G = 3(c+e) + 2r + 1 ns`` (architecture 7.3)."""
        return guard_ns(self.computation_ns, self.entry_ns, self.response_ns)


def guard_ns(computation_ns: int, entry_ns: int, response_ns: int) -> int:
    return 3 * (computation_ns + entry_ns) + 2 * response_ns + 1


def _us(sid: str, d: int, c: int, e: int, r: int) -> LatencyScenario:
    return LatencyScenario(sid, d * US, c * US, e * US, r * US)


LATENCY_SCENARIOS: dict[str, LatencyScenario] = {
    s.scenario_id: s
    for s in (
        _us("L0", 0, 0, 0, 0),
        _us("L1", 0, 50, 250, 250),
        _us("L2", 1000, 50, 250, 250),
        _us("L3", 0, 500, 250, 250),
        _us("L4", 0, 50, 2500, 250),
        _us("L5", 0, 50, 250, 2500),
        _us("L6", 1000, 500, 2500, 2500),
    )
}
PRIMARY_LATENCY_ID = "L1"

HORIZON_LADDER_NS: tuple[int, ...] = (1 * S, 5 * S, 30 * S)
EPSILON_SENSITIVITY: tuple[float, ...] = (0.0, 0.0001, 0.001, 0.01, 1.0)
THETA_MENU: tuple[float, ...] = (0.0, 0.1, 0.2)
MARKOUT_HORIZONS_NS: tuple[int, ...] = (10 * MS, 100 * MS, 1 * S)
SECONDARY_PRICE_TAU_NS: int = 100 * MS


def checkpoint_valid(horizon_ns: int, guard: int) -> bool:
    """Core protocol requires ``0 < H/2 < H - G`` (exact integer arithmetic)."""
    return horizon_ns > 0 and horizon_ns < 2 * (horizon_ns - guard)


def primary_price_tau_ns(horizon_ns: int) -> int:
    """Primary price-target horizon ``tau = H/2 - G_L1``, frozen at its L1 value."""
    tau = horizon_ns // 2 - LATENCY_SCENARIOS[PRIMARY_LATENCY_ID].guard_ns
    if tau <= 0:
        raise ValueError("horizon too short for a positive primary price-target horizon")
    return tau


def arrival_spacing_ns(horizon_ns: int) -> int:
    """Same-side task windows never overlap: spacing ``max(10 s, 2H)``."""
    return max(10 * S, 2 * horizon_ns)


class FillRule(Enum):
    """Named counterfactual queue/fill rule (architecture section 8)."""

    DIRECT_FIFO = "DIRECT_FIFO"


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    experiment_id: str
    horizon_ns: int = 1 * S
    latency_id: str = PRIMARY_LATENCY_ID
    epsilon: float = 0.001
    fee_per_contract_fixed: int = 0
    """Signed fee per executed contract in fixed-point currency units (1e-9 currency)."""
    fill_rule: FillRule = FillRule.DIRECT_FIFO
    sides: tuple[TaskSide, ...] = (TaskSide.BUY, TaskSide.SELL)
    task_seed: int = 0
    session_window_start_s: int = 0
    """Seconds after session start at which the eligible task window begins."""
    session_window_end_s: int = 23_400
    warmup_ns: int = 1 * S
    planned_scenario_ids: tuple[str, ...] | None = None
    """Common task population across a study; None means the standalone active scenario."""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def latency(self) -> LatencyScenario:
        return LATENCY_SCENARIOS[self.latency_id]

    @property
    def checkpoint_offset_ns(self) -> int:
        return self.horizon_ns // 2

    @property
    def cutoff_offset_ns(self) -> int:
        return self.horizon_ns - self.latency.guard_ns

    def validate(self) -> None:
        if not isinstance(self.experiment_id, str) or not self.experiment_id.strip():
            raise ValueError("experiment_id must be a nonempty string")
        for name in (
            "horizon_ns",
            "fee_per_contract_fixed",
            "task_seed",
            "session_window_start_s",
            "session_window_end_s",
            "warmup_ns",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
        if self.warmup_ns < 0 or self.session_window_start_s < 0:
            raise ValueError("warmup_ns and session_window_start_s must be nonnegative")
        if (
            not self.sides
            or any(not isinstance(side, TaskSide) for side in self.sides)
            or len(set(self.sides)) != len(self.sides)
        ):
            raise ValueError("sides must contain distinct TaskSide values")
        if not isinstance(self.fill_rule, FillRule):
            raise ValueError("fill_rule must be a FillRule")
        if isinstance(self.epsilon, bool) or not isinstance(self.epsilon, (int, float)):
            raise ValueError("epsilon must be numeric")
        if self.latency_id not in LATENCY_SCENARIOS:
            raise ValueError(f"unknown latency scenario {self.latency_id!r}")
        if self.planned_scenario_ids is not None:
            planned = self.planned_scenario_ids
            if (
                not isinstance(planned, tuple)
                or not planned
                or any(not isinstance(s, str) or s not in LATENCY_SCENARIOS for s in planned)
                or len(set(planned)) != len(planned)
                or self.latency_id not in planned
            ):
                raise ValueError(
                    "planned_scenario_ids must be distinct known scenarios including active"
                )
            if any(
                not checkpoint_valid(self.horizon_ns, LATENCY_SCENARIOS[s].guard_ns)
                for s in planned
            ):
                raise ValueError("configuration violates checkpoint timing in a planned scenario")
        if self.horizon_ns % 2:
            raise ValueError("horizon_ns must be even so the checkpoint is an exact integer")
        if not checkpoint_valid(self.horizon_ns, self.latency.guard_ns):
            raise ValueError("configuration violates 0 < H/2 < H - G")
        if not 0.0 <= self.epsilon <= 1.0:
            raise ValueError("epsilon must lie in [0, 1]")
        if self.session_window_end_s <= self.session_window_start_s:
            raise ValueError("empty session window")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExperimentConfig:
        known = {f.name for f in fields(cls)}
        data = dict(raw)
        # ``to_dict`` emits the derived convenience key ``guard_ns`` (it is not a constructor
        # field). Accept it on round-trip, but only when it is consistent with the reconstructed
        # configuration; a mismatched derived value is a hard error (R18).
        has_guard = "guard_ns" in data
        declared_guard = data.pop("guard_ns", None)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown configuration keys: {sorted(unknown)}")
        if "fill_rule" in data:
            data["fill_rule"] = FillRule(data["fill_rule"])
        if "sides" in data:
            data["sides"] = tuple(
                TaskSide[s] if isinstance(s, str) else TaskSide(s) for s in data["sides"]
            )
        if data.get("planned_scenario_ids") is not None:
            if not isinstance(data["planned_scenario_ids"], (list, tuple)):
                raise ValueError("planned_scenario_ids must be an array")
            data["planned_scenario_ids"] = tuple(data["planned_scenario_ids"])
        cfg = cls(**data)
        cfg.validate()
        if has_guard and (
            not isinstance(declared_guard, int)
            or isinstance(declared_guard, bool)
            or declared_guard != cfg.latency.guard_ns
        ):
            raise ValueError(
                f"inconsistent derived guard_ns: got {declared_guard!r}, "
                f"expected {cfg.latency.guard_ns} for latency {cfg.latency_id!r}"
            )
        return cfg

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["fill_rule"] = self.fill_rule.value
        out["sides"] = [s.name for s in self.sides]
        out["guard_ns"] = self.latency.guard_ns
        out["planned_scenario_ids"] = (
            list(self.planned_scenario_ids) if self.planned_scenario_ids is not None else None
        )
        return out


def to_time(value: int) -> TimeNs:
    return TimeNs(value)
