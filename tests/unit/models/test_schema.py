"""T52: reordered/missing/extra model feature columns are a hard schema error, plus SupportRule
behavior.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from qexec.models.schema import FeatureSchema, SchemaMismatchError, SupportRule


def test_schema_accepts_exact_match() -> None:
    schema = FeatureSchema.of(["a", "b", "c"])
    schema.validate(["a", "b", "c"])
    schema.validate(pl.DataFrame({"a": [1.0], "b": [2.0], "c": [3.0]}))


def test_schema_rejects_missing_column() -> None:
    schema = FeatureSchema.of(["a", "b", "c"])
    with pytest.raises(SchemaMismatchError, match="missing="):
        schema.validate(["a", "b"])


def test_schema_rejects_extra_column() -> None:
    schema = FeatureSchema.of(["a", "b"])
    with pytest.raises(SchemaMismatchError, match="extra="):
        schema.validate(["a", "b", "c"])


def test_schema_rejects_reordered_columns() -> None:
    schema = FeatureSchema.of(["a", "b", "c"])
    with pytest.raises(SchemaMismatchError, match="order mismatch"):
        schema.validate(["a", "c", "b"])


def test_schema_matrix_is_in_schema_order() -> None:
    schema = FeatureSchema.of(["a", "b"])
    frame = pl.DataFrame({"a": [1.0, 3.0], "b": [2.0, 4.0]})
    mat = schema.matrix(frame)
    assert mat.shape == (2, 2)
    assert mat[0, 0] == 1.0 and mat[0, 1] == 2.0


def test_schema_rejects_duplicates_and_empty() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        FeatureSchema.of(["a", "a"])
    with pytest.raises(ValueError, match="at least one"):
        FeatureSchema.of([])


def test_support_rule_within_and_outside_range() -> None:
    schema = FeatureSchema.of(["x", "y"])
    x_train = np.array([[0.0, 10.0], [2.0, 20.0], [4.0, 30.0]])
    rule = SupportRule.from_training(schema, x_train, min_training_rows=2, margin=0.5)
    # Training range x in [0,4], y in [10,30], margin 0.5.
    assert rule.is_supported({"x": 2.0, "y": 20.0})
    assert rule.is_supported({"x": 4.5, "y": 30.5})  # exactly on the margin boundary (<=)
    assert not rule.is_supported({"x": 4.6, "y": 20.0})  # beyond margin on x
    assert not rule.is_supported({"x": 2.0, "y": 9.4})  # below margin on y


def test_support_rule_rejects_nonfinite_and_insufficient_rows() -> None:
    schema = FeatureSchema.of(["x"])
    x_train = np.array([[1.0], [2.0]])
    rule = SupportRule.from_training(schema, x_train, min_training_rows=5, margin=0.0)
    # Only 2 training rows but minimum is 5 -> never supported.
    assert not rule.has_min_rows
    assert not rule.is_supported({"x": 1.5})
    # With enough rows, nonfinite query is unsupported.
    rule2 = SupportRule.from_training(schema, x_train, min_training_rows=1, margin=0.0)
    assert not rule2.is_supported({"x": float("nan")})
    assert not rule2.is_supported({"x": float("inf")})


def test_support_rule_accepts_vector_and_array_inputs() -> None:
    schema = FeatureSchema.of(["x", "y"])
    rule = SupportRule.from_training(
        schema, np.array([[0.0, 0.0], [1.0, 1.0]]), min_training_rows=1, margin=0.0
    )
    assert rule.is_supported([0.5, 0.5])
    assert rule.is_supported(np.array([1.0, 1.0]))
    assert not rule.is_supported([2.0, 0.5])


def test_support_rule_missing_feature_in_mapping_is_error() -> None:
    schema = FeatureSchema.of(["x", "y"])
    rule = SupportRule.from_training(
        schema, np.array([[0.0, 0.0], [1.0, 1.0]]), min_training_rows=1, margin=0.0
    )
    with pytest.raises(SchemaMismatchError):
        rule.is_supported({"x": 0.5})
