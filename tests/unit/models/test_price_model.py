"""PriceModel tests: fixed class order (down, unchanged, up), missing class handled without
dropping the unchanged class, deterministic predictions, up_minus_down convenience.
"""

from __future__ import annotations

import numpy as np
import pytest

from qexec.models.price import PRICE_CLASSES, PriceModel
from qexec.models.schema import FeatureSchema


@pytest.mark.parametrize("label", [0.9, float("nan"), float("inf")])
def test_original_price_labels_must_be_discrete(label: float) -> None:
    with pytest.raises(ValueError, match="labels"):
        PriceModel(FeatureSchema.of(["x"])).fit(np.zeros((2, 1)), np.array([label, 1.0]))


def test_constant_price_model_rejects_nonfinite_features() -> None:
    model = PriceModel(FeatureSchema.of(["x"])).fit(np.zeros((2, 1)), np.zeros(2))
    with pytest.raises(ValueError, match="finite"):
        model.predict_proba(np.array([[float("nan")]]))


def _schema() -> FeatureSchema:
    return FeatureSchema.of(["f0", "f1"])


def _separable_three_class(n: int = 90) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    per = n // 3
    down = rng.normal(loc=[-3.0, 0.0], scale=0.3, size=(per, 2))
    unch = rng.normal(loc=[0.0, 0.0], scale=0.3, size=(per, 2))
    up = rng.normal(loc=[3.0, 0.0], scale=0.3, size=(per, 2))
    x = np.vstack([down, unch, up])
    y = np.array([-1] * per + [0] * per + [1] * per)
    return x, y


def test_predict_proba_columns_follow_fixed_class_order() -> None:
    x, y = _separable_three_class()
    model = PriceModel(_schema()).fit(x, y)
    proba = model.predict_proba(x)
    assert proba.shape == (len(y), 3)
    # Rows sum to 1.
    assert np.allclose(proba.sum(axis=1), 1.0)
    # A strongly-down feature row gives highest probability in column 0 (class -1).
    down_proba = model.predict_proba(np.array([[-3.0, 0.0]]))[0]
    assert int(np.argmax(down_proba)) == PRICE_CLASSES.index(-1)
    up_proba = model.predict_proba(np.array([[3.0, 0.0]]))[0]
    assert int(np.argmax(up_proba)) == PRICE_CLASSES.index(1)


def test_missing_class_keeps_three_columns_but_is_probability_zero() -> None:
    # Training with only classes {-1, 1}; the unchanged (0) class is absent. It must remain as a
    # column (three columns always) but, per external review R6, carry probability exactly 0 --
    # an absent class gets a -inf logit and never inherits the zero-logit reference probability.
    # (This test previously asserted the buggy behaviour: that the absent unchanged class got a
    # nonzero reference probability > 0. That tie is exactly the defect R6 removes.)
    rng = np.random.default_rng(1)
    down = rng.normal(loc=[-3.0, 0.0], scale=0.3, size=(40, 2))
    up = rng.normal(loc=[3.0, 0.0], scale=0.3, size=(40, 2))
    x = np.vstack([down, up])
    y = np.array([-1] * 40 + [1] * 40)
    model = PriceModel(_schema()).fit(x, y)
    proba = model.predict_proba(x)
    # Still three columns; probabilities finite and rows sum to 1.
    assert proba.shape == (80, 3)
    unchanged_col = PRICE_CLASSES.index(0)
    assert np.all(np.isfinite(proba[:, unchanged_col]))
    assert np.allclose(proba.sum(axis=1), 1.0)
    # The absent unchanged class is exactly 0 everywhere (never the reference-class tie).
    assert np.all(proba[:, unchanged_col] == 0.0)
    # The two fitted classes carry all the mass.
    assert np.allclose(proba[:, PRICE_CLASSES.index(-1)] + proba[:, PRICE_CLASSES.index(1)], 1.0)


def test_up_minus_down_matches_proba_columns() -> None:
    x, y = _separable_three_class()
    model = PriceModel(_schema()).fit(x, y)
    proba = model.predict_proba(x)
    umd = model.up_minus_down(x)
    expected = proba[:, PRICE_CLASSES.index(1)] - proba[:, PRICE_CLASSES.index(-1)]
    assert np.allclose(umd, expected)


def test_predictions_are_deterministic() -> None:
    x, y = _separable_three_class()
    p1 = PriceModel(_schema()).fit(x, y).predict_proba(x)
    p2 = PriceModel(_schema()).fit(x, y).predict_proba(x)
    assert np.array_equal(p1, p2)


def test_c_selection_by_validation_log_loss() -> None:
    x, y = _separable_three_class(n=90)
    xv, yv = _separable_three_class(n=90)
    model = PriceModel(_schema(), c_grid=(0.001, 1.0, 100.0)).fit(x, y, xv, yv)
    assert model.chosen_c in (0.001, 1.0, 100.0)


def test_single_class_training_is_degenerate_but_three_columns() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(size=(30, 2))
    y = np.zeros(30, dtype=int)  # only unchanged class
    model = PriceModel(_schema()).fit(x, y)
    proba = model.predict_proba(x)
    assert proba.shape == (30, 3)
    # The sole class dominates.
    assert np.all(np.argmax(proba, axis=1) == PRICE_CLASSES.index(0))
