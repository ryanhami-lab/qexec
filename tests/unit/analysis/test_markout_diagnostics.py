"""Hand-worked, deadline-independent descriptive markouts and missing references."""

from __future__ import annotations

import polars as pl

from qexec.analysis.diagnostics import markouts_for_session, summarize_markouts
from qexec.labels.price import MidSeries


def _completed() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "task_id": ["buy", "sell"],
            "session_id": ["s", "s"],
            "policy_id": ["B1", "B1"],
            "scenario_id": ["L1", "L1"],
            "horizon_ns": [1000, 1000],
            "side": [1, -1],
            "status": ["COMPLETED_ON_TIME", "COMPLETED_ON_TIME"],
            "completion_time_ns": [0, 0],
            "avg_price_fixed": [100.0, 100.0],
            "fill_mechanism": ["QUEUE_DEPLETION", "QUEUE_DEPLETION"],
        }
    )


def test_markout_signs_and_missing_horizons_are_hand_derived() -> None:
    series = MidSeries([0, 100, 150, 220], [200, 204, None, 208])
    rows = markouts_for_session(_completed(), series, 10, (100, 200, 300), max_age_ns=100)
    buy = rows.filter(pl.col("task_id") == "buy").sort("markout_horizon_ns")
    sell = rows.filter(pl.col("task_id") == "sell").sort("markout_horizon_ns")
    # midpoint 102, fill 100, tick 10: BUY +0.2 ticks, SELL -0.2 ticks.
    assert buy.get_column("markout_ticks").to_list() == [0.2, None, None]
    assert sell.get_column("markout_ticks").to_list() == [-0.2, None, None]
    assert buy.get_column("missing_reason").to_list() == [None, "INVALID_STATE", "OUTSIDE_COVERAGE"]
    # Never replace invalid target=200 with the convenient valid sample at 220.
    assert buy.get_column("reference_time_ns").to_list() == [100, None, None]


def test_markout_freshness_is_explicit() -> None:
    rows = markouts_for_session(
        _completed(), MidSeries([0, 100, 200], [200, 204, 206]), 10, (125,), max_age_ns=10
    )
    assert rows.get_column("markout_ticks").null_count() == 2
    assert rows.get_column("missing_reason").to_list() == ["STALE_REFERENCE"] * 2


def test_markout_summary_counts_missing_observations() -> None:
    rows = markouts_for_session(
        _completed(),
        MidSeries([0, 100, 150, 220], [200, 204, None, 208]),
        10,
        (100, 200),
        max_age_ns=100,
    )
    summary = summarize_markouts(rows)
    valid = summary.filter((pl.col("side") == 1) & (pl.col("markout_horizon_ns") == 100))
    assert valid.get_column("mean_markout_ticks").item() == 0.2
    missing = summary.filter((pl.col("side") == 1) & (pl.col("markout_horizon_ns") == 200))
    assert missing.get_column("n_defined").item() == 0
    assert missing.get_column("n_missing").item() == 1
    assert missing.get_column("mean_markout_ticks").item() is None
