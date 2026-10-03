"""G2-S support-gate selection tests (research spec T59): the selection reads support counts
only, so cost/markout columns cannot change the chosen horizon, and the frozen rule picks the
shortest horizon meeting both minima."""

from __future__ import annotations

import polars as pl

from qexec.analysis.support import select_support_horizon


def _support(session_counts: dict[str, tuple[int, int]]) -> pl.DataFrame:
    """Build a support frame: session_id -> (decision_eligible, queue_depletion)."""
    rows = [
        {
            "session_id": sid,
            "decision_eligible_checkpoints": elig,
            "post_checkpoint_passive_fills": elig,
            "post_checkpoint_queue_depletion": depl,
            "post_checkpoint_trade_through": 0,
            "fills_before_checkpoint": 0,
        }
        for sid, (elig, depl) in session_counts.items()
    ]
    return pl.DataFrame(rows)


def test_shortest_passing_horizon_is_chosen() -> None:
    # H=1s fails (session B has 2 < 5 eligible); H=5s passes both minima -> choose 5s.
    by_h = {
        1_000_000_000: _support({"A": (10, 3), "B": (2, 3)}),
        5_000_000_000: _support({"A": (10, 3), "B": (8, 2)}),
    }
    result = select_support_horizon(
        by_h, ["A", "B"], support_min_eligible=5, support_min_queue_depletion=1
    )
    assert result.verdict == "PASSED"
    assert result.chosen_horizon_ns == 5_000_000_000
    # The 1s horizon is logged as failing with the correct minima.
    h1 = next(h for h in result.horizons if h.horizon_ns == 1_000_000_000)
    assert not h1.passes
    assert h1.min_eligible == 2


def test_failed_verdict_when_no_horizon_passes() -> None:
    by_h = {
        1_000_000_000: _support({"A": (1, 0)}),
        5_000_000_000: _support({"A": (2, 0)}),
    }
    result = select_support_horizon(
        by_h, ["A"], support_min_eligible=5, support_min_queue_depletion=1
    )
    assert result.verdict == "FAILED"
    assert result.chosen_horizon_ns is None


def test_missing_pilot_session_counts_as_zero() -> None:
    # Session B has no row at this horizon -> treated as zero, cannot pass.
    by_h = {1_000_000_000: _support({"A": (10, 3)})}
    result = select_support_horizon(
        by_h, ["A", "B"], support_min_eligible=5, support_min_queue_depletion=1
    )
    assert result.verdict == "FAILED"
    h = result.horizons[0]
    assert h.min_eligible == 0
    assert h.per_session_eligible == {"A": 10, "B": 0}


def test_selection_ignores_cost_and_markout_columns() -> None:
    """T59: adding or changing cost/markout columns cannot change the selection, because the
    gate reads only ``decision_eligible_checkpoints`` and ``post_checkpoint_queue_depletion``."""
    base = _support({"A": (10, 3), "B": (8, 2)})
    chosen_base = select_support_horizon(
        {1_000_000_000: base}, ["A", "B"], support_min_eligible=5, support_min_queue_depletion=1
    )
    # Add wildly different cost/markout columns; the counts are unchanged.
    perturbed = base.with_columns(
        pl.Series("c_t_mean", [999.0, -999.0]),
        pl.Series("markout_1s", [12.3, -45.6]),
        pl.Series("is_ticks_net", [1.0, 2.0]),
    )
    chosen_perturbed = select_support_horizon(
        {1_000_000_000: perturbed},
        ["A", "B"],
        support_min_eligible=5,
        support_min_queue_depletion=1,
    )
    assert chosen_base.verdict == chosen_perturbed.verdict == "PASSED"
    assert chosen_base.chosen_horizon_ns == chosen_perturbed.chosen_horizon_ns
    assert chosen_base.horizons[0].min_eligible == chosen_perturbed.horizons[0].min_eligible
