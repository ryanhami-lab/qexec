"""Analysis tables for the research study (research spec section 2, stage 9).

Every table consumes persisted ``TaskResult`` rows (and, for epsilon sensitivity, the stored B3
decision logs); none reruns strategy logic. The statistical protocol is
:mod:`qexec.analysis.stats` (per-session paired, session-block bootstrap, equal-weight sessions).

Tables produced (all tagged ``data_kind = "SYNTHETIC"``):

* ``primary_pair`` -- B3_NO_QUEUE (baseline) vs B3 (candidate) at the primary horizon, L1:
  per-session paired ``is_ticks_net`` effect on the common-completion set, paired miss
  difference on all evaluable tasks, joint session bootstrap intervals, and the zero-event
  session upper bound when misses are all zero.
* ``secondary_pairs`` -- B2 vs B3, B1 vs B3, B0 vs B1, B2 vs B2_100MS, each on its own pairwise
  set at the primary configuration.
* ``per_policy_distribution`` -- counts by status, passive fill fraction, mean/median IS, miss
  rate per policy (primary configuration).
* ``latency_table`` -- the primary pair effect per scenario (frozen policies).
* ``epsilon_sensitivity`` -- recomputed action shares and agreement with the primary epsilon from
  the stored B3 decision logs (no replay rerun).
* ``support_table`` -- the G2-S per-horizon support summary (counts only).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import polars as pl

from qexec.analysis.stats import (
    paired_miss_difference,
    session_block_bootstrap,
    session_paired_effect,
    zero_event_session_upper_bound,
)
from qexec.core.config import PRIMARY_LATENCY_ID
from qexec.core.tasks import CheckpointChoice
from qexec.models.decision import risk_allowance_choice

__all__ = [
    "DATA_KIND",
    "build_all_tables",
    "epsilon_sensitivity_table",
    "latency_table",
    "per_policy_distribution_table",
    "primary_pair_table",
    "secondary_pairs_table",
]

DATA_KIND = "SYNTHETIC"

_PRIMARY_BASELINE = "B3_NO_QUEUE"
_PRIMARY_CANDIDATE = "B3"
_SECONDARY_PAIRS: tuple[tuple[str, str], ...] = (
    ("B2", "B3"),
    ("B1", "B3"),
    ("B0", "B1"),
    ("B2", "B2_100MS"),
)


def _primary_rows(task_results: pl.DataFrame) -> pl.DataFrame:
    """The primary configuration slice: the primary latency (L1) scenario only."""
    if task_results.height == 0:
        return task_results
    return task_results.filter(pl.col("scenario_id") == PRIMARY_LATENCY_ID)


def _as_float(value: object) -> float | None:
    """Coerce a polars aggregate (which types as a broad union) to ``float`` or ``None``."""
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def _pair_record(
    results: pl.DataFrame,
    baseline: str,
    candidate: str,
    *,
    bootstrap_n: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    cost = session_paired_effect(results, baseline, candidate, "is_ticks_net")
    miss = paired_miss_difference(results, baseline, candidate)
    cost_lo, cost_hi = session_block_bootstrap(
        results, baseline, candidate, "cost", bootstrap_n, bootstrap_seed, metric="is_ticks_net"
    )
    miss_lo, miss_hi = session_block_bootstrap(
        results, baseline, candidate, "miss", bootstrap_n, bootstrap_seed
    )
    # Zero-event upper bound: when every evaluable candidate miss is zero, report residual risk.
    cand_rows = results.filter(
        (pl.col("policy_id") == candidate) & (pl.col("status") != "TECHNICALLY_UNEVALUABLE")
    )
    n_sessions = cand_rows.get_column("session_id").n_unique() if cand_rows.height else 0
    all_zero_miss = cand_rows.height > 0 and bool(
        (cand_rows.get_column("status") == "DEADLINE_MISS").sum() == 0
    )
    zero_event_bound = (
        zero_event_session_upper_bound(n_sessions) if all_zero_miss and n_sessions >= 1 else None
    )
    return {
        "data_kind": DATA_KIND,
        "baseline": baseline,
        "candidate": candidate,
        "cost_effect_ticks": cost.estimate,
        "cost_ci_lo": cost_lo,
        "cost_ci_hi": cost_hi,
        "cost_n_sessions_defined": cost.n_sessions_defined,
        "cost_n_sessions_undefined": cost.n_sessions_undefined,
        "cost_n_tasks": cost.n_tasks,
        "miss_diff": miss.estimate,
        "miss_ci_lo": miss_lo,
        "miss_ci_hi": miss_hi,
        "miss_n_sessions_defined": miss.n_sessions_defined,
        "miss_n_sessions_undefined": miss.n_sessions_undefined,
        "miss_n_tasks": miss.n_tasks,
        "candidate_all_zero_miss": all_zero_miss,
        "zero_event_upper_bound": zero_event_bound,
    }


def primary_pair_table(
    task_results: pl.DataFrame, bootstrap_n: int, bootstrap_seed: int
) -> pl.DataFrame:
    """Primary pair B3_NO_QUEUE (baseline) vs B3 (candidate) at the primary configuration."""
    rows = _primary_rows(task_results)
    rec = _pair_record(
        rows,
        _PRIMARY_BASELINE,
        _PRIMARY_CANDIDATE,
        bootstrap_n=bootstrap_n,
        bootstrap_seed=bootstrap_seed,
    )
    return pl.DataFrame([rec])


def secondary_pairs_table(
    task_results: pl.DataFrame, bootstrap_n: int, bootstrap_seed: int
) -> pl.DataFrame:
    """Secondary pairs, each on its own pairwise set at the primary configuration."""
    rows = _primary_rows(task_results)
    recs = [
        _pair_record(
            rows, baseline, candidate, bootstrap_n=bootstrap_n, bootstrap_seed=bootstrap_seed
        )
        for baseline, candidate in _SECONDARY_PAIRS
    ]
    return pl.DataFrame(recs)


def per_policy_distribution_table(task_results: pl.DataFrame) -> pl.DataFrame:
    """Per-policy outcome distribution at the primary configuration."""
    rows = _primary_rows(task_results)
    out: list[dict[str, Any]] = []
    if rows.height == 0:
        return pl.DataFrame()
    for policy in sorted(rows.get_column("policy_id").unique().to_list()):
        pr = rows.filter(pl.col("policy_id") == policy)
        n = pr.height
        n_completed = int((pr.get_column("status") == "COMPLETED_ON_TIME").sum())
        n_miss = int((pr.get_column("status") == "DEADLINE_MISS").sum())
        n_uneval = int((pr.get_column("status") == "TECHNICALLY_UNEVALUABLE").sum())
        n_evaluable = n - n_uneval
        completed = pr.filter(pr.get_column("status") == "COMPLETED_ON_TIME")
        passive_frac = (
            _as_float(completed.get_column("passive_filled").mean()) if completed.height else None
        )
        is_vals = completed.get_column("is_ticks_net").drop_nulls()
        mean_is = _as_float(is_vals.mean()) if is_vals.len() else None
        median_is = _as_float(is_vals.median()) if is_vals.len() else None
        miss_rate = (n_miss / n_evaluable) if n_evaluable else None
        out.append(
            {
                "data_kind": DATA_KIND,
                "policy_id": policy,
                "n_tasks": n,
                "n_completed_on_time": n_completed,
                "n_deadline_miss": n_miss,
                "n_technically_unevaluable": n_uneval,
                "passive_fill_fraction": passive_frac,
                "mean_is_ticks_net": mean_is,
                "median_is_ticks_net": median_is,
                "miss_rate": miss_rate,
            }
        )
    return pl.DataFrame(out)


def latency_table(
    task_results: pl.DataFrame, scenarios: Sequence[str], bootstrap_n: int, bootstrap_seed: int
) -> pl.DataFrame:
    """Primary pair effect per scenario (frozen policies)."""
    out: list[dict[str, Any]] = []
    for scenario in scenarios:
        rows = task_results.filter(pl.col("scenario_id") == scenario)
        rec = _pair_record(
            rows,
            _PRIMARY_BASELINE,
            _PRIMARY_CANDIDATE,
            bootstrap_n=bootstrap_n,
            bootstrap_seed=bootstrap_seed,
        )
        rec["scenario_id"] = scenario
        out.append(rec)
    return pl.DataFrame(out)


def epsilon_sensitivity_table(
    decision_log: list[dict[str, Any]],
    epsilon_set: Sequence[float],
    primary_epsilon: float,
) -> pl.DataFrame:
    """Recompute action shares/agreement per epsilon from a stored B3 decision log (no replay).

    For each epsilon we recompute the chosen action from each logged ``(p_hold, p_switch, v_hold,
    v_switch, supported)`` tuple via :func:`risk_allowance_choice`, then report the action share
    and the agreement with the primary epsilon's choice. Realized outcomes for other epsilons
    would require replay and are not claimed (noted in a column).
    """
    out: list[dict[str, Any]] = []
    if not decision_log:
        return pl.DataFrame()

    def choices_for(eps: float) -> list[CheckpointChoice]:
        result: list[CheckpointChoice] = []
        for row in decision_log:
            p_hat = {
                CheckpointChoice.HOLD: float(row["p_hold"]),
                CheckpointChoice.SWITCH: float(row["p_switch"]),
            }
            v_hat = {
                CheckpointChoice.HOLD: float(row["v_hold"]),
                CheckpointChoice.SWITCH: float(row["v_switch"]),
            }
            choice, _ = risk_allowance_choice(p_hat, v_hat, eps, supported=bool(row["supported"]))
            result.append(choice)
        return result

    primary_choices = choices_for(primary_epsilon)
    n = len(decision_log)
    for eps in epsilon_set:
        choices = choices_for(eps)
        n_switch = sum(1 for c in choices if c is CheckpointChoice.SWITCH)
        n_hold = n - n_switch
        agreement = sum(1 for a, b in zip(choices, primary_choices, strict=True) if a is b) / n
        out.append(
            {
                "data_kind": DATA_KIND,
                "epsilon": eps,
                "n_decisions": n,
                "share_hold": n_hold / n,
                "share_switch": n_switch / n,
                "agreement_with_primary": agreement,
                "is_primary_epsilon": math.isclose(eps, primary_epsilon),
                "note": "action shares only; realized outcomes would require replay",
            }
        )
    return pl.DataFrame(out)


def build_all_tables(
    task_results: pl.DataFrame,
    *,
    scenarios: Sequence[str],
    bootstrap_n: int,
    bootstrap_seed: int,
    b3_decision_log: list[dict[str, Any]],
    epsilon_set: Sequence[float],
    primary_epsilon: float,
    support_table: pl.DataFrame,
) -> dict[str, pl.DataFrame]:
    """Build every analysis table and return them keyed by name (stage 9)."""
    return {
        "primary_pair": primary_pair_table(task_results, bootstrap_n, bootstrap_seed),
        "secondary_pairs": secondary_pairs_table(task_results, bootstrap_n, bootstrap_seed),
        "per_policy_distribution": per_policy_distribution_table(task_results),
        "latency_table": latency_table(task_results, scenarios, bootstrap_n, bootstrap_seed),
        "epsilon_sensitivity": epsilon_sensitivity_table(
            b3_decision_log, epsilon_set, primary_epsilon
        ),
        "support_table": support_table,
    }
