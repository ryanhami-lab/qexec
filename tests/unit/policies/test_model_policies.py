"""Model-based policy tests: B2 sign/equality (T46), B3 end-to-end on a stub (T47),
schema mismatch is a run error (T52), the shared risk-allowance route (T60), and the
price-signal scorer contract (research spec section 1)."""

from __future__ import annotations

import numpy as np
import pytest

from qexec.core.tasks import CheckpointChoice, DecisionReason
from qexec.core.types import BookLevel, TaskSide, TradingStatus
from qexec.core.views import ClientOrderState, DecisionView
from qexec.features.groups import MARKET_FEATURES, allowlist
from qexec.models.action import ActionPrediction
from qexec.models.price import PriceModel
from qexec.models.schema import FeatureSchema, SupportRule
from qexec.policies.model_policies import B2Policy, B3Policy, PriceSignalScorer

_TICK = 250_000_000


def _market_features() -> dict[str, float]:
    # A full market-feature dict (every PRICE/B3 market feature present).
    return {name: 0.5 for name in MARKET_FEATURES}


def _view(
    *,
    side: TaskSide = TaskSide.BUY,
    price_signal: dict[str, float] | None = None,
    market: dict[str, float] | None = None,
    queue: dict[str, float] | None = None,
) -> DecisionView:
    return DecisionView(
        now_ns=1000,
        task_id="S:00000:BUY",
        session_id="S",
        side=side,
        horizon_ns=1_000_000_000,
        deadline_ns=2_000_000_000,
        time_remaining_ns=1_000_000_000,
        client_status=TradingStatus.TRADING,
        best_bid=BookLevel(100_000_000_000, 10, 1),
        best_ask=BookLevel(100_250_000_000, 10, 1),
        client_mid2=200_250_000_000,
        client_quote_age_ns=0,
        order_state=ClientOrderState.WORKING,
        pending_command=False,
        reported_executed=0,
        own_limit_price_fixed=100_000_000_000,
        order_age_ns=500,
        in_controller=False,
        market_features=market if market is not None else _market_features(),
        queue_features=queue
        if queue is not None
        else {name: 1.0 for name in allowlist("B3") if name.startswith("q_")},
        price_signal=price_signal if price_signal is not None else {},
    )


# --------------------------------------------------------------------------- B2 (T46)


def test_b2_buy_switches_when_signal_exceeds_theta() -> None:
    # Buy: side=+1, u=0.3, theta=0.2 -> +1*0.3=0.3 > 0.2 -> SWITCH.
    pol = B2Policy(0.2, "u_signal", "B2")
    choice, reason = pol.on_checkpoint(_view(price_signal={"u_signal": 0.3}))
    assert choice is CheckpointChoice.SWITCH
    assert reason is DecisionReason.MODEL_CHOICE


def test_b2_sell_switches_on_negative_signal() -> None:
    # Sell: side=-1, u=-0.3 -> -1*-0.3=0.3 > 0.2 -> SWITCH. A positive u for a sell HOLDs.
    pol = B2Policy(0.2, "u_signal", "B2")
    switch, _ = pol.on_checkpoint(_view(side=TaskSide.SELL, price_signal={"u_signal": -0.3}))
    hold, _ = pol.on_checkpoint(_view(side=TaskSide.SELL, price_signal={"u_signal": 0.3}))
    assert switch is CheckpointChoice.SWITCH
    assert hold is CheckpointChoice.HOLD


def test_b2_equality_holds() -> None:
    # Strict inequality: side*u == theta exactly -> HOLD (T46).
    pol = B2Policy(0.3, "u_signal", "B2")
    choice, reason = pol.on_checkpoint(_view(price_signal={"u_signal": 0.3}))
    assert choice is CheckpointChoice.HOLD
    assert reason is DecisionReason.MODEL_CHOICE


def test_b2_missing_or_nonfinite_signal_falls_back_to_switch() -> None:
    pol = B2Policy(0.0, "u_signal", "B2")
    miss, r1 = pol.on_checkpoint(_view(price_signal={}))
    nonf, r2 = pol.on_checkpoint(_view(price_signal={"u_signal": float("nan")}))
    assert miss is CheckpointChoice.SWITCH and r1 is DecisionReason.FALLBACK_NONFINITE
    assert nonf is CheckpointChoice.SWITCH and r2 is DecisionReason.FALLBACK_NONFINITE


def test_b2_100ms_reads_secondary_key() -> None:
    pol = B2Policy(0.0, "u_signal_100ms", "B2_100MS")
    # Only the 100ms key is present; a strong positive secondary signal switches a buy.
    choice, _ = pol.on_checkpoint(_view(price_signal={"u_signal": -0.9, "u_signal_100ms": 0.5}))
    assert choice is CheckpointChoice.SWITCH


# --------------------------------------------------------------------------- B3 (T47, T52, T60)


class _StubActionModels:
    """A stub exposing ActionModels' ``schema`` and ``predict`` for a deterministic choice."""

    def __init__(
        self, schema: FeatureSchema, p_hold: float, p_switch: float, v_hold: float, v_switch: float
    ) -> None:
        self.schema = schema
        self._p = {CheckpointChoice.HOLD: p_hold, CheckpointChoice.SWITCH: p_switch}
        self._v = {CheckpointChoice.HOLD: v_hold, CheckpointChoice.SWITCH: v_switch}

    def predict(self, x: np.ndarray) -> dict[CheckpointChoice, ActionPrediction]:
        del x
        return {
            a: ActionPrediction(p_hat=self._p[a], v_hat=self._v[a])
            for a in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)
        }


def _b3_schema() -> FeatureSchema:
    return FeatureSchema.of(list(allowlist("B3")))


def _full_support(schema: FeatureSchema) -> SupportRule:
    # Supported over a very wide region so is_supported is True for the test vector (which
    # includes large mechanics values such as time_remaining_ms ~ 1e3).
    x = np.zeros((2, schema.n_features), dtype=np.float64)
    x[1, :] = 10.0
    return SupportRule.from_training(schema, x, min_training_rows=1, margin=1e12)


def test_b3_end_to_end_holds_when_hold_is_safe_and_cheap() -> None:
    # T47: HOLD has lower miss prob and lower cost -> HOLD, reason MODEL_CHOICE.
    schema = _b3_schema()
    models = _StubActionModels(schema, p_hold=0.1, p_switch=0.9, v_hold=1.0, v_switch=2.0)
    pol = B3Policy(
        models, schema, _full_support(schema), epsilon=0.001, policy_id="B3", tick_size_fixed=_TICK
    )
    choice, reason = pol.on_checkpoint(
        _view(price_signal={"p_down": 0.1, "p_unch": 0.2, "p_up": 0.7, "u_signal": 0.6})
    )
    assert choice is CheckpointChoice.HOLD
    assert reason is DecisionReason.MODEL_CHOICE
    assert len(pol.decision_log) == 1
    row = pol.decision_log[0]
    assert row["choice"] == "HOLD" and row["supported"] is True
    assert row["p_hold"] == 0.1 and row["v_switch"] == 2.0


def test_b3_switches_when_hold_misses_more() -> None:
    # T60: routes through risk_allowance_choice; SWITCH has far lower miss -> SWITCH.
    schema = _b3_schema()
    models = _StubActionModels(schema, p_hold=0.9, p_switch=0.1, v_hold=1.0, v_switch=5.0)
    pol = B3Policy(
        models, schema, _full_support(schema), epsilon=0.001, policy_id="B3", tick_size_fixed=_TICK
    )
    choice, reason = pol.on_checkpoint(
        _view(price_signal={"p_down": 0.1, "p_unch": 0.2, "p_up": 0.7, "u_signal": 0.6})
    )
    assert choice is CheckpointChoice.SWITCH
    assert reason is DecisionReason.MODEL_CHOICE


def test_b3_unsupported_falls_back_to_switch() -> None:
    schema = _b3_schema()
    models = _StubActionModels(schema, p_hold=0.1, p_switch=0.9, v_hold=1.0, v_switch=2.0)
    # Support region [0,0] with zero margin and a nonzero feature vector -> unsupported.
    x0 = np.zeros((1, schema.n_features), dtype=np.float64)
    support = SupportRule.from_training(schema, x0, min_training_rows=1, margin=0.0)
    pol = B3Policy(models, schema, support, epsilon=0.001, policy_id="B3", tick_size_fixed=_TICK)
    choice, reason = pol.on_checkpoint(
        _view(price_signal={"p_down": 0.1, "p_unch": 0.2, "p_up": 0.7, "u_signal": 0.6})
    )
    assert choice is CheckpointChoice.SWITCH
    assert reason is DecisionReason.FALLBACK_UNSUPPORTED


def test_b3_schema_mismatch_is_run_error_not_fallback() -> None:
    # T52: a schema expecting a feature the view cannot provide raises, never a silent fallback.
    schema = FeatureSchema.of([*allowlist("B3"), "nonexistent_feature"])
    models = _StubActionModels(schema, p_hold=0.1, p_switch=0.9, v_hold=1.0, v_switch=2.0)
    pol = B3Policy(
        models, schema, _full_support(schema), epsilon=0.001, policy_id="B3", tick_size_fixed=_TICK
    )
    with pytest.raises(ValueError, match="schema mismatch"):
        pol.on_checkpoint(
            _view(price_signal={"p_down": 0.1, "p_unch": 0.2, "p_up": 0.7, "u_signal": 0.6})
        )


def test_b3_no_queue_rejects_queue_feature_via_allowlist() -> None:
    # A B3_NO_QUEUE policy whose schema contains a queue feature is rejected by assert_allowed.
    schema = FeatureSchema.of([*allowlist("B3_NO_QUEUE"), "q_ahead_est"])
    models = _StubActionModels(schema, p_hold=0.1, p_switch=0.9, v_hold=1.0, v_switch=2.0)
    pol = B3Policy(
        models,
        schema,
        _full_support(schema),
        epsilon=0.001,
        policy_id="B3_NO_QUEUE",
        tick_size_fixed=_TICK,
    )
    with pytest.raises(ValueError, match=r"not in allowlist|prohibited"):
        pol.on_checkpoint(
            _view(
                price_signal={"p_down": 0.1, "p_unch": 0.2, "p_up": 0.7, "u_signal": 0.6},
                queue={"q_ahead_est": 1.0},
            )
        )


# --------------------------------------------------------------------------- PriceSignalScorer


def _fit_price_model() -> PriceModel:
    schema = FeatureSchema.of(list(MARKET_FEATURES))
    rng = np.random.default_rng(0)
    n = 60
    x = rng.normal(size=(n, schema.n_features))
    # Label depends on the first feature so the model learns a direction.
    y = np.where(x[:, 0] > 0.3, 1, np.where(x[:, 0] < -0.3, -1, 0))
    model = PriceModel(schema)
    model.fit(x, y)
    return model


def test_price_scorer_returns_signal_and_u_is_consistent() -> None:
    model = _fit_price_model()
    scorer = PriceSignalScorer(model)
    out = scorer(_market_features())
    assert set(out) == {"p_down", "p_unch", "p_up", "u_signal"}
    assert out["u_signal"] == pytest.approx(out["p_up"] - out["p_down"])
    assert abs(out["p_down"] + out["p_unch"] + out["p_up"] - 1.0) < 1e-9


def test_price_scorer_secondary_adds_100ms_signal() -> None:
    primary = _fit_price_model()
    secondary = _fit_price_model()
    scorer = PriceSignalScorer(primary, secondary)
    out = scorer(_market_features())
    assert "u_signal_100ms" in out


def test_price_scorer_missing_feature_is_hard_error() -> None:
    model = _fit_price_model()
    scorer = PriceSignalScorer(model)
    partial = _market_features()
    partial.pop("spread_ticks")
    with pytest.raises(ValueError, match="missing market features"):
        scorer(partial)
