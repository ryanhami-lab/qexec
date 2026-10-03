"""T26: B2 threshold selection uses only provided validation rows, session means, smaller-theta
ties, and a complete trial log. Expected numbers are hand-derived below.

Product 6.3: on validation, keep thetas whose session-mean miss rate is within epsilon of the
menu minimum; among them minimize session-mean C_T over all technically evaluable tasks; exact
ties prefer smaller theta; log all trials. "Session-mean" means mean over sessions of each
session's task-mean (equal session weight), NOT a task-mean.
"""

from __future__ import annotations

import math

import polars as pl

from qexec.models.decision import THETA_MENU, select_theta


def _rows() -> pl.DataFrame:
    # Two sessions, unequal task counts, so session-mean vs task-mean differ.
    #
    # theta=0.0:
    #   session A (3 tasks): miss=[0,0,0] -> session_miss=0.0 ; c_t=[2,2,2] -> session_c_t=2.0
    #   session B (1 task):  miss=[1]     -> session_miss=1.0 ; c_t=[0]      -> session_c_t=0.0
    #   session-mean miss = (0.0 + 1.0)/2 = 0.5 ; session-mean c_t = (2.0 + 0.0)/2 = 1.0
    #   (task-mean miss would be 1/4 = 0.25 -> different, proving session weighting)
    #
    # theta=0.1:
    #   session A (3 tasks): miss=[0,0,0]=0.0 ; c_t=[1,1,1] -> 1.0
    #   session B (1 task):  miss=[0]=0.0     ; c_t=[3]     -> 3.0
    #   session-mean miss = 0.0 ; session-mean c_t = (1.0 + 3.0)/2 = 2.0
    #
    # theta=0.2:
    #   session A (3 tasks): miss=[0,0,0]=0.0 ; c_t=[1,1,1] -> 1.0
    #   session B (1 task):  miss=[0]=0.0     ; c_t=[3]     -> 3.0
    #   session-mean miss = 0.0 ; session-mean c_t = 2.0  (identical to theta=0.1)
    rows: list[dict[str, object]] = []

    def add(theta: float, session: str, misses: list[int], costs: list[float]) -> None:
        for m, c in zip(misses, costs, strict=True):
            rows.append({"theta": theta, "session_id": session, "miss": m, "c_t": c})

    add(0.0, "A", [0, 0, 0], [2.0, 2.0, 2.0])
    add(0.0, "B", [1], [0.0])
    add(0.1, "A", [0, 0, 0], [1.0, 1.0, 1.0])
    add(0.1, "B", [0], [3.0])
    add(0.2, "A", [0, 0, 0], [1.0, 1.0, 1.0])
    add(0.2, "B", [0], [3.0])
    return pl.DataFrame(rows)


def test_select_theta_uses_session_means_and_small_epsilon() -> None:
    sel = select_theta(_rows(), epsilon=0.001, menu=THETA_MENU)
    # Menu-min session-mean miss = min(0.5, 0.0, 0.0) = 0.0.
    assert sel.min_mean_miss_rate == 0.0
    trials = {t.theta: t for t in sel.trials}
    # Hand-computed session-means:
    assert trials[0.0].mean_miss_rate == 0.5
    assert trials[0.0].mean_c_t == 1.0
    assert trials[0.1].mean_miss_rate == 0.0
    assert trials[0.1].mean_c_t == 2.0
    assert trials[0.2].mean_miss_rate == 0.0
    assert trials[0.2].mean_c_t == 2.0
    # Allowance (eps=0.001): theta 0.0 has miss 0.5 > 0.001 -> excluded; 0.1 and 0.2 included.
    assert not trials[0.0].within_allowance
    assert trials[0.1].within_allowance
    assert trials[0.2].within_allowance
    # 0.1 and 0.2 tie on C_T (2.0 each) -> smaller theta wins.
    assert sel.chosen_theta == 0.1


def test_trial_log_is_complete_and_in_menu_order() -> None:
    sel = select_theta(_rows(), epsilon=0.001)
    assert tuple(t.theta for t in sel.trials) == THETA_MENU
    # n_tasks and n_sessions are recorded for every trial.
    by_theta = {t.theta: t for t in sel.trials}
    assert by_theta[0.0].n_sessions == 2
    assert by_theta[0.0].n_tasks == 4  # 3 + 1


def test_task_mean_would_differ_from_session_mean() -> None:
    # Guards the session-weighting requirement: a task-mean miss for theta=0.0 is 1/4=0.25,
    # which is NOT what the function reports (0.5).
    sel = select_theta(_rows(), epsilon=0.001)
    trial0 = next(t for t in sel.trials if t.theta == 0.0)
    assert trial0.mean_miss_rate == 0.5
    assert trial0.mean_miss_rate != 0.25


def test_large_epsilon_admits_all_then_pure_cost_with_smaller_theta_tie() -> None:
    # eps=1 admits every theta; among {0.0:c_t=1.0, 0.1:2.0, 0.2:2.0} min c_t is theta=0.0.
    sel = select_theta(_rows(), epsilon=1.0)
    assert all(t.within_allowance for t in sel.trials)
    assert sel.chosen_theta == 0.0


def test_missing_menu_theta_is_logged_but_not_selectable() -> None:
    # Only theta 0.1 present in data; menu still has 0.0 and 0.2 -> logged with NaN, unselectable.
    df = pl.DataFrame(
        [
            {"theta": 0.1, "session_id": "A", "miss": 0, "c_t": 1.0},
            {"theta": 0.1, "session_id": "B", "miss": 0, "c_t": 1.0},
        ]
    )
    sel = select_theta(df, epsilon=0.001)
    by_theta = {t.theta: t for t in sel.trials}
    assert math.isnan(by_theta[0.0].mean_miss_rate)
    assert not by_theta[0.0].within_allowance
    assert sel.chosen_theta == 0.1
