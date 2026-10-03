"""Baseline policy and shared checkpoint-eligibility tests (product section 6.1)."""

from __future__ import annotations

from qexec.core.tasks import CheckpointChoice, DecisionReason
from qexec.core.types import BookLevel, TaskSide, TradingStatus
from qexec.core.views import ClientOrderState, DecisionView
from qexec.policies.base import checkpoint_eligibility
from qexec.policies.baselines import B0Policy, B1Policy, B1ProbePolicy


def _view(
    *,
    order_state: ClientOrderState,
    pending: bool = False,
    reported_executed: int = 0,
    in_controller: bool = False,
) -> DecisionView:
    return DecisionView(
        now_ns=1000,
        task_id="T",
        session_id="S",
        side=TaskSide.BUY,
        horizon_ns=1_000_000_000,
        deadline_ns=2000,
        time_remaining_ns=1000,
        client_status=TradingStatus.TRADING,
        best_bid=BookLevel(100_000_000_000, 10, 1),
        best_ask=BookLevel(100_250_000_000, 10, 1),
        client_mid2=200_250_000_000,
        client_quote_age_ns=0,
        order_state=order_state,
        pending_command=pending,
        reported_executed=reported_executed,
        own_limit_price_fixed=100_000_000_000,
        order_age_ns=500,
        in_controller=in_controller,
    )


def test_eligibility_model_choice_when_working_unfilled_no_pending() -> None:
    v = _view(order_state=ClientOrderState.WORKING)
    assert checkpoint_eligibility(v) is DecisionReason.MODEL_CHOICE


def test_eligibility_reported_complete() -> None:
    v = _view(order_state=ClientOrderState.FILLED, reported_executed=1)
    assert checkpoint_eligibility(v) is DecisionReason.REPORTED_COMPLETE


def test_eligibility_pending_command() -> None:
    v = _view(order_state=ClientOrderState.WORKING, pending=True)
    assert checkpoint_eligibility(v) is DecisionReason.INELIGIBLE_PENDING_COMMAND


def test_eligibility_in_flight_is_pending() -> None:
    v = _view(order_state=ClientOrderState.IN_FLIGHT)
    assert checkpoint_eligibility(v) is DecisionReason.INELIGIBLE_PENDING_COMMAND


def test_eligibility_rejected() -> None:
    v = _view(order_state=ClientOrderState.REJECTED)
    assert checkpoint_eligibility(v) is DecisionReason.INELIGIBLE_REJECTED


def test_eligibility_not_working() -> None:
    v = _view(order_state=ClientOrderState.CANCELLED)
    assert checkpoint_eligibility(v) is DecisionReason.INELIGIBLE_NOT_WORKING


def test_eligibility_in_controller_not_working() -> None:
    v = _view(order_state=ClientOrderState.WORKING, in_controller=True)
    assert checkpoint_eligibility(v) is DecisionReason.INELIGIBLE_NOT_WORKING


def test_eligibility_unreported_fill_does_not_make_pending_eligible() -> None:
    """An unreported exchange fill does not change the client state; a pending in-flight command
    stays INELIGIBLE_PENDING_COMMAND (product 6.1: unreported fills must not affect this)."""
    v = _view(order_state=ClientOrderState.IN_FLIGHT, pending=True)
    assert checkpoint_eligibility(v) is DecisionReason.INELIGIBLE_PENDING_COMMAND


def test_b0_arrival_switches() -> None:
    v = _view(order_state=ClientOrderState.NONE)
    assert B0Policy().on_arrival(v) == "SWITCH_TO_TAKER"


def test_b1_arrival_joins_and_checkpoint_holds() -> None:
    v = _view(order_state=ClientOrderState.NONE)
    pol = B1Policy()
    assert pol.on_arrival(v) == "JOIN_BEST"
    choice, reason = pol.on_checkpoint(_view(order_state=ClientOrderState.WORKING))
    assert choice is CheckpointChoice.HOLD
    assert reason is DecisionReason.NOT_APPLICABLE


def test_b1_probe_is_b1_prefix() -> None:
    pol = B1ProbePolicy()
    assert pol.policy_id == "B1_PROBE"
    assert pol.on_arrival(_view(order_state=ClientOrderState.NONE)) == "JOIN_BEST"
