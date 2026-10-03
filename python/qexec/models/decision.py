"""Decision rules: the shared risk-allowance choice and B2 threshold selection.

These implement product sections 6.1 and 6.3 exactly. The single ``risk_allowance_choice``
function owns the risk-allowance set, exact-tie handling, and the nonfinite / unsupported
fallbacks; B3, B3_NO_QUEUE, and B2 threshold selection all route through it (architecture
9.1, T60). ``select_theta`` reuses the same ordering logic generalized to the price-rule
threshold menu, operating on session-mean aggregates of validation tasks (product 6.3).

Nothing here reruns strategy logic; it consumes aggregates and prediction maps only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl

from qexec.core.config import THETA_MENU
from qexec.core.tasks import CheckpointChoice, DecisionReason

__all__ = [
    "THETA_MENU",
    "ThetaSelection",
    "ThetaTrial",
    "risk_allowance_choice",
    "select_theta",
]


def risk_allowance_choice(
    p_hat: Mapping[CheckpointChoice, float],
    v_hat: Mapping[CheckpointChoice, float],
    epsilon: float,
    supported: bool = True,
) -> tuple[CheckpointChoice, DecisionReason]:
    """Choose HOLD or SWITCH by the product 6.1 risk-allowance rule.

    ``A_eps = {a : p_a <= min_b p_b + epsilon}``; choose ``argmin_{a in A_eps} v_a``.

    Conventions (product 6.1, exact):

    * Any nonfinite predicted ``p`` or ``v`` -> ``(SWITCH, FALLBACK_NONFINITE)``.
    * ``supported is False`` -> ``(SWITCH, FALLBACK_UNSUPPORTED)``.
    * Exact cost tie within ``A_eps`` -> ``SWITCH``.
    * Otherwise reason ``MODEL_CHOICE``.

    ``epsilon = 0`` reduces to strict risk-first ordering (lowest miss probability, with a
    cost tiebreak only among exactly-equal miss probabilities). ``epsilon = 1`` admits every
    action into ``A_eps`` (probabilities lie in ``[0, 1]``), reducing to pure cost
    minimization.

    Fallbacks are evaluated in a fixed order so provenance is unambiguous: a nonfinite
    prediction is checked first (the most specific numerical failure), then lack of support.
    Both fallbacks are deterministic and depend only on the inputs.

    ``p_hat`` and ``v_hat`` must each contain both checkpoint choices.
    """
    choices = (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)
    for choice in choices:
        if choice not in p_hat or choice not in v_hat:
            raise KeyError(f"risk_allowance_choice requires both actions; missing {choice}")

    # Nonfinite fallback (checked before support so the most specific numerical failure wins).
    for choice in choices:
        if not math.isfinite(p_hat[choice]) or not math.isfinite(v_hat[choice]):
            return CheckpointChoice.SWITCH, DecisionReason.FALLBACK_NONFINITE

    if not supported:
        return CheckpointChoice.SWITCH, DecisionReason.FALLBACK_UNSUPPORTED

    min_p = min(p_hat[choice] for choice in choices)
    allowed = [choice for choice in choices if p_hat[choice] <= min_p + epsilon]

    # Minimize cost within the allowance; SWITCH wins exact cost ties.
    best = allowed[0]
    for choice in allowed[1:]:
        cheaper = v_hat[choice] < v_hat[best]
        tie_to_switch = v_hat[choice] == v_hat[best] and choice is CheckpointChoice.SWITCH
        if cheaper or tie_to_switch:
            best = choice
    return best, DecisionReason.MODEL_CHOICE


@dataclass(frozen=True, slots=True)
class ThetaTrial:
    """One row of the threshold-selection trial log (one candidate ``theta``)."""

    theta: float
    mean_miss_rate: float
    """Equal-weight mean over sessions of each session's mean miss rate."""
    mean_c_t: float
    """Equal-weight mean over sessions of each session's mean ``C_T``."""
    n_sessions: int
    n_tasks: int
    within_allowance: bool
    """Whether this theta's session-mean miss rate is within ``epsilon`` of the menu min."""


@dataclass(frozen=True, slots=True)
class ThetaSelection:
    """Result of B2 threshold selection (product 6.3)."""

    chosen_theta: float
    epsilon: float
    min_mean_miss_rate: float
    """The minimum session-mean miss rate across the menu (the allowance anchor)."""
    trials: tuple[ThetaTrial, ...]
    """One entry per menu theta, in menu order (a complete trial log)."""


def select_theta(
    validation: pl.DataFrame,
    epsilon: float,
    menu: tuple[float, ...] = THETA_MENU,
) -> ThetaSelection:
    """Select the price-rule threshold ``theta`` on validation aggregates (product 6.3).

    ``validation`` is a polars ``DataFrame`` with one row per (theta, session, task):

    * ``theta`` (float): the candidate threshold the row was evaluated under.
    * ``session_id``: session identifier (equal-weight aggregation unit).
    * ``miss`` (0/1): deadline-miss indicator for the task.
    * ``c_t`` (float): deadline-horizon cost ``C_T`` for the task (product section 8).

    Rows must cover **all technically evaluable validation tasks** (the caller is
    responsible for that; this function does not filter). For each theta we compute the
    equal-weight session-mean miss rate (mean over sessions of each session's task-mean
    miss) and the equal-weight session-mean ``C_T``. We keep thetas whose session-mean miss
    rate is within ``epsilon`` of the menu minimum, then minimize session-mean ``C_T`` among
    them; exact ties prefer the **smaller** theta. Every menu theta appears in the trial log.

    This is the same risk-allowance ordering as :func:`risk_allowance_choice` generalized to
    a menu: miss rate plays the role of ``p``, ``C_T`` the role of ``v``; the only difference
    is the exact-tie preference (smaller theta here, SWITCH there), as the product specifies.
    """
    required = {"theta", "session_id", "miss", "c_t"}
    missing = required - set(validation.columns)
    if missing:
        raise ValueError(f"validation frame missing columns: {sorted(missing)}")
    if not 0.0 <= epsilon <= 1.0:
        raise ValueError("epsilon must lie in [0, 1]")
    if len(menu) == 0:
        raise ValueError("theta menu must be non-empty")

    # Aggregate task rows to session means, then session means to equal-weight overall means.
    # Done in two groupbys so sessions are weighted equally regardless of task count.
    per_session = validation.group_by("theta", "session_id").agg(
        pl.col("miss").cast(pl.Float64).mean().alias("session_miss"),
        pl.col("c_t").cast(pl.Float64).mean().alias("session_c_t"),
        pl.len().alias("session_n_tasks"),
    )
    per_theta = per_session.group_by("theta").agg(
        pl.col("session_miss").mean().alias("mean_miss_rate"),
        pl.col("session_c_t").mean().alias("mean_c_t"),
        pl.len().alias("n_sessions"),
        pl.col("session_n_tasks").sum().alias("n_tasks"),
    )

    stats: dict[float, dict[str, float]] = {}
    for row in per_theta.iter_rows(named=True):
        stats[float(row["theta"])] = {
            "mean_miss_rate": float(row["mean_miss_rate"]),
            "mean_c_t": float(row["mean_c_t"]),
            "n_sessions": int(row["n_sessions"]),
            "n_tasks": int(row["n_tasks"]),
        }

    present = [theta for theta in menu if theta in stats]
    if not present:
        raise ValueError("validation frame contains no rows for any menu theta")

    min_mean_miss = min(stats[theta]["mean_miss_rate"] for theta in present)

    trials: list[ThetaTrial] = []
    for theta in menu:
        if theta in stats:
            s = stats[theta]
            within = s["mean_miss_rate"] <= min_mean_miss + epsilon
            trials.append(
                ThetaTrial(
                    theta=theta,
                    mean_miss_rate=s["mean_miss_rate"],
                    mean_c_t=s["mean_c_t"],
                    n_sessions=int(s["n_sessions"]),
                    n_tasks=int(s["n_tasks"]),
                    within_allowance=within,
                )
            )
        else:
            # Menu theta with no validation rows: logged but never selectable.
            trials.append(
                ThetaTrial(
                    theta=theta,
                    mean_miss_rate=float("nan"),
                    mean_c_t=float("nan"),
                    n_sessions=0,
                    n_tasks=0,
                    within_allowance=False,
                )
            )

    # Minimize mean C_T; exact ties prefer smaller theta. Sort candidates by theta ascending
    # first, so a strict "<" comparison keeps the smallest theta on an exact cost tie regardless
    # of the menu's declared order.
    allowed = sorted((t for t in trials if t.within_allowance), key=lambda t: t.theta)
    best = allowed[0]
    for trial in allowed[1:]:
        if trial.mean_c_t < best.mean_c_t:
            best = trial

    return ThetaSelection(
        chosen_theta=best.theta,
        epsilon=epsilon,
        min_mean_miss_rate=min_mean_miss,
        trials=tuple(trials),
    )
