"""ModelArtifact round-trip: a loaded model reproduces predictions exactly; schema mismatch on
load is a hard error. Serialization is pickle-free (JSON + npz).
"""

from __future__ import annotations

import numpy as np
import pytest

from qexec.core.tasks import CheckpointChoice
from qexec.models.action import ActionModels
from qexec.models.artifacts import ModelArtifact
from qexec.models.price import PriceModel
from qexec.models.schema import FeatureSchema, SchemaMismatchError


def _schema() -> FeatureSchema:
    return FeatureSchema.of(["f0", "f1"])


def _price_data() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    down = rng.normal(loc=[-3.0, 0.0], scale=0.3, size=(30, 2))
    unch = rng.normal(loc=[0.0, 0.0], scale=0.3, size=(30, 2))
    up = rng.normal(loc=[3.0, 0.0], scale=0.3, size=(30, 2))
    x = np.vstack([down, unch, up])
    y = np.array([-1] * 30 + [0] * 30 + [1] * 30)
    return x, y


def test_price_artifact_round_trip_is_exact(tmp_path) -> None:  # type: ignore[no-untyped-def]
    x, y = _price_data()
    model = PriceModel(_schema()).fit(x, y)
    before = model.predict_proba(x)

    art = ModelArtifact.create(
        model_kind="price",
        schema=_schema(),
        training_sessions=["s0", "s1"],
        preprocessing="standard-scaler-on-train",
        hyperparameters={"c_grid": [0.01, 0.1, 1.0, 10.0], "chosen_c": model.chosen_c},
        seed=0,
        state=model.to_state(),
    )
    art.save(tmp_path / "price_model")
    loaded = ModelArtifact.load(tmp_path / "price_model", expected_schema=_schema())

    assert loaded.model_kind == "price"
    assert loaded.training_sessions == ("s0", "s1")
    assert loaded.sklearn_version == art.sklearn_version
    assert loaded.numpy_version == art.numpy_version

    reloaded_model = PriceModel.from_state(loaded.state)
    after = reloaded_model.predict_proba(x)
    assert np.array_equal(before, after)  # bit-for-bit identical


def test_action_artifact_round_trip_is_exact(tmp_path) -> None:  # type: ignore[no-untyped-def]
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
        hyperparameters={"alpha_grid": [0.1, 1.0, 10.0, 100.0]},
        seed=0,
        state=model.to_state(),
    )
    art.save(tmp_path / "action_model")
    loaded = ModelArtifact.load(tmp_path / "action_model", expected_schema=_schema())
    reloaded_model = ActionModels.from_state(loaded.state)
    after = reloaded_model.predict(query)
    for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
        assert before[action].p_hat == after[action].p_hat
        assert before[action].v_hat == after[action].v_hat


def test_load_with_mismatched_schema_is_hard_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    x, y = _price_data()
    model = PriceModel(_schema()).fit(x, y)
    art = ModelArtifact.create(
        model_kind="price",
        schema=_schema(),
        training_sessions=["s0"],
        preprocessing="standard-scaler-on-train",
        hyperparameters={},
        seed=0,
        state=model.to_state(),
    )
    art.save(tmp_path / "m")
    # Reordered schema at load -> hard error.
    with pytest.raises(SchemaMismatchError):
        ModelArtifact.load(tmp_path / "m", expected_schema=FeatureSchema.of(["f1", "f0"]))
    # Missing column -> hard error.
    with pytest.raises(SchemaMismatchError):
        ModelArtifact.load(tmp_path / "m", expected_schema=FeatureSchema.of(["f0"]))


def test_save_is_pickle_free(tmp_path) -> None:  # type: ignore[no-untyped-def]
    x, y = _price_data()
    model = PriceModel(_schema()).fit(x, y)
    art = ModelArtifact.create(
        model_kind="price",
        schema=_schema(),
        training_sessions=["s0"],
        preprocessing="standard-scaler-on-train",
        hyperparameters={},
        seed=0,
        state=model.to_state(),
    )
    art.save(tmp_path / "m")
    # JSON is human-readable text with no pickle opcodes; npz is a zip of .npy arrays.
    json_text = (tmp_path / "m.json").read_text()
    assert '"model_kind": "price"' in json_text
    assert (tmp_path / "m.npz").exists()
