"""Pairing must not multiply tasks or invent evidence from undefined costs."""

import polars as pl
import pytest

from qexec.analysis.stats import paired_miss_difference, session_paired_effect


def _rows() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "task_id": ["t", "t"],
            "session_id": ["s", "s"],
            "policy_id": ["B1", "B3"],
            "scenario_id": ["L1", "L1"],
            "status": ["COMPLETED_ON_TIME", "COMPLETED_ON_TIME"],
            "side": [1, 1],
            "is_ticks_net": [2.0, 1.0],
        }
    )


@pytest.mark.parametrize("fn", [session_paired_effect, paired_miss_difference])
def test_duplicate_policy_task_is_rejected(fn: object) -> None:
    assert callable(fn)
    rows = pl.concat([_rows(), _rows().head(1)])
    with pytest.raises(ValueError, match="duplicate"):
        fn(rows, "B1", "B3")


@pytest.mark.parametrize("column,value", [("session_id", "other"), ("side", -1)])
def test_pair_metadata_must_agree(column: str, value: object) -> None:
    rows = _rows().with_columns(
        pl.when(pl.col("policy_id") == "B3")
        .then(pl.lit(value))
        .otherwise(pl.col(column))
        .alias(column)
    )
    with pytest.raises(ValueError, match="metadata"):
        session_paired_effect(rows, "B1", "B3")


def test_missing_completed_cost_is_undefined_counted_without_erasing_miss_evidence() -> None:
    rows = _rows().with_columns(
        pl.when(pl.col("policy_id") == "B3")
        .then(None)
        .otherwise(pl.col("is_ticks_net"))
        .alias("is_ticks_net")
    )
    cost = session_paired_effect(rows, "B1", "B3")
    assert cost.estimate is None
    assert cost.n_tasks == 0
    assert cost.n_sessions_undefined == 1
    assert paired_miss_difference(rows, "B1", "B3").n_tasks == 1


def test_nan_completed_cost_is_a_hard_error() -> None:
    with pytest.raises(ValueError, match="finite"):
        session_paired_effect(
            _rows().with_columns(pl.lit(float("nan")).alias("is_ticks_net")), "B1", "B3"
        )
