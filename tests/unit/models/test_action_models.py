"""ActionModels tests: per-action predict returns p_hat/v_hat; constant miss model when a single
class is present (with recorded n and constant flag); deterministic; feeds risk_allowance_choice.
"""

from __future__ import annotations

import numpy as np

from qexec.core.tasks import CheckpointChoice
from qexec.models.action import ActionModels
from qexec.models.decision import risk_allowance_choice
from qexec.models.schema import FeatureSchema

HOLD = CheckpointChoice.HOLD
SWITCH = CheckpointChoice.SWITCH


def _schema() -> FeatureSchema:
    return FeatureSchema.of(["f0", "f1"])


def _data(n: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 2))
    # miss correlated with f0 > 0; cost linear in f1.
    miss = (x[:, 0] > 0.0).astype(float)
    cost = 2.0 * x[:, 1] + 0.5
    return x, miss, cost


def test_predict_returns_p_and_v_per_action() -> None:
    xh, mh, ch = _data(80, 0)
    xs, ms, cs = _data(80, 1)
    model = ActionModels(_schema())
    model.fit_action(HOLD, xh, mh, ch)
    model.fit_action(SWITCH, xs, ms, cs)
    pred = model.predict(np.array([0.5, 1.0]))
    assert set(pred.keys()) == {HOLD, SWITCH}
    for p in pred.values():
        assert 0.0 <= p.p_hat <= 1.0
        assert np.isfinite(p.v_hat)


def test_constant_miss_model_when_single_class() -> None:
    # All HOLD training rows miss=0 (no deadline misses observed for HOLD).
    x = np.random.default_rng(3).normal(size=(50, 2))
    miss = np.zeros(50)
    cost = np.ones(50)
    model = ActionModels(_schema())
    model.fit_action(HOLD, x, miss, cost)
    assert model.miss_is_constant(HOLD) is True
    assert model.miss_n(HOLD) == 50
    # Fit SWITCH with two classes so we can predict.
    xs, ms, cs = _data(60, 4)
    model.fit_action(SWITCH, xs, ms, cs)
    pred = model.predict(np.array([0.0, 0.0]))
    # Constant empirical rate for HOLD is 0.0 (observed), not asserted as a true zero elsewhere.
    assert pred[HOLD].p_hat == 0.0


def test_constant_rate_reflects_single_positive_class() -> None:
    x = np.random.default_rng(5).normal(size=(20, 2))
    miss = np.ones(20)  # every training example missed
    cost = np.ones(20)
    model = ActionModels(_schema())
    model.fit_action(HOLD, x, miss, cost)
    model.fit_action(SWITCH, x, miss, cost)
    pred = model.predict(np.array([1.0, 1.0]))
    assert model.miss_is_constant(HOLD)
    assert pred[HOLD].p_hat == 1.0


def test_predictions_feed_risk_allowance_choice() -> None:
    xh, mh, ch = _data(80, 6)
    xs, ms, cs = _data(80, 7)
    model = ActionModels(_schema())
    model.fit_action(HOLD, xh, mh, ch)
    model.fit_action(SWITCH, xs, ms, cs)
    pred = model.predict(np.array([0.3, -0.2]))
    p_hat = {HOLD: pred[HOLD].p_hat, SWITCH: pred[SWITCH].p_hat}
    v_hat = {HOLD: pred[HOLD].v_hat, SWITCH: pred[SWITCH].v_hat}
    choice, reason = risk_allowance_choice(p_hat, v_hat, epsilon=0.001)
    assert choice in (HOLD, SWITCH)
    assert reason.name == "MODEL_CHOICE"


def test_alpha_selected_from_grid() -> None:
    xh, mh, ch = _data(80, 8)
    xv, _, cv = _data(40, 9)
    model = ActionModels(_schema(), alpha_grid=(0.01, 1.0, 100.0))
    model.fit_action(HOLD, xh, mh, ch, x_val=xv, cost_val=cv)
    assert model.chosen_alpha(HOLD) in (0.01, 1.0, 100.0)


def test_predictions_are_deterministic() -> None:
    xh, mh, ch = _data(80, 10)
    xs, ms, cs = _data(80, 11)

    def build() -> ActionModels:
        m = ActionModels(_schema())
        m.fit_action(HOLD, xh, mh, ch)
        m.fit_action(SWITCH, xs, ms, cs)
        return m

    p1 = build().predict(np.array([0.1, 0.2]))
    p2 = build().predict(np.array([0.1, 0.2]))
    assert p1[HOLD].p_hat == p2[HOLD].p_hat
    assert p1[SWITCH].v_hat == p2[SWITCH].v_hat
