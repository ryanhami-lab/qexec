"""StudyConfig tests: JSON round-trip, unknown-key rejection, split determinism, quick preset."""

from __future__ import annotations

import pytest

from qexec.core.config import HORIZON_LADDER_NS
from qexec.experiments.config import DATA_KIND, StudyConfig


def test_defaults_match_spec() -> None:
    cfg = StudyConfig(study_id="s")
    assert cfg.n_sessions == 10
    assert cfg.duration_s == 600
    assert cfg.epsilon == 0.001
    assert cfg.split_fractions == (0.5, 0.2, 0.3)
    assert cfg.support_min_eligible == 30
    assert cfg.support_min_queue_depletion == 3
    assert cfg.support_margin_frac == 0.5
    assert cfg.n_pilot_sessions == 3
    assert cfg.bootstrap_n == 2000
    assert cfg.horizon_ladder_ns == HORIZON_LADDER_NS
    assert cfg.max_workers >= 1


def test_json_round_trip_is_exact() -> None:
    cfg = StudyConfig(study_id="study", n_sessions=12, max_workers=4)
    restored = StudyConfig.from_dict(cfg.to_dict())
    assert restored == cfg


def test_support_margin_frac_round_trips_and_validates() -> None:
    cfg = StudyConfig(study_id="study", support_margin_frac=0.25)
    raw = cfg.to_dict()
    assert raw["support_margin_frac"] == 0.25
    assert StudyConfig.from_dict(raw) == cfg
    # Nonnegative, finite required.
    with pytest.raises(ValueError, match="support_margin_frac"):
        StudyConfig(study_id="s", support_margin_frac=-0.1)
    with pytest.raises(ValueError, match="support_margin_frac"):
        StudyConfig(study_id="s", support_margin_frac=float("inf"))


def test_unknown_key_rejected() -> None:
    raw = StudyConfig(study_id="s").to_dict()
    raw["bogus"] = 1
    with pytest.raises(ValueError, match="unknown study configuration keys"):
        StudyConfig.from_dict(raw)


def test_data_kind_is_synthetic() -> None:
    assert DATA_KIND == "SYNTHETIC"


def test_split_counts_min_one_each() -> None:
    # 10 sessions, (0.5, 0.2, 0.3) -> 5 train / 2 validation / 3 test.
    assert StudyConfig(study_id="s", n_sessions=10).split_counts == (5, 2, 3)
    # OOF action training needs two train sessions in addition to validation and test.
    assert StudyConfig(study_id="s", n_sessions=4, n_pilot_sessions=1).split_counts == (2, 1, 1)


def test_split_counts_sum_to_n() -> None:
    for n in range(4, 20):
        cfg = StudyConfig(study_id="s", n_sessions=n, n_pilot_sessions=1)
        counts = cfg.split_counts
        assert sum(counts) == n
        assert all(c >= 1 for c in counts)


def test_rejects_too_few_sessions() -> None:
    with pytest.raises(ValueError, match="n_sessions"):
        StudyConfig(study_id="s", n_sessions=2)


def test_rejects_split_not_summing_to_one() -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        StudyConfig(study_id="s", split_fractions=(0.5, 0.2, 0.2))


def test_scenarios_must_include_primary() -> None:
    with pytest.raises(ValueError, match="primary latency"):
        StudyConfig(study_id="s", scenarios=("L0", "L6"))


def test_quick_preset_values() -> None:
    cfg = StudyConfig.quick_preset()
    assert cfg.quick is True
    assert cfg.n_sessions == 4
    assert cfg.duration_s == 120
    assert cfg.scenarios == ("L1", "L6")
    assert cfg.support_min_eligible == 5
    assert cfg.support_min_queue_depletion == 1
    assert cfg.n_pilot_sessions == 1
    assert cfg.bootstrap_n == 200
    assert cfg.max_workers == 2
    assert cfg.split_counts == (2, 1, 1)
