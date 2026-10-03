"""R18 reproduction: ``ExperimentConfig.from_dict(to_dict())`` must round-trip.

The external review found that ``to_dict`` injects the derived key ``guard_ns`` (a convenience
for consumers) while ``from_dict`` rejects any unknown key, so the obvious round-trip raises.
The fix accepts derived keys only when they are consistent with the reconstructed config.
"""

from __future__ import annotations

import pytest

from qexec.core.config import ExperimentConfig


def test_experiment_config_round_trip_through_to_dict() -> None:
    cfg = ExperimentConfig(experiment_id="x", horizon_ns=1_000_000_000, latency_id="L1")
    restored = ExperimentConfig.from_dict(cfg.to_dict())
    assert restored == cfg


def test_experiment_config_round_trip_with_custom_fields() -> None:
    cfg = ExperimentConfig(
        experiment_id="y",
        horizon_ns=5_000_000_000,
        latency_id="L6",
        epsilon=0.01,
        fee_per_contract_fixed=3,
    )
    assert ExperimentConfig.from_dict(cfg.to_dict()) == cfg


def test_experiment_config_rejects_inconsistent_derived_guard() -> None:
    raw = ExperimentConfig(experiment_id="z", horizon_ns=1_000_000_000, latency_id="L1").to_dict()
    raw["guard_ns"] = raw["guard_ns"] + 1  # inconsistent derived value
    with pytest.raises(ValueError, match="guard_ns"):
        ExperimentConfig.from_dict(raw)


def test_experiment_config_still_rejects_unknown_non_derived_key() -> None:
    raw = ExperimentConfig(experiment_id="z").to_dict()
    raw["bogus"] = 1
    with pytest.raises(ValueError, match="unknown configuration keys"):
        ExperimentConfig.from_dict(raw)
