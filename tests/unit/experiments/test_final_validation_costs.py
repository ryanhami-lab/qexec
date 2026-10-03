"""An evaluable deadline miss cannot disappear from B2 threshold selection."""

from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from qexec.experiments import pipeline
from qexec.experiments.config import StudyConfig


@pytest.mark.parametrize("cost", [None, float("nan"), float("inf")])
def test_threshold_selection_refuses_undefined_evaluable_costs(
    monkeypatch: pytest.MonkeyPatch, cost: float | None
) -> None:
    results = pl.DataFrame(
        {"policy_id": ["B2@0.0"], "status": ["DEADLINE_MISS"], "c_t_ticks": [cost]},
        schema_overrides={"c_t_ticks": pl.Float64},
    )
    monkeypatch.setattr(pipeline, "PriceSignalScorer", lambda *args: object())
    monkeypatch.setattr(
        pipeline,
        "SessionEngine",
        lambda *args, **kwargs: SimpleNamespace(run=lambda: SimpleNamespace(task_results=results)),
    )
    split = pipeline._Split(("a", "b"), ("v",), ("t",), {"v": Path("unused")})
    price = SimpleNamespace(primary_model=None, secondary_model=None)
    with pytest.raises(ValueError, match=r"validation.*C_T"):
        pipeline._run_validation_selection(StudyConfig.quick_preset(), split, price, 10**9)
