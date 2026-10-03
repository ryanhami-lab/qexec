"""Feature schema and frozen numeric support rule (architecture 10.3).

``FeatureSchema`` pins the exact ordered feature names a model consumes. Any missing,
extra, or reordered column is a hard error (``SchemaMismatchError``) — never a silently
different prediction (T52). ``SupportRule`` holds frozen numeric thresholds learned on
training data (minimum rows per action and per-feature training range with an absolute
margin); ``is_supported`` returns ``False`` when a feature value falls outside the
training range by more than its margin. Support decisions depend only on current client
features and frozen thresholds, never on outcomes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import polars as pl


class SchemaMismatchError(ValueError):
    """Raised when presented feature columns do not match the frozen schema exactly."""


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    """An ordered, immutable list of feature names a model was fitted on.

    The order is significant: a model's coefficient vector is positional, so a reordered
    frame would silently produce different predictions. ``validate`` therefore rejects
    missing columns, extra columns, and reordering alike.
    """

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.names) == 0:
            raise ValueError("FeatureSchema must contain at least one feature name")
        if len(set(self.names)) != len(self.names):
            raise ValueError(f"FeatureSchema contains duplicate names: {self.names!r}")

    @classmethod
    def of(cls, names: Sequence[str]) -> FeatureSchema:
        return cls(tuple(names))

    @property
    def n_features(self) -> int:
        return len(self.names)

    def _columns_of(self, frame_or_columns: pl.DataFrame | Sequence[str]) -> list[str]:
        if isinstance(frame_or_columns, pl.DataFrame):
            return list(frame_or_columns.columns)
        if isinstance(frame_or_columns, str):
            raise TypeError("expected a polars DataFrame or a sequence of column names")
        return list(frame_or_columns)

    def validate(self, frame_or_columns: pl.DataFrame | Sequence[str]) -> None:
        """Raise ``SchemaMismatchError`` unless columns equal ``names`` in the same order.

        Accepts a polars ``DataFrame`` (uses its ``columns``) or an explicit sequence of
        column names.
        """
        presented = self._columns_of(frame_or_columns)
        expected = list(self.names)
        if presented == expected:
            return
        presented_set, expected_set = set(presented), set(expected)
        missing = [name for name in expected if name not in presented_set]
        extra = [name for name in presented if name not in expected_set]
        if missing or extra:
            raise SchemaMismatchError(
                f"feature schema mismatch: missing={missing!r} extra={extra!r} "
                f"(expected {expected!r}, got {presented!r})"
            )
        # Same set, different order: reordering is a mismatch because coefficients are positional.
        raise SchemaMismatchError(
            f"feature schema column order mismatch: expected {expected!r}, got {presented!r}"
        )

    def matrix(self, frame: pl.DataFrame) -> np.ndarray:
        """Validate ``frame`` then return its feature columns as a float64 ``ndarray``.

        The returned matrix has columns in schema order, so it aligns with fitted
        coefficients positionally.
        """
        self.validate(frame)
        return frame.select(self.names).to_numpy().astype(np.float64, copy=False)


@dataclass(frozen=True, slots=True)
class SupportRule:
    """Frozen numeric support thresholds for one model (architecture 10.3).

    Attributes:
        schema: the feature schema these thresholds correspond to (positional).
        feature_min: per-feature minimum observed in training, schema order.
        feature_max: per-feature maximum observed in training, schema order.
        feature_margin: per-feature absolute margin added to the training range; a value
            is unsupported only if it lies more than this margin outside ``[min, max]``.
        min_training_rows: minimum number of training rows required for the model to be
            considered supported at all (per-action row-count floor).
        n_training_rows: the number of rows used to fit feature ranges (frozen record).
        n_min_fit_rows: optional minimum among action-specific miss/cost fit populations.
    """

    schema: FeatureSchema
    feature_min: tuple[float, ...]
    feature_max: tuple[float, ...]
    feature_margin: tuple[float, ...]
    min_training_rows: int
    n_training_rows: int
    n_min_fit_rows: int | None = None

    def __post_init__(self) -> None:
        n = self.schema.n_features
        for field_name in ("feature_min", "feature_max", "feature_margin"):
            value = getattr(self, field_name)
            if len(value) != n:
                raise ValueError(f"{field_name} length {len(value)} != schema features {n}")
        for lo, hi in zip(self.feature_min, self.feature_max, strict=True):
            if lo > hi:
                raise ValueError(f"feature_min {lo} exceeds feature_max {hi}")
        for m in self.feature_margin:
            if m < 0.0 or not np.isfinite(m):
                raise ValueError(f"feature_margin must be finite and nonnegative, got {m!r}")
        if self.min_training_rows < 0:
            raise ValueError("min_training_rows must be nonnegative")
        if self.n_training_rows < 0:
            raise ValueError("n_training_rows must be nonnegative")
        if self.n_min_fit_rows is not None and self.n_min_fit_rows < 0:
            raise ValueError("n_min_fit_rows must be nonnegative")

    @property
    def has_min_rows(self) -> bool:
        """Whether the training row count met the frozen minimum."""
        count = self.n_training_rows if self.n_min_fit_rows is None else self.n_min_fit_rows
        return count >= self.min_training_rows

    @classmethod
    def from_training(
        cls,
        schema: FeatureSchema,
        x_train: np.ndarray,
        *,
        min_training_rows: int,
        margin: float | Sequence[float] = 0.0,
    ) -> SupportRule:
        """Derive thresholds from a training matrix (columns in schema order).

        ``margin`` may be a scalar (applied to every feature) or a per-feature sequence.
        """
        x = np.asarray(x_train, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != schema.n_features:
            raise ValueError(f"x_train must be (n, {schema.n_features}); got shape {x.shape}")
        n_rows = int(x.shape[0])
        if n_rows == 0:
            mins = tuple(0.0 for _ in range(schema.n_features))
            maxs = tuple(0.0 for _ in range(schema.n_features))
        else:
            if not np.all(np.isfinite(x)):
                raise ValueError("x_train contains nonfinite values")
            mins = tuple(float(v) for v in x.min(axis=0))
            maxs = tuple(float(v) for v in x.max(axis=0))
        if isinstance(margin, int | float):
            margins = tuple(float(margin) for _ in range(schema.n_features))
        else:
            margins = tuple(float(m) for m in margin)
        return cls(
            schema=schema,
            feature_min=mins,
            feature_max=maxs,
            feature_margin=margins,
            min_training_rows=min_training_rows,
            n_training_rows=n_rows,
        )

    def is_supported(self, x: Mapping[str, float] | Sequence[float] | np.ndarray) -> bool:
        """Whether a single feature vector lies within the frozen support region.

        A value is unsupported if the model lacked the minimum training rows, if any
        feature is nonfinite, or if any feature falls more than its margin outside the
        training range.
        """
        if not self.has_min_rows:
            return False
        values = self._vector(x)
        for value, lo, hi, margin in zip(
            values, self.feature_min, self.feature_max, self.feature_margin, strict=True
        ):
            if not np.isfinite(value):
                return False
            if value < lo - margin or value > hi + margin:
                return False
        return True

    def _vector(self, x: Mapping[str, float] | Sequence[float] | np.ndarray) -> list[float]:
        if isinstance(x, Mapping):
            missing = [name for name in self.schema.names if name not in x]
            if missing:
                raise SchemaMismatchError(f"support check missing features: {missing!r}")
            return [float(x[name]) for name in self.schema.names]
        arr = np.asarray(list(x) if not isinstance(x, np.ndarray) else x, dtype=np.float64)
        if arr.ndim != 1 or arr.shape[0] != self.schema.n_features:
            raise ValueError(
                f"support vector must have {self.schema.n_features} values; got {arr.shape}"
            )
        return [float(v) for v in arr]
