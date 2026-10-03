"""Interpretable multinomial logistic price model (product section 8, 6.3).

Classes are the fixed ordered triple ``(-1 down, 0 unchanged, 1 up)``. The "unchanged"
class is never silently dropped even when absent from training, but it is also never allowed
to tie a fitted class: scikit-learn's ``LogisticRegression`` only learns coefficients for the
classes it sees, so each class absent from training is reintroduced here with an explicit
``-inf`` logit (zero coefficient row, ``-inf`` intercept). After the fixed-order softmax that
assigns the absent class probability **exactly 0** while the fitted classes reproduce
scikit-learn's ``predict_proba`` bit-for-bit (external review R6). This keeps every class
column present (the frame always has three columns) without the old behaviour where an absent
class inherited the zero-logit reference probability and tied a fitted class.

The ``-inf`` intercepts are serialized through the artifact's NumPy ``.npz`` sidecar (not the
JSON sidecar), so a strict JSON parser never sees a bare ``-Infinity`` token and the round
trip is still exact.

Regularization strength ``C`` is chosen from a fixed grid by chronological validation
log-loss; the ``StandardScaler`` is fitted on training rows only. Everything is deterministic
(fixed ``random_state``, ``lbfgs`` solver).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from qexec.models.schema import FeatureSchema

__all__ = ["PRICE_CLASSES", "PriceModel"]

PRICE_CLASSES: tuple[int, int, int] = (-1, 0, 1)
"""Fixed class order: down, unchanged, up. Never reordered, never dropped."""

DEFAULT_C_GRID: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0)
"""Fixed inverse-regularization grid, searched by chronological validation log-loss."""

_RANDOM_STATE = 0


def _softmax_rows(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    result: np.ndarray = exp / exp.sum(axis=1, keepdims=True)
    return result


@dataclass(frozen=True, slots=True)
class _FitParams:
    """Frozen fitted parameters sufficient to reproduce predictions exactly."""

    coef: np.ndarray  # shape (3, n_features), rows in PRICE_CLASSES order
    intercept: np.ndarray  # shape (3,), PRICE_CLASSES order
    scaler_mean: np.ndarray  # shape (n_features,)
    scaler_scale: np.ndarray  # shape (n_features,)
    chosen_c: float


class PriceModel:
    """Multinomial logistic classifier over fixed classes ``(-1, 0, 1)``.

    Fit with :meth:`fit`. If a validation split is supplied, ``C`` is chosen by validation
    log-loss over the grid; otherwise the first grid value is used. Predictions are produced
    by a self-contained scale+linear+softmax path so a reloaded artifact reproduces them
    without re-instantiating scikit-learn estimators.
    """

    def __init__(
        self,
        schema: FeatureSchema,
        c_grid: tuple[float, ...] = DEFAULT_C_GRID,
    ) -> None:
        if len(c_grid) == 0:
            raise ValueError("c_grid must be non-empty")
        self.schema = schema
        self.c_grid = c_grid
        self._params: _FitParams | None = None

    # -- fitting ---------------------------------------------------------------------------

    def fit(
        self,
        x_train: np.ndarray,
        y_train: np.ndarray,
        x_val: np.ndarray | None = None,
        y_val: np.ndarray | None = None,
    ) -> PriceModel:
        """Fit on training rows, selecting ``C`` by validation log-loss when provided.

        ``y`` values must be drawn from ``PRICE_CLASSES``. A class absent from training is
        handled by zero coefficients (see module docstring); it is never dropped.
        """
        x_train = self._check_matrix(x_train, "x_train")
        y_train = self._check_labels(y_train, x_train.shape[0], "y_train")

        scaler = StandardScaler().fit(x_train)
        xs_train = scaler.transform(x_train)

        if x_val is not None and y_val is not None:
            x_val_c = self._check_matrix(x_val, "x_val")
            y_val_c = self._check_labels(y_val, x_val_c.shape[0], "y_val")
            xs_val = scaler.transform(x_val_c)
            chosen_c = self._select_c(xs_train, y_train, xs_val, y_val_c)
        elif (x_val is None) != (y_val is None):
            raise ValueError("x_val and y_val must be provided together")
        else:
            chosen_c = self.c_grid[0]

        coef, intercept = self._fit_full(xs_train, y_train, chosen_c)
        self._params = _FitParams(
            coef=coef,
            intercept=intercept,
            scaler_mean=np.asarray(scaler.mean_, dtype=np.float64),
            scaler_scale=np.asarray(scaler.scale_, dtype=np.float64),
            chosen_c=chosen_c,
        )
        return self

    def _select_c(
        self, xs_train: np.ndarray, y_train: np.ndarray, xs_val: np.ndarray, y_val: np.ndarray
    ) -> float:
        best_c = self.c_grid[0]
        best_loss = float("inf")
        for c in self.c_grid:
            coef, intercept = self._fit_full(xs_train, y_train, c)
            proba = self._proba_from(xs_val, coef, intercept)
            loss = self._log_loss(proba, y_val)
            # Strict improvement keeps the first (smallest-C, most-regularized) on ties.
            if loss < best_loss:
                best_loss = loss
                best_c = c
        return best_c

    def _fit_full(self, xs: np.ndarray, y: np.ndarray, c: float) -> tuple[np.ndarray, np.ndarray]:
        """Fit scikit-learn LR on scaled features; expand to the fixed 3-class layout.

        A class absent from training carries logit ``-inf`` (intercept ``-inf``, zero
        coefficients), so its softmax probability is exactly 0 -- it never ties a fitted class
        (external review R6). The fitted classes keep the exact logits scikit-learn would use,
        so ``predict_proba`` over the fitted columns reproduces sklearn's ``predict_proba``
        bit-for-bit (softmax is invariant to adding ``-inf`` columns).
        """
        present = sorted(set(int(v) for v in y))
        n_features = xs.shape[1]
        coef = np.zeros((3, n_features), dtype=np.float64)
        # Default every class to the absent sentinel; fitted classes overwrite their rows below.
        intercept = np.full(3, -np.inf, dtype=np.float64)
        if len(present) <= 1:
            # Degenerate: one (or zero) class. The present class carries a finite (zero-logit)
            # bias so its probability is 1 after softmax against the two ``-inf`` absent classes;
            # a single-class training set cannot inform coefficients. If no class is present the
            # (unreachable here) all-``-inf`` layout is left for the caller to handle.
            if present:
                intercept[PRICE_CLASSES.index(present[0])] = 0.0
            return coef, intercept
        # L2 regularization: sklearn 1.9 deprecated the ``penalty`` argument; its default
        # (``l1_ratio=0``) is pure L2, so we rely on the default rather than ``penalty="l2"``.
        clf = LogisticRegression(
            C=c,
            solver="lbfgs",
            max_iter=1000,
            random_state=_RANDOM_STATE,
        )
        clf.fit(xs, y)
        sk_classes = [int(v) for v in clf.classes_]
        if len(sk_classes) == 2:
            # Binary LR stores a single coef row for the positive class; expand to two logits.
            # The negative (reference) class keeps a finite zero logit; the positive class keeps
            # sklearn's stored logit. The third, absent class keeps the ``-inf`` sentinel.
            pos = sk_classes[1]
            neg = sk_classes[0]
            intercept[PRICE_CLASSES.index(neg)] = 0.0
            coef[PRICE_CLASSES.index(pos)] = clf.coef_[0]
            intercept[PRICE_CLASSES.index(pos)] = clf.intercept_[0]
        else:
            for row, cls in enumerate(sk_classes):
                coef[PRICE_CLASSES.index(cls)] = clf.coef_[row]
                intercept[PRICE_CLASSES.index(cls)] = clf.intercept_[row]
        return coef, intercept

    # -- prediction ------------------------------------------------------------------------

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        """Return probabilities with columns in ``PRICE_CLASSES`` order (down, unchanged, up)."""
        p = self._require_fit()
        x = self._check_matrix(x, "x")
        xs = (x - p.scaler_mean) / p.scaler_scale
        return self._proba_from(xs, p.coef, p.intercept)

    def up_minus_down(self, x: np.ndarray) -> np.ndarray:
        """Convenience ``P(up) - P(down)`` used by the B2 rule (``u`` in product 6.1)."""
        proba = self.predict_proba(x)
        result: np.ndarray = proba[:, PRICE_CLASSES.index(1)] - proba[:, PRICE_CLASSES.index(-1)]
        return result

    @staticmethod
    def _proba_from(xs: np.ndarray, coef: np.ndarray, intercept: np.ndarray) -> np.ndarray:
        logits = xs @ coef.T + intercept
        return _softmax_rows(logits)

    @staticmethod
    def _log_loss(proba: np.ndarray, y: np.ndarray) -> float:
        eps = 1e-15
        clipped = np.clip(proba, eps, 1.0)
        idx = np.array([PRICE_CLASSES.index(int(v)) for v in y])
        return float(-np.log(clipped[np.arange(len(y)), idx]).mean())

    # -- (de)serialization support ---------------------------------------------------------

    @property
    def chosen_c(self) -> float:
        return self._require_fit().chosen_c

    @property
    def is_fitted(self) -> bool:
        return self._params is not None

    def to_state(self) -> dict[str, Any]:
        """Return a parameter dict for :class:`ModelArtifact`.

        ``coef`` and ``intercept`` are kept as NumPy arrays so :class:`ModelArtifact`
        externalizes them into the ``.npz`` sidecar. This matters for the absent-class
        convention: an absent class carries intercept ``-inf`` (external review R6), which the
        ``.npz`` stores exactly as a float64 while keeping the JSON sidecar strict (a bare
        ``-Infinity`` token is not valid JSON). ``from_state`` reads them back with
        ``np.asarray`` regardless of whether they arrive as arrays or nested lists.
        """
        p = self._require_fit()
        return {
            "classes": list(PRICE_CLASSES),
            "coef": p.coef,
            "intercept": p.intercept,
            "scaler_mean": p.scaler_mean,
            "scaler_scale": p.scaler_scale,
            "chosen_c": p.chosen_c,
            "c_grid": list(self.c_grid),
            "feature_names": list(self.schema.names),
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> PriceModel:
        schema = FeatureSchema.of([str(n) for n in state["feature_names"]])
        model = cls(schema, c_grid=tuple(float(c) for c in state["c_grid"]))
        model._params = _FitParams(
            coef=np.asarray(state["coef"], dtype=np.float64),
            intercept=np.asarray(state["intercept"], dtype=np.float64),
            scaler_mean=np.asarray(state["scaler_mean"], dtype=np.float64),
            scaler_scale=np.asarray(state["scaler_scale"], dtype=np.float64),
            chosen_c=float(state["chosen_c"]),
        )
        return model

    # -- validation helpers ----------------------------------------------------------------

    def _require_fit(self) -> _FitParams:
        if self._params is None:
            raise RuntimeError("PriceModel is not fitted")
        return self._params

    def _check_matrix(self, x: np.ndarray, name: str) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != self.schema.n_features:
            raise ValueError(f"{name} must be (n, {self.schema.n_features}); got shape {arr.shape}")
        if not np.all(np.isfinite(arr)):
            raise ValueError(f"{name} features must be finite")
        return arr

    @staticmethod
    def _check_labels(y: np.ndarray, n: int, name: str) -> np.ndarray:
        arr = np.asarray(y)
        if arr.ndim != 1 or arr.shape[0] != n:
            raise ValueError(f"{name} must be a 1-D array of length {n}; got shape {arr.shape}")
        if not np.isin(arr, PRICE_CLASSES).all():
            raise ValueError(f"{name} contains labels outside {PRICE_CLASSES}")
        return arr.astype(np.int64, copy=False)
