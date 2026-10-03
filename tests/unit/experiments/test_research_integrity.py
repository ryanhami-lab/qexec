"""Hand-derived regressions for research populations, fits, and diagnostics."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from qexec.core.tasks import CheckpointChoice
from qexec.experiments.config import StudyConfig
from qexec.experiments.pipeline import (
    _action_diagnostics,
    _calibration_by_action,
    _DevelopmentData,
    _evaluable_rows,
    _fit_action_arm,
    _fit_price_on_sessions,
    _run_price_models,
    _Split,
)
from qexec.features.groups import MARKET_FEATURES
from qexec.models.action import ActionModels
from qexec.models.schema import FeatureSchema


def _branch(sid: str, value: float, label: int | None, *, bad: bool = False) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "session_id": sid,
                "task_id": f"{sid}:BUY",
                "action": action,
                "technically_unevaluable": bad,
                "price_label": label,
                "price_label_100ms": label,
                "miss": 0,
                "c_t_ticks": 2.0,
                **{f"feat_{name}": value for name in MARKET_FEATURES},
            }
            for action in ("HOLD", "SWITCH")
        ],
        schema_overrides={"price_label": pl.Int64, "price_label_100ms": pl.Int64},
    )


def test_price_fit_excludes_technical_rows_before_fit_and_tuning() -> None:
    schema = FeatureSchema.of(list(MARKET_FEATURES))
    bad = _branch("bad", 1000.0, 1, bad=True)
    assert _fit_price_on_sessions(schema, {"bad": bad}, ["bad"], label_col="price_label") is None
    rows = {"good": _branch("good", 2.0, -1), "bad": bad}
    model = _fit_price_on_sessions(schema, rows, list(rows), label_col="price_label")
    assert model is not None
    assert np.array_equal(model.to_state()["scaler_mean"], np.full(len(MARKET_FEATURES), 2.0))
    assert model.predict_proba(np.zeros((1, len(MARKET_FEATURES)))).tolist() == [[1.0, 0.0, 0.0]]


def test_price_selects_c_then_refits_all_eligible_training_sessions() -> None:
    schema = FeatureSchema.of(list(MARKET_FEATURES))
    rows = {"early": _branch("early", 0.0, -1), "last": _branch("last", 10.0, 1)}
    model = _fit_price_on_sessions(schema, rows, list(rows), label_col="price_label")
    assert model is not None
    # Exactly one checkpoint per session: full refit mean is (0+10)/2 = 5, not 0.
    assert np.array_equal(model.to_state()["scaler_mean"], np.full(len(MARKET_FEATURES), 5.0))
    assert model.predict_proba(np.full((1, len(MARKET_FEATURES)), 10.0))[0, 2] > 0.0


def _development() -> tuple[_Split, _DevelopmentData]:
    split = _Split(
        train=("early", "invalid", "last"),
        validation=("val",),
        test=("test",),
        session_dirs={sid: Path(sid) for sid in ("early", "invalid", "last", "val", "test")},
    )
    dev = _DevelopmentData(
        branch_labels={
            "early": _branch("early", 0.0, -1),
            "invalid": _branch("invalid", 1000.0, 1, bad=True),
            "last": _branch("last", 10.0, 1),
            "val": pl.concat([_branch("val", 3.0, 0), _branch("badval", 1000.0, 1, bad=True)]),
        },
        n_price_label_none=0,
        primary_tau_ns=100,
    )
    return split, dev


def test_price_provenance_reports_actual_fits_and_quality_exclusions() -> None:
    split, dev = _development()
    price = _run_price_models(StudyConfig.quick_preset(), split, dev)
    assert price.train_index_log["last"] == ["early"]
    assert "invalid" not in price.oof_signal_by_session
    log = price.fit_provenance["primary"]
    assert log["training_sessions"] == ["early", "last"]
    assert log["tuning_train_sessions"] == ["early"]
    assert log["tuning_validation_sessions"] == ["last"]
    assert log["n_training_rows"] == 2
    assert log["per_session"]["invalid"]["n_technical_excluded"] == 1
    assert price.primary_log_loss is not None


def _action_rows() -> tuple[FeatureSchema, pl.DataFrame, pl.DataFrame]:
    schema = FeatureSchema.of([MARKET_FEATURES[0]])
    train = pl.DataFrame(
        [
            {"action": a, "feat_spread_ticks": 0.0, "miss": m, "c_t_ticks": c}
            for a in ("HOLD", "SWITCH")
            for m, c in ((0, 4.0), (1, 6.0))
        ]
    )
    val = pl.DataFrame(
        [
            {
                "action": a,
                "feat_spread_ticks": 0.0,
                "miss": m,
                "c_t_ticks": c,
                "technically_unevaluable": bad,
            }
            for a in ("HOLD", "SWITCH")
            for m, c, bad in ((0, 2.0, False), (1, None, False), (1, 100.0, True))
        ]
    )
    return schema, train, val


def test_calibration_keeps_null_cost_misses_and_uses_training_baselines() -> None:
    schema, train, val = _action_rows()
    models, _ = _fit_action_arm(schema, train, train, min_rows=1, margin_frac=0.5)
    metrics = _calibration_by_action(val, schema, models, train)
    for action in ("HOLD", "SWITCH"):
        block = metrics[action]
        assert block["n_rows"] == 2
        assert block["n_cost_rows"] == 1
        assert block["n_technical_excluded"] == 1
        assert block["n_cost_missing"] == 1
        assert block["miss_brier"] == pytest.approx(0.25)
        # Training costs are 4,6: baseline 5; validation cost 2 gives RMSE 3 (not zero).
        assert block["cost_constant_mean_rmse"] == pytest.approx(3.0)
        assert block["miss_base_rate_log_loss"] == pytest.approx(np.log(2))


def test_action_diagnostics_preserves_both_actions_and_excludes_invalid_tasks() -> None:
    split, dev = _development()
    price = _run_price_models(StudyConfig.quick_preset(), split, dev)
    schema = FeatureSchema.of([MARKET_FEATURES[0]])
    train = pl.concat([dev.branch_labels["early"], dev.branch_labels["last"]])
    models, support = _fit_action_arm(schema, train, train, min_rows=1, margin_frac=0.5)
    diag = _action_diagnostics(
        split,
        dev,
        price,
        b3_schema=schema,
        b3_nq_schema=schema,
        b3_models=models,
        b3_nq_models=models,
        b3_support=support,
        b3_nq_support=support,
        epsilon=0.001,
    )
    assert diag["n_validation_rows"] == 2
    assert diag["n_validation_technical_excluded"] == 2
    for variant in ("b3", "b3_no_queue"):
        for action in ("HOLD", "SWITCH"):
            assert diag["calibration"][variant][action]["n_rows"] == 1


def test_action_models_fit_distinct_miss_and_cost_populations() -> None:
    schema = FeatureSchema.of(["x"])
    models = ActionModels(schema)
    for action in CheckpointChoice:
        models.fit_action(
            action,
            np.zeros((3, 1)),
            np.array([0.0, 1.0, 1.0]),
            np.array([4.0, 6.0]),
            x_cost_train=np.zeros((2, 1)),
        )
        assert models.miss_n(action) == 3
    pred = models.predict(np.array([0.0]))[CheckpointChoice.HOLD]
    assert pred.p_hat == pytest.approx(2 / 3, abs=1e-4)
    assert pred.v_hat == pytest.approx(5.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"study_id": "  "},
        {"n_sessions": 3, "n_pilot_sessions": 1},
        {"split_fractions": (0.1, 0.4, 0.5)},
        {"split_fractions": (float("nan"), 0.2, 0.8)},
        {"epsilon_sensitivity": (0.0, float("nan"))},
        {"epsilon_sensitivity": (-0.1, 0.1)},
        {"epsilon_sensitivity": (0.0, 0.0)},
        {"scenarios": ("L1", "L1")},
        {"horizon_ladder_ns": (1_000_000_000, 1_000_000_000)},
        {"horizon_ladder_ns": (2.5,)},
        {"n_sessions": 4.5},
        {"duration_s": True},
        {"base_seed": -1},
    ],
)
def test_study_rejects_invalid_research_inputs(overrides: dict[str, object]) -> None:
    raw = StudyConfig(study_id="valid").to_dict()
    raw.update(overrides)
    with pytest.raises(ValueError):
        StudyConfig.from_dict(raw)


def test_no_evaluable_cost_rows_fail_with_an_explicit_population_error() -> None:
    schema, train, _ = _action_rows()
    train = train.with_columns(pl.lit(None, dtype=pl.Float64).alias("c_t_ticks"))
    with pytest.raises(ValueError, match=r"HOLD.*cost.*rows"):
        _fit_action_arm(schema, train, train, min_rows=1, margin_frac=0.5)


def test_miss_reference_is_training_rate_when_validation_contains_only_misses() -> None:
    schema, train, val = _action_rows()
    models, _ = _fit_action_arm(schema, train, train, min_rows=1, margin_frac=0.5)
    val = val.filter(pl.col("c_t_ticks").is_null())
    for block in _calibration_by_action(val, schema, models, train).values():
        assert block["n_rows"] == 1
        assert block["n_cost_rows"] == 0
        assert block["miss_base_rate_log_loss"] == pytest.approx(np.log(2))
        assert block["miss_base_rate_brier"] == pytest.approx(0.25)
        assert block["cost_rmse"] is None


def test_a_technical_flag_on_either_action_excludes_the_whole_task() -> None:
    frame = _branch("one", 0.0, 0).with_columns(
        (pl.col("action") == "SWITCH").alias("technically_unevaluable")
    )
    assert _evaluable_rows(frame).height == 0


def test_support_rejects_a_separately_rescored_frame() -> None:
    schema, train, _ = _action_rows()
    rescored = train.with_columns(pl.lit(100.0).alias("feat_spread_ticks"))
    with pytest.raises(ValueError, match="exact action-training frame"):
        _fit_action_arm(schema, train, rescored, min_rows=1, margin_frac=0.5)


def test_validation_labels_cannot_change_fitted_price_models() -> None:
    split, dev = _development()
    first = _run_price_models(StudyConfig.quick_preset(), split, dev)
    altered = dict(dev.branch_labels)
    altered["val"] = _branch("val", 10000.0, 1)
    second = _run_price_models(
        StudyConfig.quick_preset(),
        split,
        _DevelopmentData(altered, dev.n_price_label_none, dev.primary_tau_ns),
    )
    x = np.zeros((2, len(MARKET_FEATURES)))
    assert np.array_equal(
        first.primary_model.predict_proba(x), second.primary_model.predict_proba(x)
    )
    assert np.array_equal(
        first.secondary_model.predict_proba(x), second.secondary_model.predict_proba(x)
    )
    assert first.fit_provenance == second.fit_provenance


def test_empty_validation_is_undefined_and_counted() -> None:
    schema, train, _ = _action_rows()
    models, _ = _fit_action_arm(schema, train, train, min_rows=1, margin_frac=0.5)
    for block in _calibration_by_action(pl.DataFrame(), schema, models, train).values():
        assert block["n_rows"] == 0
        assert block["miss_brier"] is None
        assert block["cost_rmse"] is None


@pytest.mark.parametrize("miss", [np.array([0.0, 0.5]), np.array([0.0, np.nan])])
def test_action_labels_cannot_silently_truncate_or_impute(miss: np.ndarray) -> None:
    model = ActionModels(FeatureSchema.of(["x"]))
    with pytest.raises(ValueError):
        model.fit_action(CheckpointChoice.HOLD, np.zeros((2, 1)), miss, np.zeros(2))
