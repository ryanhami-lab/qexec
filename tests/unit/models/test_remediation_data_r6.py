"""R6 reproduction: an absent price class must get probability exactly 0, never the
reference-class (zero-logit) tie.

External review R6: ``PriceModel`` gave an absent class the reference-class logit, so a binary
DOWN/UNCH fit predicted ``P(up) ~= P(down)`` instead of ``P(up) == 0``. The fix makes absent
classes carry logit ``-inf`` (probability exactly 0) while the fitted classes reproduce
scikit-learn's ``predict_proba`` exactly. The artifact round trip must still be exact with the
``-inf`` encoded safely in JSON.
"""

from __future__ import annotations

import json

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from qexec.core.tasks import CheckpointChoice
from qexec.models.action import ActionModels
from qexec.models.artifacts import ModelArtifact
from qexec.models.price import PRICE_CLASSES, PriceModel
from qexec.models.schema import FeatureSchema


def _schema() -> FeatureSchema:
    return FeatureSchema.of(["f0", "f1"])


def _down_unch_data() -> tuple[np.ndarray, np.ndarray]:
    """90 DOWN rows and 10 UNCH rows; the UP class is absent from training."""
    rng = np.random.default_rng(0)
    down = rng.normal(loc=[-3.0, 0.0], scale=0.3, size=(90, 2))
    unch = rng.normal(loc=[0.0, 0.0], scale=0.3, size=(10, 2))
    x = np.vstack([down, unch])
    y = np.array([-1] * 90 + [0] * 10)
    return x, y


def _sklearn_binary_proba(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, list[int]]:
    """Reference sklearn probabilities for the two present classes (same pipeline as PriceModel)."""
    scaler = StandardScaler().fit(x)
    xs = scaler.transform(x)
    clf = LogisticRegression(C=0.01, solver="lbfgs", max_iter=1000, random_state=0)
    clf.fit(xs, y)
    return clf.predict_proba(xs), [int(c) for c in clf.classes_]


def test_absent_up_class_has_probability_exactly_zero() -> None:
    x, y = _down_unch_data()
    model = PriceModel(_schema()).fit(x, y)
    proba = model.predict_proba(x)
    up_col = PRICE_CLASSES.index(1)
    # The absent UP class must be exactly 0 everywhere -- not the reference-class tie.
    assert np.all(proba[:, up_col] == 0.0)
    # Rows still sum to 1.
    assert np.allclose(proba.sum(axis=1), 1.0)


def test_fitted_classes_match_sklearn_predict_proba_exactly() -> None:
    x, y = _down_unch_data()
    model = PriceModel(_schema()).fit(x, y)
    proba = model.predict_proba(x)
    sk_proba, sk_classes = _sklearn_binary_proba(x, y)
    down_col = PRICE_CLASSES.index(-1)
    unch_col = PRICE_CLASSES.index(0)
    sk_down = sk_proba[:, sk_classes.index(-1)]
    sk_unch = sk_proba[:, sk_classes.index(0)]
    assert np.allclose(proba[:, down_col], sk_down, atol=1e-12)
    assert np.allclose(proba[:, unch_col], sk_unch, atol=1e-12)


def test_up_minus_down_reflects_zero_up() -> None:
    # With UP absent, up_minus_down == -P(down) exactly (B2's u_signal is no longer forced to 0).
    x, y = _down_unch_data()
    model = PriceModel(_schema()).fit(x, y)
    proba = model.predict_proba(x)
    umd = model.up_minus_down(x)
    expected = -proba[:, PRICE_CLASSES.index(-1)]
    assert np.allclose(umd, expected)
    # u_signal is genuinely nonzero (not clamped to 0) for a 2-class fit.
    assert float(np.abs(umd).max()) > 0.0


def test_single_class_semantics_unchanged() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(size=(30, 2))
    y = np.zeros(30, dtype=int)  # only the UNCHANGED class present
    model = PriceModel(_schema()).fit(x, y)
    proba = model.predict_proba(x)
    unch_col = PRICE_CLASSES.index(0)
    # The sole present class carries ~all probability; the two absent classes are exactly 0.
    assert np.allclose(proba[:, unch_col], 1.0)
    assert np.all(proba[:, PRICE_CLASSES.index(-1)] == 0.0)
    assert np.all(proba[:, PRICE_CLASSES.index(1)] == 0.0)


def test_artifact_round_trip_exact_with_minus_inf(tmp_path) -> None:  # type: ignore[no-untyped-def]
    x, y = _down_unch_data()
    model = PriceModel(_schema()).fit(x, y)
    before = model.predict_proba(x)

    art = ModelArtifact.create(
        model_kind="price",
        schema=_schema(),
        training_sessions=["s0"],
        preprocessing="standard-scaler-on-train",
        hyperparameters={"chosen_c": model.chosen_c},
        seed=0,
        state=model.to_state(),
    )
    art.save(tmp_path / "price_model")

    # The JSON sidecar must be loadable by a strict JSON parser (no bare -Infinity token).
    json_text = (tmp_path / "price_model.json").read_text()
    json.loads(json_text)  # strict: raises if -Infinity leaked in as a bare token

    loaded = ModelArtifact.load(tmp_path / "price_model", expected_schema=_schema())
    reloaded = PriceModel.from_state(loaded.state)
    after = reloaded.predict_proba(x)
    assert np.array_equal(before, after)  # bit-for-bit identical, UP column still exactly 0
    assert np.all(after[:, PRICE_CLASSES.index(1)] == 0.0)


def test_action_artifact_still_round_trips(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Guard: the JSON -inf encoding must not disturb unrelated (action) artifacts.
    rng = np.random.default_rng(1)
    x = rng.normal(size=(80, 2))
    miss = (x[:, 0] > 0).astype(float)
    cost = 2.0 * x[:, 1]
    model = ActionModels(_schema())
    model.fit_action(CheckpointChoice.HOLD, x, miss, cost)
    model.fit_action(CheckpointChoice.SWITCH, x, 1.0 - miss, cost + 1.0)
    query = np.array([0.4, -0.1])
    before = model.predict(query)
    art = ModelArtifact.create(
        model_kind="action",
        schema=_schema(),
        training_sessions=["s0"],
        preprocessing="standard-scaler-on-train",
        hyperparameters={},
        seed=0,
        state=model.to_state(),
    )
    art.save(tmp_path / "action_model")
    loaded = ModelArtifact.load(tmp_path / "action_model", expected_schema=_schema())
    after = ActionModels.from_state(loaded.state).predict(query)
    for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
        assert before[action].p_hat == after[action].p_hat
        assert before[action].v_hat == after[action].v_hat
