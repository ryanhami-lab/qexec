"""T60-style: the ordering logic in select_theta is the risk-allowance rule generalized to a
menu. B3/B3_NO_QUEUE call risk_allowance_choice directly; B2 threshold selection reuses the
same allowance-then-cost ordering. This test shows the two agree on a reduced two-theta menu
mapped onto a two-action choice.

The only sanctioned difference (product 6.3 vs 6.1) is the exact-tie preference: smaller theta
for select_theta, SWITCH for risk_allowance_choice. This test exercises a strict (non-tie) case
so both reduce to "within-allowance then min cost".
"""

from __future__ import annotations

import polars as pl

from qexec.core.tasks import CheckpointChoice
from qexec.models.decision import risk_allowance_choice, select_theta


def test_select_theta_matches_risk_allowance_on_two_candidates() -> None:
    # Map theta=0.0 <-> HOLD, theta=0.1 <-> SWITCH. One session so session-mean == task value.
    # miss: theta0=0.02, theta1=0.05 ; c_t: theta0=5.0, theta1=1.0.
    df = pl.DataFrame(
        [
            {"theta": 0.0, "session_id": "S", "miss": 0, "c_t": 5.0},
            {"theta": 0.1, "session_id": "S", "miss": 0, "c_t": 1.0},
        ]
    )
    # Make miss rates match the risk-allowance scenario via fractional means: use 100 rows.
    rows: list[dict[str, object]] = []
    for i in range(100):
        rows.append({"theta": 0.0, "session_id": "S", "miss": 1 if i < 2 else 0, "c_t": 5.0})
        rows.append({"theta": 0.1, "session_id": "S", "miss": 1 if i < 5 else 0, "c_t": 1.0})
    df = pl.DataFrame(rows)

    epsilon = 0.05
    sel = select_theta(df, epsilon=epsilon, menu=(0.0, 0.1))
    by_theta = {t.theta: t for t in sel.trials}
    assert abs(by_theta[0.0].mean_miss_rate - 0.02) < 1e-12
    assert abs(by_theta[0.1].mean_miss_rate - 0.05) < 1e-12

    # Equivalent risk_allowance_choice: p=HOLD 0.02, SWITCH 0.05 ; v=HOLD 5.0, SWITCH 1.0.
    # min p = 0.02 ; eps=0.05 -> both allowed (0.05 <= 0.07). argmin v -> SWITCH.
    choice, _ = risk_allowance_choice(
        {CheckpointChoice.HOLD: 0.02, CheckpointChoice.SWITCH: 0.05},
        {CheckpointChoice.HOLD: 5.0, CheckpointChoice.SWITCH: 1.0},
        epsilon=epsilon,
    )
    assert choice is CheckpointChoice.SWITCH
    # select_theta picks theta=0.1, the SWITCH-mapped candidate: same decision.
    assert sel.chosen_theta == 0.1


def test_tighter_epsilon_excludes_riskier_candidate_in_both() -> None:
    rows: list[dict[str, object]] = []
    for i in range(100):
        rows.append({"theta": 0.0, "session_id": "S", "miss": 1 if i < 2 else 0, "c_t": 5.0})
        rows.append({"theta": 0.1, "session_id": "S", "miss": 1 if i < 5 else 0, "c_t": 1.0})
    df = pl.DataFrame(rows)
    epsilon = 0.01  # 0.05 > 0.02 + 0.01 -> theta 0.1 excluded.
    sel = select_theta(df, epsilon=epsilon, menu=(0.0, 0.1))
    assert sel.chosen_theta == 0.0
    choice, _ = risk_allowance_choice(
        {CheckpointChoice.HOLD: 0.02, CheckpointChoice.SWITCH: 0.05},
        {CheckpointChoice.HOLD: 5.0, CheckpointChoice.SWITCH: 1.0},
        epsilon=epsilon,
    )
    assert choice is CheckpointChoice.HOLD
