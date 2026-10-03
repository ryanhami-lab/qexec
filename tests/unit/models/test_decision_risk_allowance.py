"""T47: B3 risk-allowance rule, exact tie, missing support, epsilon boundaries.

All expected outcomes are hand-derived from product section 6.1:
``A_eps = {a : p_a <= min_b p_b + eps}``; ``a* = argmin_{a in A_eps} v_a``; exact cost tie
within A_eps chooses SWITCH; nonfinite -> (SWITCH, FALLBACK_NONFINITE); unsupported ->
(SWITCH, FALLBACK_UNSUPPORTED).
"""

from __future__ import annotations

import math

from qexec.core.tasks import CheckpointChoice, DecisionReason
from qexec.models.decision import risk_allowance_choice

HOLD = CheckpointChoice.HOLD
SWITCH = CheckpointChoice.SWITCH


def test_cheaper_action_within_epsilon_is_chosen() -> None:
    # p: HOLD=0.010, SWITCH=0.011. min p = 0.010; eps=0.005 -> both in A_eps (0.011 <= 0.015).
    # v: HOLD=5.0, SWITCH=2.0 -> argmin v is SWITCH. Cheaper action chosen because its slightly
    # higher miss gap (0.001) is within the allowance.
    p = {HOLD: 0.010, SWITCH: 0.011}
    v = {HOLD: 5.0, SWITCH: 2.0}
    choice, reason = risk_allowance_choice(p, v, epsilon=0.005)
    assert choice is SWITCH
    assert reason is DecisionReason.MODEL_CHOICE


def test_safer_action_chosen_when_gap_exceeds_epsilon() -> None:
    # p: HOLD=0.010, SWITCH=0.030. min p=0.010; eps=0.005 -> A_eps = {HOLD} only
    # (0.030 > 0.015). So HOLD is chosen despite its higher cost: the gap exceeds allowance.
    p = {HOLD: 0.010, SWITCH: 0.030}
    v = {HOLD: 5.0, SWITCH: 2.0}
    choice, reason = risk_allowance_choice(p, v, epsilon=0.005)
    assert choice is HOLD
    assert reason is DecisionReason.MODEL_CHOICE


def test_exact_cost_tie_within_allowance_chooses_switch() -> None:
    # Equal p (both in A_eps for any eps>=0) and exactly equal v -> SWITCH by tie convention.
    p = {HOLD: 0.02, SWITCH: 0.02}
    v = {HOLD: 3.0, SWITCH: 3.0}
    choice, reason = risk_allowance_choice(p, v, epsilon=0.0)
    assert choice is SWITCH
    assert reason is DecisionReason.MODEL_CHOICE


def test_nonfinite_prediction_falls_back_to_switch() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        p = {HOLD: bad, SWITCH: 0.01}
        v = {HOLD: 1.0, SWITCH: 2.0}
        choice, reason = risk_allowance_choice(p, v, epsilon=0.001)
        assert choice is SWITCH
        assert reason is DecisionReason.FALLBACK_NONFINITE
    # Nonfinite in v also triggers the same fallback.
    choice, reason = risk_allowance_choice(
        {HOLD: 0.01, SWITCH: 0.02}, {HOLD: math.inf, SWITCH: 1.0}, epsilon=0.001
    )
    assert choice is SWITCH
    assert reason is DecisionReason.FALLBACK_NONFINITE


def test_unsupported_falls_back_to_switch() -> None:
    p = {HOLD: 0.01, SWITCH: 0.5}
    v = {HOLD: 1.0, SWITCH: 2.0}
    choice, reason = risk_allowance_choice(p, v, epsilon=0.001, supported=False)
    assert choice is SWITCH
    assert reason is DecisionReason.FALLBACK_UNSUPPORTED


def test_nonfinite_takes_priority_over_unsupported() -> None:
    # Both failures present: the more specific numerical failure is reported.
    p = {HOLD: math.nan, SWITCH: 0.01}
    v = {HOLD: 1.0, SWITCH: 2.0}
    choice, reason = risk_allowance_choice(p, v, epsilon=0.001, supported=False)
    assert choice is SWITCH
    assert reason is DecisionReason.FALLBACK_NONFINITE


def test_epsilon_zero_is_strict_risk_first() -> None:
    # eps=0: only the strictly-lowest-miss action is in A_eps unless there is an exact p tie.
    # HOLD has lower miss, so HOLD wins even though SWITCH is cheaper.
    p = {HOLD: 0.010, SWITCH: 0.011}
    v = {HOLD: 9.0, SWITCH: 1.0}
    choice, reason = risk_allowance_choice(p, v, epsilon=0.0)
    assert choice is HOLD
    assert reason is DecisionReason.MODEL_CHOICE
    # When miss probs tie exactly under eps=0, cost decides (and ties go SWITCH).
    choice2, _ = risk_allowance_choice({HOLD: 0.02, SWITCH: 0.02}, v, epsilon=0.0)
    assert choice2 is SWITCH  # SWITCH cheaper


def test_epsilon_one_is_pure_cost() -> None:
    # eps=1: every action enters A_eps (probabilities in [0,1]), so pure cost minimization.
    # HOLD much safer but SWITCH far cheaper -> SWITCH wins.
    p = {HOLD: 0.001, SWITCH: 0.900}
    v = {HOLD: 10.0, SWITCH: 1.0}
    choice, reason = risk_allowance_choice(p, v, epsilon=1.0)
    assert choice is SWITCH
    assert reason is DecisionReason.MODEL_CHOICE
    # And if HOLD is cheaper under eps=1, HOLD wins (pure cost, no SWITCH tiebreak needed).
    choice2, _ = risk_allowance_choice(p, {HOLD: 1.0, SWITCH: 10.0}, epsilon=1.0)
    assert choice2 is HOLD
