"""Action-conditioned risk and value models for B3 (product section 8, architecture 10.3).

For each action in ``{HOLD, SWITCH}`` we fit:

* a binary deadline-miss model: ``LogisticRegression`` on standardized features. If the
  training labels contain only one class, we substitute a *constant empirical rate* model
  that records ``n`` and sets ``constant=True`` (an observed zero rate is disclosed, never
  asserted as a true zero — product section 8 / architecture 10.3).
* a regularized mean-cost model: ``Ridge`` with an alpha grid chosen by chronological
  validation MSE, on standardized features.

``predict`` returns, per action, ``p_hat`` (miss probability) and ``v_hat`` (expected
``C_T``). These feed :func:`qexec.models.decision.risk_allowance_choice` unchanged.
Everything is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.preprocessing import StandardScaler

from qexec.core.tasks import CheckpointChoice
from qexec.models.schema import FeatureSchema

__all__ = ["ActionModels", "ActionPrediction"]

DEFAULT_ALPHA_GRID: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0)
_RANDOM_STATE = 0


@dataclass(frozen=True, slots=True)
class ActionPrediction:
    """Per-action prediction pair."""

    p_hat: float
    v_hat: float


@dataclass(frozen=True, slots=True)
class _MissModel:
    """A fitted miss model, either logistic or a disclosed constant empirical rate."""

    constant: bool
    n: int
    constant_rate: float
    # Logistic parameters (used when not constant); scaled-feature space.
    coef: np.ndarray
    intercept: float
    scaler_mean: np.ndarray
    scaler_scale: np.ndarray

    def predict_one(self, x: np.ndarray) -> float:
        if self.constant:
            return self.constant_rate
        xs = (x - self.scaler_mean) / self.scaler_scale
        logit = float(xs @ self.coef + self.intercept)
        return float(1.0 / (1.0 + np.exp(-logit)))


@dataclass(frozen=True, slots=True)
class _CostModel:
    """A fitted Ridge mean-cost model in scaled-feature space."""

    coef: np.ndarray
    intercept: float
    scaler_mean: np.ndarray
    scaler_scale: np.ndarray
    chosen_alpha: float

    def predict_one(self, x: np.ndarray) -> float:
        xs = (x - self.scaler_mean) / self.scaler_scale
        return float(xs @ self.coef + self.intercept)


class ActionModels:
    """Container of per-action (miss, cost) models for B3."""

    def __init__(
        self,
        schema: FeatureSchema,
        alpha_grid: tuple[float, ...] = DEFAULT_ALPHA_GRID,
    ) -> None:
        if len(alpha_grid) == 0:
            raise ValueError("alpha_grid must be non-empty")
        self.schema = schema
        self.alpha_grid = alpha_grid
        self._miss: dict[CheckpointChoice, _MissModel] = {}
        self._cost: dict[CheckpointChoice, _CostModel] = {}

    def fit_action(
        self,
        action: CheckpointChoice,
        x_train: np.ndarray,
        miss_train: np.ndarray,
        cost_train: np.ndarray,
        *,
        x_cost_train: np.ndarray | None = None,
        cost_alpha: float | None = None,
        x_val: np.ndarray | None = None,
        cost_val: np.ndarray | None = None,
    ) -> ActionModels:
        """Fit the miss and cost models for one action.

        ``x_train``/``miss_train`` cover every evaluable outcome. ``x_cost_train`` may supply
        the smaller finite-cost population; by default it is ``x_train``. Missing costs must
        never remove miss labels. Validation chooses alpha; the caller may refit afterward.
        """
        x = self._check_matrix(x_train, "x_train")
        miss = self._check_1d(miss_train, x.shape[0], "miss_train")
        xc = x if x_cost_train is None else self._check_matrix(x_cost_train, "x_cost_train")
        cost = self._check_1d(cost_train, xc.shape[0], "cost_train")
        if x.shape[0] == 0 or xc.shape[0] == 0:
            raise ValueError(f"{action.value} requires non-empty miss and cost training rows")

        if cost_alpha is not None and cost_alpha not in self.alpha_grid:
            raise ValueError("cost_alpha must belong to the frozen alpha_grid")
        if cost_alpha is not None and (x_val is not None or cost_val is not None):
            raise ValueError("supply either cost_alpha for refit or validation rows for selection")
        if (x_val is None) != (cost_val is None):
            raise ValueError("x_val and cost_val must be provided together")
        miss_model = self._fit_miss(x, miss)
        cost_model = self._fit_cost(xc, cost, x_val, cost_val, cost_alpha)
        # A failed fit must leave the last complete model intact.
        self._miss[action] = miss_model
        self._cost[action] = cost_model
        return self

    def _fit_miss(self, x: np.ndarray, miss: np.ndarray) -> _MissModel:
        if not np.isin(miss, (0.0, 1.0)).all():
            raise ValueError("miss labels must be 0 or 1")
        classes = sorted(set(int(v) for v in miss))
        n = int(x.shape[0])
        if any(c not in (0, 1) for c in classes):
            raise ValueError("miss labels must be 0 or 1")
        n_features = x.shape[1]
        if len(classes) <= 1:
            # Single class: record a disclosed constant empirical rate.
            rate = float(classes[0]) if classes else 0.0
            return _MissModel(
                constant=True,
                n=n,
                constant_rate=rate,
                coef=np.zeros(n_features, dtype=np.float64),
                intercept=0.0,
                scaler_mean=np.zeros(n_features, dtype=np.float64),
                scaler_scale=np.ones(n_features, dtype=np.float64),
            )
        scaler = StandardScaler().fit(x)
        xs = scaler.transform(x)
        # L2 regularization via sklearn's default (``l1_ratio=0``); the explicit ``penalty``
        # argument was deprecated in sklearn 1.9.
        clf = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000, random_state=_RANDOM_STATE)
        clf.fit(xs, miss)
        return _MissModel(
            constant=False,
            n=n,
            constant_rate=float(miss.mean()),
            coef=np.asarray(clf.coef_[0], dtype=np.float64),
            intercept=float(clf.intercept_[0]),
            scaler_mean=np.asarray(scaler.mean_, dtype=np.float64),
            scaler_scale=np.asarray(scaler.scale_, dtype=np.float64),
        )

    def _fit_cost(
        self,
        x: np.ndarray,
        cost: np.ndarray,
        x_val: np.ndarray | None,
        cost_val: np.ndarray | None,
        cost_alpha: float | None = None,
    ) -> _CostModel:
        scaler = StandardScaler().fit(x)
        xs = scaler.transform(x)
        if (x_val is None) != (cost_val is None):
            raise ValueError("x_val and cost_val must be provided together")
        if x_val is not None and cost_val is not None:
            xv = self._check_matrix(x_val, "x_val")
            cv = self._check_1d(cost_val, xv.shape[0], "cost_val")
            xsv = scaler.transform(xv)
            chosen_alpha = self._select_alpha(xs, cost, xsv, cv)
        else:
            chosen_alpha = self.alpha_grid[0] if cost_alpha is None else cost_alpha
        ridge = Ridge(alpha=chosen_alpha, random_state=_RANDOM_STATE)
        ridge.fit(xs, cost)
        return _CostModel(
            coef=np.asarray(ridge.coef_, dtype=np.float64),
            intercept=float(ridge.intercept_),
            scaler_mean=np.asarray(scaler.mean_, dtype=np.float64),
            scaler_scale=np.asarray(scaler.scale_, dtype=np.float64),
            chosen_alpha=chosen_alpha,
        )

    def _select_alpha(
        self, xs: np.ndarray, cost: np.ndarray, xsv: np.ndarray, cv: np.ndarray
    ) -> float:
        best_alpha = self.alpha_grid[0]
        best_mse = float("inf")
        for alpha in self.alpha_grid:
            ridge = Ridge(alpha=alpha, random_state=_RANDOM_STATE)
            ridge.fit(xs, cost)
            pred = ridge.predict(xsv)
            mse = float(np.mean((pred - cv) ** 2))
            if mse < best_mse:
                best_mse = mse
                best_alpha = alpha
        return best_alpha

    # -- prediction ------------------------------------------------------------------------

    def predict(self, x: np.ndarray) -> dict[CheckpointChoice, ActionPrediction]:
        """Return ``{action: ActionPrediction(p_hat, v_hat)}`` for a single feature vector."""
        self._require_fit()
        vec = self._check_vector(x)
        return {
            action: ActionPrediction(
                p_hat=self._miss[action].predict_one(vec),
                v_hat=self._cost[action].predict_one(vec),
            )
            for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)
        }

    def miss_is_constant(self, action: CheckpointChoice) -> bool:
        return self._miss[action].constant

    def miss_n(self, action: CheckpointChoice) -> int:
        return self._miss[action].n

    def chosen_alpha(self, action: CheckpointChoice) -> float:
        return self._cost[action].chosen_alpha

    # -- (de)serialization -----------------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        self._require_fit()

        def miss_state(m: _MissModel) -> dict[str, Any]:
            return {
                "constant": m.constant,
                "n": m.n,
                "constant_rate": m.constant_rate,
                "coef": m.coef.tolist(),
                "intercept": m.intercept,
                "scaler_mean": m.scaler_mean.tolist(),
                "scaler_scale": m.scaler_scale.tolist(),
            }

        def cost_state(c: _CostModel) -> dict[str, Any]:
            return {
                "coef": c.coef.tolist(),
                "intercept": c.intercept,
                "scaler_mean": c.scaler_mean.tolist(),
                "scaler_scale": c.scaler_scale.tolist(),
                "chosen_alpha": c.chosen_alpha,
            }

        return {
            "alpha_grid": list(self.alpha_grid),
            "feature_names": list(self.schema.names),
            "actions": {
                action.value: {
                    "miss": miss_state(self._miss[action]),
                    "cost": cost_state(self._cost[action]),
                }
                for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)
            },
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> ActionModels:
        schema = FeatureSchema.of([str(n) for n in state["feature_names"]])
        model = cls(schema, alpha_grid=tuple(float(a) for a in state["alpha_grid"]))
        actions = state["actions"]
        for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
            entry = actions[action.value]
            m = entry["miss"]
            c = entry["cost"]
            model._miss[action] = _MissModel(
                constant=bool(m["constant"]),
                n=int(m["n"]),
                constant_rate=float(m["constant_rate"]),
                coef=np.asarray(m["coef"], dtype=np.float64),
                intercept=float(m["intercept"]),
                scaler_mean=np.asarray(m["scaler_mean"], dtype=np.float64),
                scaler_scale=np.asarray(m["scaler_scale"], dtype=np.float64),
            )
            model._cost[action] = _CostModel(
                coef=np.asarray(c["coef"], dtype=np.float64),
                intercept=float(c["intercept"]),
                scaler_mean=np.asarray(c["scaler_mean"], dtype=np.float64),
                scaler_scale=np.asarray(c["scaler_scale"], dtype=np.float64),
                chosen_alpha=float(c["chosen_alpha"]),
            )
        return model

    # -- validation helpers ----------------------------------------------------------------

    def _require_fit(self) -> None:
        for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
            if action not in self._miss or action not in self._cost:
                raise RuntimeError(f"ActionModels missing fitted models for {action}")

    def _check_matrix(self, x: np.ndarray, name: str) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != self.schema.n_features:
            raise ValueError(f"{name} must be (n, {self.schema.n_features}); got shape {arr.shape}")
        if not np.isfinite(arr).all():
            raise ValueError(f"{name} must contain only finite values")
        return arr

    def _check_vector(self, x: np.ndarray) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim != 1 or arr.shape[0] != self.schema.n_features:
            raise ValueError(
                f"feature vector must have {self.schema.n_features} values; got {arr.shape}"
            )
        return arr

    @staticmethod
    def _check_1d(y: np.ndarray, n: int, name: str) -> np.ndarray:
        arr = np.asarray(y, dtype=np.float64)
        if arr.ndim != 1 or arr.shape[0] != n:
            raise ValueError(f"{name} must be a 1-D array of length {n}; got shape {arr.shape}")
        if not np.isfinite(arr).all():
            raise ValueError(f"{name} must contain only finite values")
        return arr
