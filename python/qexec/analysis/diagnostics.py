"""Descriptive post-fill markouts; never part of the execution-cost objective.

References use the last committed state at the exact target, valid trading status,
source continuity across the window, coverage, and a predeclared maximum age.
Missing targets are retained and never replaced with a later convenient quote.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import polars as pl

from qexec.analysis.metrics import markout_ticks
from qexec.core.config import MARKOUT_HORIZONS_NS, S
from qexec.labels.price import MidSeries

MARKOUT_MAX_AGE_NS = S

_SCHEMA = {
    "data_kind": pl.Utf8,
    "task_id": pl.Utf8,
    "session_id": pl.Utf8,
    "policy_id": pl.Utf8,
    "scenario_id": pl.Utf8,
    "horizon_ns": pl.Int64,
    "side": pl.Int64,
    "fill_mechanism": pl.Utf8,
    "markout_horizon_ns": pl.Int64,
    "target_time_ns": pl.Int64,
    "reference_time_ns": pl.Int64,
    "reference_age_ns": pl.Int64,
    "max_reference_age_ns": pl.Int64,
    "markout_ticks": pl.Float64,
    "missing_reason": pl.Utf8,
}
_GROUPS = ("scenario_id", "horizon_ns", "policy_id", "side", "fill_mechanism", "markout_horizon_ns")


def markouts_for_session(
    task_results: pl.DataFrame,
    series: MidSeries,
    tick_size_fixed: int,
    horizons_ns: Sequence[int] = MARKOUT_HORIZONS_NS,
    *,
    max_age_ns: int = MARKOUT_MAX_AGE_NS,
) -> pl.DataFrame:
    """One diagnostic row per evaluable completed task and requested post-fill horizon."""
    if tick_size_fixed <= 0 or max_age_ns < 0:
        raise ValueError("positive tick size and nonnegative maximum reference age required")
    if (
        not horizons_ns
        or any(h <= 0 for h in horizons_ns)
        or len(set(horizons_ns)) != len(horizons_ns)
    ):
        raise ValueError("markout horizons must be distinct positive values")
    rows: list[dict[str, Any]] = []  # Persisted artifact rows have mixed scalar field types.
    if task_results.height == 0:
        return pl.DataFrame(schema=_SCHEMA)
    completed = task_results.filter(pl.col("status") == "COMPLETED_ON_TIME")
    for task in completed.iter_rows(named=True):
        if task["completion_time_ns"] is None or task["avg_price_fixed"] is None:
            raise ValueError("completed task is missing its execution time or price")
        fill_time = int(task["completion_time_ns"])
        price = float(task["avg_price_fixed"])
        if not math.isfinite(price) or not price.is_integer():
            raise ValueError("one-contract execution price must be an exact integer")
        for horizon in horizons_ns:
            target = fill_time + horizon
            missing: str | None = None
            reference_time: int | None = None
            age: int | None = None
            value: float | None = None
            if series.last_sample_time_ns is None or target > series.last_sample_time_ns:
                missing = "OUTSIDE_COVERAGE"
            elif not series.source_window_valid(fill_time, target):
                missing = "SOURCE_DISCONTINUITY"
            elif series.mid_at(target) is None:
                missing = "INVALID_STATE"
            else:
                lookup = series.last_valid_mid_at(target)
                assert lookup is not None
                mid, reference_time = lookup
                age = target - reference_time
                if age > max_age_ns:
                    missing = "STALE_REFERENCE"
                else:
                    value = markout_ticks(int(task["side"]), int(price), mid, tick_size_fixed)
            rows.append(
                {
                    "data_kind": "SYNTHETIC",
                    "task_id": task["task_id"],
                    "session_id": task["session_id"],
                    "policy_id": task["policy_id"],
                    "scenario_id": task["scenario_id"],
                    "horizon_ns": task["horizon_ns"],
                    "side": task["side"],
                    "fill_mechanism": task["fill_mechanism"],
                    "markout_horizon_ns": horizon,
                    "target_time_ns": target,
                    "reference_time_ns": reference_time,
                    "reference_age_ns": age,
                    "max_reference_age_ns": max_age_ns,
                    "markout_ticks": value,
                    "missing_reason": missing,
                }
            )
    return pl.DataFrame(rows, schema=_SCHEMA).sort(["task_id", "policy_id", "markout_horizon_ns"])


def summarize_markouts(rows: pl.DataFrame) -> pl.DataFrame:
    """Task-weighted descriptive means with defined/missing counts and explicit regimes."""
    return (
        rows.group_by(list(_GROUPS))
        .agg(
            pl.len().alias("n_completed"),
            pl.col("markout_ticks").count().alias("n_defined"),
            pl.col("markout_ticks").null_count().alias("n_missing"),
            pl.col("session_id").n_unique().alias("n_sessions"),
            pl.col("markout_ticks").mean().alias("mean_markout_ticks"),
            pl.col("markout_ticks").median().alias("median_markout_ticks"),
        )
        .with_columns(pl.lit("SYNTHETIC").alias("data_kind"))
        .sort(list(_GROUPS))
    )
