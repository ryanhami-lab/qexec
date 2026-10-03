"""Frozen study configuration (research spec section 2).

``StudyConfig`` is an immutable dataclass with a strict JSON round-trip: unknown keys are
rejected (so a typo never silently uses a default), and ``to_dict``/``from_dict`` are exact
inverses. Defaults come from :mod:`qexec.core.config` so the study and the engine share a single
source of truth for the horizon ladder, epsilon sensitivity set, and latency scenarios.

The ``quick`` preset (:meth:`StudyConfig.quick_preset`) is the CI smoke / demo configuration:
4 sessions x 120 s, scenarios (L1, L6), a short horizon ladder, scaled-down support minima, and
``max_workers = 2`` -- it must finish well under 180 s on the development machine.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from qexec.core.config import (
    EPSILON_SENSITIVITY,
    HORIZON_LADDER_NS,
    LATENCY_SCENARIOS,
    PRIMARY_LATENCY_ID,
    S,
)

__all__ = ["DATA_KIND", "StudyConfig"]

DATA_KIND = "SYNTHETIC"
"""Every written artifact carries this; results are software validation, never market facts."""

_ALL_SCENARIOS: tuple[str, ...] = tuple(LATENCY_SCENARIOS.keys())


def _default_max_workers() -> int:
    cpu = os.cpu_count() or 1
    return min(8, cpu)


@dataclass(frozen=True, slots=True)
class StudyConfig:
    """Immutable configuration for one research study (research spec section 2)."""

    study_id: str
    n_sessions: int = 10
    duration_s: int = 600
    base_seed: int = 0
    epsilon: float = 0.001
    epsilon_sensitivity: tuple[float, ...] = EPSILON_SENSITIVITY
    horizon_ladder_ns: tuple[int, ...] = HORIZON_LADDER_NS
    scenarios: tuple[str, ...] = _ALL_SCENARIOS
    split_fractions: tuple[float, float, float] = (0.5, 0.2, 0.3)
    support_min_eligible: int = 30
    support_min_queue_depletion: int = 3
    support_margin_frac: float = 0.5
    n_pilot_sessions: int = 3
    bootstrap_n: int = 2000
    bootstrap_seed: int = 0
    max_workers: int = field(default_factory=_default_max_workers)
    quick: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.study_id, str) or not self.study_id.strip():
            raise ValueError("study_id must be a non-empty string")
        for name in (
            "n_sessions",
            "duration_s",
            "base_seed",
            "support_min_eligible",
            "support_min_queue_depletion",
            "n_pilot_sessions",
            "bootstrap_n",
            "bootstrap_seed",
            "max_workers",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not isinstance(self.quick, bool):
            raise ValueError("quick must be a boolean")
        if self.n_sessions < 4:
            raise ValueError("n_sessions must be >= 4 (2 train for OOF, 1 validation, 1 test)")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if not _finite_number(self.epsilon) or not 0.0 <= self.epsilon <= 1.0:
            raise ValueError("epsilon must lie in [0, 1]")
        for name in ("split_fractions", "horizon_ladder_ns", "scenarios", "epsilon_sensitivity"):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"{name} must be an immutable tuple")
        if len(self.split_fractions) != 3:
            raise ValueError("split_fractions must be a 3-tuple (train, validation, test)")
        if any(not _finite_number(f) for f in self.split_fractions):
            raise ValueError("split_fractions must be finite numbers")
        if abs(sum(self.split_fractions) - 1.0) > 1e-9:
            raise ValueError("split_fractions must sum to 1.0")
        if any(f <= 0.0 for f in self.split_fractions):
            raise ValueError("split_fractions must all be positive")
        if self.split_counts[0] < 2:
            raise ValueError("split_fractions must allocate at least 2 training sessions for OOF")
        if len(self.horizon_ladder_ns) == 0:
            raise ValueError("horizon_ladder_ns must be non-empty")
        if any(
            not isinstance(h, int) or isinstance(h, bool) or h <= 0 or h % 2
            for h in self.horizon_ladder_ns
        ):
            raise ValueError("every horizon must be a positive even integer")
        if len(set(self.horizon_ladder_ns)) != len(self.horizon_ladder_ns):
            raise ValueError("horizon_ladder_ns must not contain duplicates")
        if len(self.scenarios) == 0:
            raise ValueError("scenarios must be non-empty")
        if any(not isinstance(s, str) for s in self.scenarios):
            raise ValueError("scenarios must contain strings")
        if len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("scenarios must not contain duplicates")
        unknown_scenarios = [s for s in self.scenarios if s not in LATENCY_SCENARIOS]
        if unknown_scenarios:
            raise ValueError(f"unknown latency scenarios: {unknown_scenarios}")
        if PRIMARY_LATENCY_ID not in self.scenarios:
            raise ValueError(f"scenarios must include the primary latency {PRIMARY_LATENCY_ID!r}")
        if not 1 <= self.n_pilot_sessions <= self.split_counts[0]:
            raise ValueError("n_pilot_sessions must lie in [1, number of training sessions]")
        if self.support_min_eligible < 0 or self.support_min_queue_depletion < 0:
            raise ValueError("support minima must be nonnegative")
        if not _finite_number(self.support_margin_frac) or self.support_margin_frac < 0.0:
            raise ValueError("support_margin_frac must be finite and nonnegative")
        if self.bootstrap_n <= 0:
            raise ValueError("bootstrap_n must be positive")
        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if len(self.epsilon_sensitivity) == 0:
            raise ValueError("epsilon_sensitivity must be non-empty")
        if any(not _finite_number(e) or not 0.0 <= e <= 1.0 for e in self.epsilon_sensitivity):
            raise ValueError("epsilon_sensitivity values must be finite and lie in [0, 1]")
        if len(set(self.epsilon_sensitivity)) != len(self.epsilon_sensitivity):
            raise ValueError("epsilon_sensitivity must not contain duplicates")

    @property
    def split_counts(self) -> tuple[int, int, int]:
        """Chronological split sizes by session index, with at least one session per split."""
        n = self.n_sessions
        n_train = max(1, round(n * self.split_fractions[0]))
        n_val = max(1, round(n * self.split_fractions[1]))
        # Test takes the remainder; guarantee at least one and leave at least one each earlier.
        if n_train + n_val >= n:
            n_train = max(1, n - 2)
            n_val = 1
        n_test = n - n_train - n_val
        if n_test < 1:
            n_test = 1
            n_val = max(1, n - n_train - n_test)
        return (n_train, n_val, n_test)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["epsilon_sensitivity"] = list(self.epsilon_sensitivity)
        out["horizon_ladder_ns"] = list(self.horizon_ladder_ns)
        out["scenarios"] = list(self.scenarios)
        out["split_fractions"] = list(self.split_fractions)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StudyConfig:
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown study configuration keys: {sorted(unknown)}")
        data = dict(raw)
        for name in ("epsilon_sensitivity", "horizon_ladder_ns", "scenarios", "split_fractions"):
            if name in data:
                if not isinstance(data[name], (list, tuple)):
                    raise ValueError(f"{name} must be an array")
                # Preserve input types: coercing 2.5 to 2 silently changes a frozen horizon.
                data[name] = tuple(data[name])
        return cls(**data)

    @classmethod
    def quick_preset(cls, study_id: str = "quick", base_seed: int = 0) -> StudyConfig:
        """The CI smoke / demo configuration (research spec section 2)."""
        return cls(
            study_id=study_id,
            n_sessions=4,
            duration_s=120,
            base_seed=base_seed,
            epsilon=0.001,
            horizon_ladder_ns=(1 * S, 5 * S),
            scenarios=("L1", "L6"),
            support_min_eligible=5,
            support_min_queue_depletion=1,
            n_pilot_sessions=1,
            bootstrap_n=200,
            max_workers=2,
            quick=True,
        )


def _finite_number(value: object) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
