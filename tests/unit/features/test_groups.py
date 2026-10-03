"""Tests for :mod:`qexec.features.groups` -- allowlists, prohibitions, mechanics (T25, T45)."""

from __future__ import annotations

import pytest

from qexec.core.types import BookLevel, TaskSide, TradingStatus
from qexec.core.views import ClientOrderState, DecisionView
from qexec.features.groups import (
    MARKET_FEATURES,
    MECHANICS_FEATURES,
    PRICE_SIGNAL_FEATURES,
    QUEUE_FEATURES,
    allowlist,
    assert_allowed,
    mechanics_features,
)

TICK = 250_000_000


# --------------------------------------------------------------------------- allowlist shape


def test_allowlist_b3_is_union_of_all_four_groups() -> None:
    expected = MARKET_FEATURES + MECHANICS_FEATURES + PRICE_SIGNAL_FEATURES + QUEUE_FEATURES
    assert allowlist("B3") == expected


def test_allowlist_b2_is_price_signal_plus_mechanics() -> None:
    assert allowlist("B2") == PRICE_SIGNAL_FEATURES + MECHANICS_FEATURES


def test_allowlist_unknown_policy_raises() -> None:
    with pytest.raises(ValueError, match="unknown policy_id"):
        allowlist("NOPE")


# --------------------------------------------------------------------------- T45


def test_t45_b3_no_queue_excludes_exactly_the_queue_group() -> None:
    b3 = set(allowlist("B3"))
    b3_no_queue = set(allowlist("B3_NO_QUEUE"))
    # Removed set is exactly QUEUE_FEATURES: nothing more, nothing less.
    assert b3 - b3_no_queue == set(QUEUE_FEATURES)
    # No queue feature survives in B3_NO_QUEUE.
    assert not (b3_no_queue & set(QUEUE_FEATURES))
    # Every non-queue B3 feature is retained.
    assert b3_no_queue == b3 - set(QUEUE_FEATURES)


def test_t45_price_allowlist_has_no_queue_or_mechanics_features() -> None:
    price = set(allowlist("PRICE"))
    assert price == set(MARKET_FEATURES)
    assert not (price & set(QUEUE_FEATURES))
    assert not (price & set(MECHANICS_FEATURES))


def test_t45_queue_features_pass_for_b3_but_fail_for_b3_no_queue() -> None:
    # All queue features are allowed under B3 ...
    assert_allowed(QUEUE_FEATURES, "B3")
    # ... and every one of them is rejected under B3_NO_QUEUE.
    for col in QUEUE_FEATURES:
        with pytest.raises(ValueError, match="not in allowlist"):
            assert_allowed([col], "B3_NO_QUEUE")


# --------------------------------------------------------------------------- T25


def test_t25_oracle_and_true_queue_and_unreported_columns_rejected() -> None:
    for bad in (
        "true_queue_ahead",  # exact prohibited name
        "oracle_queue",  # contains 'oracle'
        "true_rank",  # contains 'true_'
        "unreported_executed",  # exact prohibited name
        "future_mid",  # 'future_' prefix
        "m0",  # evaluator benchmark
    ):
        with pytest.raises(ValueError, match="prohibited feature column"):
            assert_allowed([bad], "B3")


def test_t25_prohibited_takes_precedence_over_allowlist_message() -> None:
    # A prohibited column is reported as prohibited even if also absent from the allowlist.
    with pytest.raises(ValueError, match="prohibited feature column"):
        assert_allowed(["oracle_true_queue_ahead"], "PRICE")


def test_assert_allowed_accepts_full_valid_set() -> None:
    assert_allowed(allowlist("B3"), "B3")
    assert_allowed(MARKET_FEATURES, "PRICE")


def test_assert_allowed_rejects_out_of_allowlist_nonprohibited() -> None:
    # A perfectly innocent but out-of-group column is rejected as not-in-allowlist.
    with pytest.raises(ValueError, match="not in allowlist"):
        assert_allowed(["spread_ticks"], "B2")  # market feature not in B2


# --------------------------------------------------------------------------- mechanics_features


def _view(
    *,
    side: TaskSide,
    best_bid: int | None,
    best_ask: int | None,
    own_limit: int | None,
    order_age_ns: int | None,
    time_remaining_ns: int,
) -> DecisionView:
    bb = BookLevel(best_bid, 10, 1) if best_bid is not None else None
    ba = BookLevel(best_ask, 10, 1) if best_ask is not None else None
    return DecisionView(
        now_ns=1_000_000,
        task_id="t",
        session_id="s",
        side=side,
        horizon_ns=1_000_000_000,
        deadline_ns=2_000_000_000,
        time_remaining_ns=time_remaining_ns,
        client_status=TradingStatus.TRADING,
        best_bid=bb,
        best_ask=ba,
        client_mid2=None,
        client_quote_age_ns=0,
        order_state=ClientOrderState.WORKING,
        pending_command=False,
        reported_executed=0,
        own_limit_price_fixed=own_limit,
        order_age_ns=order_age_ns,
        in_controller=False,
    )


def test_mechanics_side_sign() -> None:
    buy = mechanics_features(
        _view(
            side=TaskSide.BUY,
            best_bid=100_000_000_000,
            best_ask=100_250_000_000,
            own_limit=100_000_000_000,
            order_age_ns=0,
            time_remaining_ns=0,
        ),
        TICK,
    )
    sell = mechanics_features(
        _view(
            side=TaskSide.SELL,
            best_bid=100_000_000_000,
            best_ask=100_250_000_000,
            own_limit=100_250_000_000,
            order_age_ns=0,
            time_remaining_ns=0,
        ),
        TICK,
    )
    assert buy["side"] == 1.0
    assert sell["side"] == -1.0


def test_mechanics_limit_offset_positive_is_less_aggressive_buy() -> None:
    # Buy resting two ticks below the best bid -> less aggressive -> +2.
    v = _view(
        side=TaskSide.BUY,
        best_bid=100_000_000_000,
        best_ask=100_250_000_000,
        own_limit=100_000_000_000 - 2 * TICK,
        order_age_ns=0,
        time_remaining_ns=0,
    )
    assert mechanics_features(v, TICK)["limit_offset_ticks"] == 2.0


def test_mechanics_limit_offset_positive_is_less_aggressive_sell() -> None:
    # Sell resting three ticks above the best ask -> less aggressive -> +3.
    v = _view(
        side=TaskSide.SELL,
        best_bid=100_000_000_000,
        best_ask=100_250_000_000,
        own_limit=100_250_000_000 + 3 * TICK,
        order_age_ns=0,
        time_remaining_ns=0,
    )
    assert mechanics_features(v, TICK)["limit_offset_ticks"] == 3.0


def test_mechanics_limit_offset_unknown_is_zero() -> None:
    v = _view(
        side=TaskSide.BUY,
        best_bid=None,
        best_ask=100_250_000_000,
        own_limit=None,
        order_age_ns=None,
        time_remaining_ns=0,
    )
    assert mechanics_features(v, TICK)["limit_offset_ticks"] == 0.0


def test_mechanics_age_and_time_remaining_in_ms() -> None:
    v = _view(
        side=TaskSide.BUY,
        best_bid=100_000_000_000,
        best_ask=100_250_000_000,
        own_limit=100_000_000_000,
        order_age_ns=5_000_000,  # 5 ms
        time_remaining_ns=250_000_000,  # 250 ms
    )
    out = mechanics_features(v, TICK)
    assert out["order_age_ms"] == 5.0
    assert out["time_remaining_ms"] == 250.0


def test_mechanics_time_remaining_clamped_at_zero() -> None:
    v = _view(
        side=TaskSide.BUY,
        best_bid=100_000_000_000,
        best_ask=100_250_000_000,
        own_limit=100_000_000_000,
        order_age_ns=None,
        time_remaining_ns=-10_000_000,
    )
    out = mechanics_features(v, TICK)
    assert out["time_remaining_ms"] == 0.0
    assert out["order_age_ms"] == 0.0
