"""Model-based policies B2, B2_100MS, B3, and B3_NO_QUEUE (product sections 6.1, 6.3).

These are the only *discretionary* policies in the study. All of them join the best quote at
arrival (identical B1 prefix) and act only at the single eligible checkpoint ``t0 + H/2``; the
engine applies :func:`~qexec.policies.base.checkpoint_eligibility` first and consults the policy
only when the verdict is ``MODEL_CHOICE`` (architecture 9.1). None of these policies reruns the
engine; they consume the client-plane :class:`~qexec.core.views.DecisionView` and the frozen
model artifacts only.

* :class:`PriceSignalScorer` turns a market-feature dict into the shared price-direction signal
  dict the engine attaches to every ``DecisionView`` as ``view.price_signal``.
* :class:`B2Policy` is the price-rule threshold policy: SWITCH iff ``side * u > theta`` (strict).
  ``B2`` uses the primary signal ``u_signal``; ``B2_100MS`` uses the secondary ``u_signal_100ms``.
* :class:`B3Policy` is the risk-allowance action policy. It builds the feature vector in the
  frozen :class:`~qexec.models.schema.FeatureSchema` order, predicts per-action miss probability
  and expected cost with the frozen :class:`~qexec.models.action.ActionModels`, and routes the
  choice through :func:`~qexec.models.risk_allowance_choice` (the single shared decision rule,
  T60). Every call appends a row to :attr:`B3Policy.decision_log` so epsilon sensitivity can be
  recomputed from stored predictions without rerunning replay (research spec section 1).

The same :class:`B3Policy` class serves both ``B3`` (queue features included) and
``B3_NO_QUEUE`` (queue features removed) via different ``models``/``schema``/``policy_id``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from qexec.core.tasks import CheckpointChoice, DecisionReason
from qexec.core.views import DecisionView
from qexec.features.groups import (
    PRICE_SIGNAL_FEATURES,
    assert_allowed,
    mechanics_features,
)
from qexec.models.action import ActionModels
from qexec.models.decision import risk_allowance_choice
from qexec.models.price import PRICE_CLASSES, PriceModel
from qexec.models.schema import FeatureSchema, SupportRule
from qexec.policies.base import ArrivalAction

__all__ = ["B2Policy", "B3Policy", "PriceSignalScorer"]


class PriceSignalScorer:
    """Callable mapping a market-feature dict to the shared price-direction signal.

    The scorer evaluates the primary price model and, if present, a secondary (100 ms) price
    model over the exact :data:`~qexec.features.groups.MARKET_FEATURES` ordering fixed by each
    model's :class:`~qexec.models.schema.FeatureSchema`. It returns

    * ``p_down``, ``p_unch``, ``p_up`` -- primary class probabilities (classes ``-1, 0, 1``);
    * ``u_signal`` -- the primary directional signal ``P(up) - P(down)`` (``u`` in product 6.1);
    * ``u_signal_100ms`` -- the secondary directional signal (present only with a secondary
      model).

    A market-feature dict missing any schema feature, or carrying a nonfinite value, is a hard
    error -- the price signal must never be fabricated from partial information.
    """

    def __init__(self, primary: PriceModel, secondary: PriceModel | None = None) -> None:
        self._primary = primary
        self._secondary = secondary

    def _vector(self, market_features: Mapping[str, float], schema: FeatureSchema) -> np.ndarray:
        missing = [name for name in schema.names if name not in market_features]
        if missing:
            raise ValueError(f"price scorer missing market features: {missing!r}")
        values = [float(market_features[name]) for name in schema.names]
        if any(not math.isfinite(v) for v in values):
            raise ValueError("price scorer received a nonfinite market feature")
        return np.asarray(values, dtype=np.float64).reshape(1, -1)

    def __call__(self, market_features: Mapping[str, float]) -> dict[str, float]:
        x = self._vector(market_features, self._primary.schema)
        proba = self._primary.predict_proba(x)[0]
        p_down = float(proba[PRICE_CLASSES.index(-1)])
        p_unch = float(proba[PRICE_CLASSES.index(0)])
        p_up = float(proba[PRICE_CLASSES.index(1)])
        signal: dict[str, float] = {
            "p_down": p_down,
            "p_unch": p_unch,
            "p_up": p_up,
            "u_signal": p_up - p_down,
        }
        if self._secondary is not None:
            xs = self._vector(market_features, self._secondary.schema)
            proba2 = self._secondary.predict_proba(xs)[0]
            p_up2 = float(proba2[PRICE_CLASSES.index(1)])
            p_down2 = float(proba2[PRICE_CLASSES.index(-1)])
            signal["u_signal_100ms"] = p_up2 - p_down2
        return signal


class B2Policy:
    """Price-rule threshold policy (product 6.1).

    JOIN_BEST at arrival. At an eligible checkpoint SWITCH iff ``side * u > theta`` (strict;
    equality HOLDs), with reason ``MODEL_CHOICE``. A missing or nonfinite signal yields
    ``(SWITCH, FALLBACK_NONFINITE)`` -- consistent with the risk-allowance fallback ordering, so
    an unavailable price signal is handled conservatively rather than silently.

    ``signal_key`` selects which component of ``view.price_signal`` is read: ``u_signal`` for the
    primary ``B2`` and ``u_signal_100ms`` for the secondary ``B2_100MS``.
    """

    def __init__(
        self,
        theta: float,
        signal_key: str = "u_signal",
        policy_id: str = "B2",
    ) -> None:
        self.theta = float(theta)
        self.signal_key = signal_key
        self.policy_id = policy_id

    def on_arrival(self, view: DecisionView) -> ArrivalAction:
        del view
        return "JOIN_BEST"

    def on_checkpoint(self, view: DecisionView) -> tuple[CheckpointChoice, DecisionReason]:
        signal = view.price_signal.get(self.signal_key)
        if signal is None or not math.isfinite(signal):
            return (CheckpointChoice.SWITCH, DecisionReason.FALLBACK_NONFINITE)
        # side * u > theta is a strict inequality; exact equality HOLDs (research spec T46).
        if int(view.side) * float(signal) > self.theta:
            return (CheckpointChoice.SWITCH, DecisionReason.MODEL_CHOICE)
        return (CheckpointChoice.HOLD, DecisionReason.MODEL_CHOICE)


class B3Policy:
    """Risk-allowance action policy for B3 / B3_NO_QUEUE (product 6.1, architecture 9.1).

    JOIN_BEST at arrival. At an eligible checkpoint it builds the feature vector ``x`` in the
    frozen :class:`FeatureSchema` order from the client-plane view's
    ``market_features | mechanics_features | price_signal | queue_features`` union, asserts the
    columns are allowed for ``policy_id`` (never leaking an oracle/queue column into the
    no-queue arm), predicts per-action miss probability and expected cost, and routes the choice
    through :func:`risk_allowance_choice` with the configured ``epsilon`` and the support verdict.

    Every eligible-checkpoint call appends one row to :attr:`decision_log` recording the task id,
    time, both predicted ``p``/``v`` pairs, the support verdict, the chosen action, and the
    reason. This lets epsilon sensitivity be recomputed from stored predictions (research spec
    section 9) without rerunning replay. A schema mismatch is a run error (never a fallback,
    T52): the ``assert_allowed`` and schema-ordered vector build raise rather than silently
    producing a different prediction.
    """

    def __init__(
        self,
        models: ActionModels,
        schema: FeatureSchema,
        support: SupportRule,
        epsilon: float,
        policy_id: str,
        *,
        tick_size_fixed: int,
    ) -> None:
        self._models = models
        self._schema = schema
        self._support = support
        self._epsilon = float(epsilon)
        self.policy_id = policy_id
        # ``mechanics_features`` needs the tick size to form ``limit_offset_ticks``. The engine
        # calls ``on_checkpoint(view)`` with the view alone (``policies/base.Policy`` protocol),
        # so B3 captures the instrument tick size at construction rather than reading it off the
        # view (which does not carry it).
        self._tick_size_fixed = int(tick_size_fixed)
        # One row per eligible-checkpoint decision; stored predictions enable epsilon
        # sensitivity recomputation without replay (research spec section 9).
        self.decision_log: list[dict[str, Any]] = []

    @property
    def schema(self) -> FeatureSchema:
        return self._schema

    def on_arrival(self, view: DecisionView) -> ArrivalAction:
        del view
        return "JOIN_BEST"

    def _feature_dict(self, view: DecisionView) -> dict[str, float]:
        feats: dict[str, float] = {}
        feats.update(view.market_features)
        feats.update(mechanics_features(view, self._tick_size_fixed))
        # Only the modelled price-signal columns enter the schema (not u_signal_100ms).
        for name in PRICE_SIGNAL_FEATURES:
            if name in view.price_signal:
                feats[name] = float(view.price_signal[name])
        feats.update(view.queue_features)
        return feats

    def on_checkpoint(self, view: DecisionView) -> tuple[CheckpointChoice, DecisionReason]:
        """Decide HOLD/SWITCH for an eligible checkpoint and log the prediction.

        Builds ``x`` in frozen schema order, asserts allowed columns, predicts, and routes
        through :func:`risk_allowance_choice`. A schema mismatch raises (T52), never a fallback.
        """
        feats = self._feature_dict(view)
        missing = [name for name in self._schema.names if name not in feats]
        if missing:
            # Schema mismatch is a run error, never a fallback (T52).
            raise ValueError(
                f"B3 feature schema mismatch: missing {missing!r} for {self.policy_id}"
            )
        assert_allowed(self._schema.names, self.policy_id)
        x = np.asarray([feats[name] for name in self._schema.names], dtype=np.float64)
        preds = self._models.predict(x)
        p_hat = {
            action: preds[action].p_hat
            for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)
        }
        v_hat = {
            action: preds[action].v_hat
            for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)
        }
        supported = self._support.is_supported(x)
        choice, reason = risk_allowance_choice(p_hat, v_hat, self._epsilon, supported=supported)
        self.decision_log.append(
            {
                "task_id": view.task_id,
                "time_ns": int(view.now_ns),
                "p_hold": float(p_hat[CheckpointChoice.HOLD]),
                "p_switch": float(p_hat[CheckpointChoice.SWITCH]),
                "v_hold": float(v_hat[CheckpointChoice.HOLD]),
                "v_switch": float(v_hat[CheckpointChoice.SWITCH]),
                "supported": bool(supported),
                "choice": choice.value,
                "reason": reason.value,
            }
        )
        return (choice, reason)
