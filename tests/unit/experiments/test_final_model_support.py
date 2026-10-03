"""Every action needs adequate fitted populations; failed refits are atomic."""

import numpy as np
import polars as pl
import pytest

from qexec.core.tasks import CheckpointChoice
from qexec.experiments.pipeline import _fit_action_arm
from qexec.models.action import ActionModels
from qexec.models.schema import FeatureSchema


@pytest.mark.parametrize("costs", [[1.0, 1.0], [1.0, None, 1.0, 1.0]])
def test_support_requires_each_miss_and_cost_population(costs: list[float | None]) -> None:
    schema = FeatureSchema.of(["x"])
    count = len(costs) // 2
    rows = pl.DataFrame(
        {
            "action": ["HOLD"] * count + ["SWITCH"] * count,
            "feat_x": [0.0] * len(costs),
            "miss": [0] * len(costs),
            "c_t_ticks": costs,
        }
    )
    _, support = _fit_action_arm(schema, rows, rows, min_rows=2, margin_frac=0.0)
    assert support.n_training_rows == len(costs)  # Union range-fitting matrix is preserved.
    assert not support.is_supported(np.zeros(1))


@pytest.mark.parametrize("kwargs", [{"x_val": np.zeros((1, 1))}, {"cost_alpha": 999.0}])
def test_failed_refit_preserves_complete_previous_model(kwargs: dict[str, object]) -> None:
    models = ActionModels(FeatureSchema.of(["x"]))
    for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
        models.fit_action(action, np.zeros((2, 1)), np.zeros(2), np.zeros(2))
    before = models.predict(np.zeros(1))
    with pytest.raises(ValueError):
        models.fit_action(CheckpointChoice.HOLD, np.zeros((2, 1)), np.ones(2), np.ones(2), **kwargs)
    assert models.predict(np.zeros(1)) == before
