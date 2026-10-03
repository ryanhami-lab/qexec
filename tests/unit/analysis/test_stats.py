"""Statistical-protocol tests: T32, T49, T50, bootstrap pairing/determinism, equal weighting.

Golden values are hand-derived; expected numbers appear in comments.
"""

from __future__ import annotations

import polars as pl

from qexec.analysis.stats import (
    PairedEffect,
    common_completion_set,
    paired_miss_difference,
    session_block_bootstrap,
    session_paired_effect,
    zero_event_session_upper_bound,
)

COMPLETED = "COMPLETED_ON_TIME"
MISS = "DEADLINE_MISS"
UNEVAL = "TECHNICALLY_UNEVALUABLE"


def _row(
    task_id: str,
    session_id: str,
    policy_id: str,
    status: str,
    is_ticks_net: float | None,
    *,
    scenario_id: str = "L1",
    c_t_ticks: float | None = None,
    side: int = 1,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "session_id": session_id,
        "policy_id": policy_id,
        "scenario_id": scenario_id,
        "side": side,
        "status": status,
        "is_ticks_net": is_ticks_net,
        "c_t_ticks": c_t_ticks,
    }


def _frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    schema = {
        "task_id": pl.Utf8,
        "session_id": pl.Utf8,
        "policy_id": pl.Utf8,
        "scenario_id": pl.Utf8,
        "side": pl.Int64,
        "status": pl.Utf8,
        "is_ticks_net": pl.Float64,
        "c_t_ticks": pl.Float64,
    }
    return pl.DataFrame(rows, schema=schema)


# --------------------------------------------------------------------------------------
# common_completion_set.
# --------------------------------------------------------------------------------------


def test_common_completion_requires_both_completed() -> None:
    rows = [
        # t1: both completed -> included.
        _row("t1", "s1", "B3", COMPLETED, 1.0),
        _row("t1", "s1", "B3_NO_QUEUE", COMPLETED, 2.0),
        # t2: baseline misses -> excluded.
        _row("t2", "s1", "B3", MISS, None),
        _row("t2", "s1", "B3_NO_QUEUE", COMPLETED, 1.0),
        # t3: candidate uneval -> excluded.
        _row("t3", "s1", "B3", COMPLETED, 0.5),
        _row("t3", "s1", "B3_NO_QUEUE", UNEVAL, None),
    ]
    common = common_completion_set(_frame(rows), "B3", "B3_NO_QUEUE")
    task_ids = set(common.get_column("task_id").to_list())
    assert task_ids == {"t1"}
    assert common.height == 2  # one row per policy


# --------------------------------------------------------------------------------------
# session_paired_effect: equal weighting + hand-computed example.
# --------------------------------------------------------------------------------------


def test_equal_session_weighting_hand_computed() -> None:
    # Session A: 2 tasks, baseline - candidate diffs = +4 and +6 -> session mean = +5.
    # Session B: 100 tasks, each diff = -1 -> session mean = -1.
    # Equal session weighting -> estimate = (5 + (-1)) / 2 = 2.0 (NOT task-weighted,
    # which would be (4+6 + 100*(-1)) / 102 = -90/102 ~= -0.882).
    rows: list[dict[str, object]] = []
    # Session A, task a1: baseline 10, candidate 6 -> diff +4.
    rows += [
        _row("a1", "A", "B3", COMPLETED, 10.0),
        _row("a1", "A", "B3_NO_QUEUE", COMPLETED, 6.0),
    ]
    # Session A, task a2: baseline 9, candidate 3 -> diff +6.
    rows += [
        _row("a2", "A", "B3", COMPLETED, 9.0),
        _row("a2", "A", "B3_NO_QUEUE", COMPLETED, 3.0),
    ]
    # Session B, 100 tasks: baseline 0, candidate 1 -> diff -1 each.
    for i in range(100):
        tid = f"b{i}"
        rows += [
            _row(tid, "B", "B3", COMPLETED, 0.0),
            _row(tid, "B", "B3_NO_QUEUE", COMPLETED, 1.0),
        ]
    effect = session_paired_effect(_frame(rows), "B3", "B3_NO_QUEUE", metric="is_ticks_net")
    assert effect.estimate == 2.0
    assert effect.per_session == {"A": 5.0, "B": -1.0}
    assert effect.n_sessions_defined == 2
    assert effect.n_sessions_undefined == 0
    assert effect.n_tasks == 102


def test_positive_estimate_favours_candidate() -> None:
    # baseline IS higher than candidate -> positive diff favours candidate.
    rows = [
        _row("t1", "s1", "B3", COMPLETED, 3.0),
        _row("t1", "s1", "B3_NO_QUEUE", COMPLETED, 1.0),
    ]
    effect = session_paired_effect(_frame(rows), "B3", "B3_NO_QUEUE")
    assert effect.estimate == 2.0  # 3 - 1 favours candidate (B3_NO_QUEUE)


# --------------------------------------------------------------------------------------
# T32: no common completion -> undefined effect, counted (not zero).
# --------------------------------------------------------------------------------------


def test_t32_no_common_completion_is_undefined_and_counted() -> None:
    rows = [
        # s1: baseline completes, candidate misses -> no common completion.
        _row("t1", "s1", "B3", COMPLETED, 1.0),
        _row("t1", "s1", "B3_NO_QUEUE", MISS, None),
        # s2: candidate completes, baseline misses -> no common completion.
        _row("t2", "s2", "B3", MISS, None),
        _row("t2", "s2", "B3_NO_QUEUE", COMPLETED, 2.0),
    ]
    effect = session_paired_effect(_frame(rows), "B3", "B3_NO_QUEUE")
    assert effect.estimate is None  # undefined, NOT zero
    assert effect.n_sessions_defined == 0
    assert effect.n_sessions_undefined == 2  # both sessions counted as undefined
    assert effect.n_tasks == 0
    assert effect.per_session == {}


def test_undefined_session_counted_alongside_defined() -> None:
    rows = [
        # s1 defined.
        _row("t1", "s1", "B3", COMPLETED, 2.0),
        _row("t1", "s1", "B3_NO_QUEUE", COMPLETED, 1.0),
        # s2 undefined (candidate miss).
        _row("t2", "s2", "B3", COMPLETED, 5.0),
        _row("t2", "s2", "B3_NO_QUEUE", MISS, None),
    ]
    effect = session_paired_effect(_frame(rows), "B3", "B3_NO_QUEUE")
    assert effect.estimate == 1.0  # only s1 contributes
    assert effect.n_sessions_defined == 1
    assert effect.n_sessions_undefined == 1


# --------------------------------------------------------------------------------------
# T49: adding a third policy's rows leaves the B3 vs B3_NO_QUEUE estimate identical.
# --------------------------------------------------------------------------------------


def test_t49_third_policy_does_not_change_estimate() -> None:
    base_rows = [
        _row("t1", "s1", "B3", COMPLETED, 2.0),
        _row("t1", "s1", "B3_NO_QUEUE", COMPLETED, 1.0),
        _row("t2", "s2", "B3", COMPLETED, 4.0),
        _row("t2", "s2", "B3_NO_QUEUE", COMPLETED, 1.0),
    ]
    before = session_paired_effect(_frame(base_rows), "B3", "B3_NO_QUEUE")

    extra_rows = [
        *base_rows,
        # A third policy (B2), including a row that completes on the same tasks.
        _row("t1", "s1", "B2", COMPLETED, 99.0),
        _row("t2", "s2", "B2", MISS, None),
        _row("t1", "s1", "B0", COMPLETED, -50.0),
    ]
    after = session_paired_effect(_frame(extra_rows), "B3", "B3_NO_QUEUE")

    assert before == after
    assert after.estimate == before.estimate
    assert after.n_tasks == before.n_tasks
    assert after.per_session == before.per_session

    # Bootstrap interval is also unchanged by the extra policy rows.
    iv_before = session_block_bootstrap(_frame(base_rows), "B3", "B3_NO_QUEUE", "cost", 2000, 7)
    iv_after = session_block_bootstrap(_frame(extra_rows), "B3", "B3_NO_QUEUE", "cost", 2000, 7)
    assert iv_before == iv_after


# --------------------------------------------------------------------------------------
# paired_miss_difference: only TECHNICALLY_UNEVALUABLE excluded; positive = candidate worse.
# --------------------------------------------------------------------------------------


def test_paired_miss_difference_positive_candidate_worse() -> None:
    rows = [
        # s1: baseline completes (miss 0), candidate misses (miss 1) -> diff +1 (candidate worse).
        _row("t1", "s1", "B3", COMPLETED, 1.0),
        _row("t1", "s1", "B3_NO_QUEUE", MISS, None),
        # s1 t2: both complete -> diff 0.
        _row("t2", "s1", "B3", COMPLETED, 1.0),
        _row("t2", "s1", "B3_NO_QUEUE", COMPLETED, 1.0),
    ]
    effect = paired_miss_difference(_frame(rows), "B3", "B3_NO_QUEUE")
    # session s1 mean diff = (1 + 0)/2 = 0.5
    assert effect.per_session == {"s1": 0.5}
    assert effect.estimate == 0.5
    assert effect.n_tasks == 2


def test_paired_miss_difference_excludes_only_uneval() -> None:
    rows = [
        # t1 technically unevaluable for candidate -> excluded entirely from pairing.
        _row("t1", "s1", "B3", MISS, None),
        _row("t1", "s1", "B3_NO_QUEUE", UNEVAL, None),
        # t2 both evaluable: baseline miss (1), candidate complete (0) -> diff -1.
        _row("t2", "s1", "B3", MISS, None),
        _row("t2", "s1", "B3_NO_QUEUE", COMPLETED, 1.0),
    ]
    effect = paired_miss_difference(_frame(rows), "B3", "B3_NO_QUEUE")
    assert effect.n_tasks == 1  # only t2 pairs
    assert effect.per_session == {"s1": -1.0}  # candidate better here


# --------------------------------------------------------------------------------------
# Bootstrap: pairing (permuting policy labels within a task flips the sign), determinism.
# --------------------------------------------------------------------------------------


def _bootstrap_fixture() -> list[dict[str, object]]:
    # 4 sessions, one task each, clear positive baseline-candidate differences.
    diffs = {"s1": (3.0, 1.0), "s2": (4.0, 1.0), "s3": (2.5, 0.5), "s4": (5.0, 2.0)}
    rows: list[dict[str, object]] = []
    for sess, (b, c) in diffs.items():
        rows += [
            _row(f"t_{sess}", sess, "B3", COMPLETED, b),
            _row(f"t_{sess}", sess, "B3_NO_QUEUE", COMPLETED, c),
        ]
    return rows


def test_bootstrap_seed_determinism() -> None:
    rows = _frame(_bootstrap_fixture())
    iv1 = session_block_bootstrap(rows, "B3", "B3_NO_QUEUE", "cost", 5000, seed=123)
    iv2 = session_block_bootstrap(rows, "B3", "B3_NO_QUEUE", "cost", 5000, seed=123)
    # Same seed -> byte-identical interval (determinism is the binding guarantee).
    assert iv1 == iv2
    lo, hi = iv1
    assert lo is not None and hi is not None
    assert lo <= hi
    # All diffs are strictly positive -> the interval lies strictly above zero.
    assert lo > 0.0
    # The point estimate (equal-weighted mean of per-session diffs) lies within the interval.
    point = session_paired_effect(rows, "B3", "B3_NO_QUEUE").estimate
    assert point is not None
    assert lo <= point <= hi


def test_bootstrap_seed_sensitivity_rich_fixture() -> None:
    # With many sessions of varied per-session values, distinct seeds yield distinct intervals,
    # confirming the Generator seed actually drives resampling (not a fixed computation).
    rows: list[dict[str, object]] = []
    for i in range(20):
        sess = f"s{i:02d}"
        # Spread the per-session diffs across a range so percentile endpoints vary with the draw.
        base_val = 1.0 + i  # candidate fixed at 0.0 -> diff = base_val
        rows += [
            _row(f"t_{sess}", sess, "B3", COMPLETED, base_val),
            _row(f"t_{sess}", sess, "B3_NO_QUEUE", COMPLETED, 0.0),
        ]
    frame = _frame(rows)
    iv_a = session_block_bootstrap(frame, "B3", "B3_NO_QUEUE", "cost", 5000, seed=1)
    iv_b = session_block_bootstrap(frame, "B3", "B3_NO_QUEUE", "cost", 5000, seed=2)
    assert iv_a != iv_b  # different seeds -> different resampled intervals
    # Determinism within a seed still holds.
    assert iv_a == session_block_bootstrap(frame, "B3", "B3_NO_QUEUE", "cost", 5000, seed=1)


def test_bootstrap_pairing_label_swap_flips_sign() -> None:
    rows = _bootstrap_fixture()
    frame = _frame(rows)
    iv = session_block_bootstrap(frame, "B3", "B3_NO_QUEUE", "cost", 5000, seed=42)

    # Swap policy labels within each task (preserve pairing). The paired difference
    # baseline - candidate must flip sign, so the interval reflects negated bounds.
    swapped = frame.with_columns(
        pl.when(pl.col("policy_id") == "B3")
        .then(pl.lit("B3_NO_QUEUE"))
        .otherwise(pl.lit("B3"))
        .alias("policy_id")
    )
    iv_swapped = session_block_bootstrap(swapped, "B3", "B3_NO_QUEUE", "cost", 5000, seed=42)

    lo, hi = iv
    lo_s, hi_s = iv_swapped
    assert lo is not None and hi is not None and lo_s is not None and hi_s is not None
    # Swapping labels negates each per-session diff; with the same seed the resample indices are
    # identical, so the swapped interval is exactly the negation reversed.
    assert lo_s == -hi
    assert hi_s == -lo
    # Original diffs are strictly positive (favouring candidate) -> interval above zero;
    # after the label swap the paired difference flips sign -> interval below zero.
    assert lo > 0.0  # original interval strictly positive
    assert hi_s < 0.0  # swapped interval strictly negative


def test_bootstrap_fewer_than_two_sessions_returns_none() -> None:
    rows = [
        _row("t1", "s1", "B3", COMPLETED, 2.0),
        _row("t1", "s1", "B3_NO_QUEUE", COMPLETED, 1.0),
    ]
    iv = session_block_bootstrap(_frame(rows), "B3", "B3_NO_QUEUE", "cost", 1000, 1)
    assert iv == (None, None)  # only one defined session


# --------------------------------------------------------------------------------------
# T50: all-zero misses -> bound reported and no zero-width interval claimed.
# --------------------------------------------------------------------------------------


def test_t50_all_zero_misses_no_degenerate_interval() -> None:
    # Multiple sessions, all tasks complete for both policies -> every per-session miss diff is 0.
    rows: list[dict[str, object]] = []
    for sess in ("s1", "s2", "s3"):
        for i in range(3):
            tid = f"{sess}_t{i}"
            rows += [
                _row(tid, sess, "B3", COMPLETED, 1.0),
                _row(tid, sess, "B3_NO_QUEUE", COMPLETED, 1.0),
            ]
    frame = _frame(rows)
    miss = paired_miss_difference(frame, "B3", "B3_NO_QUEUE")
    # All sessions defined, all per-session diffs exactly zero.
    assert miss.n_sessions_defined == 3
    assert all(v == 0.0 for v in miss.per_session.values())

    # Degenerate all-zero: the bootstrap must NOT present a zero-width interval as a zero-risk
    # claim; it returns (None, None).
    iv = session_block_bootstrap(frame, "B3", "B3_NO_QUEUE", "miss", 5000, seed=5)
    assert iv == (None, None)

    # Instead, the zero-event session upper bound reports the residual risk from D sessions.
    bound = zero_event_session_upper_bound(n_sessions=3, alpha=0.05)
    # 1 - 0.05**(1/3) = 1 - 0.3684... = 0.6315...
    assert abs(bound - (1.0 - 0.05 ** (1.0 / 3.0))) < 1e-15
    assert 0.0 < bound < 1.0


def test_t50_nonzero_spread_still_gives_interval() -> None:
    # If misses are not all zero, a genuine (possibly zero-containing) interval is produced.
    rows = [
        _row("t1", "s1", "B3", COMPLETED, 1.0),
        _row("t1", "s1", "B3_NO_QUEUE", MISS, None),  # s1 diff +1
        _row("t2", "s2", "B3", COMPLETED, 1.0),
        _row("t2", "s2", "B3_NO_QUEUE", COMPLETED, 1.0),  # s2 diff 0
        _row("t3", "s3", "B3", COMPLETED, 1.0),
        _row("t3", "s3", "B3_NO_QUEUE", COMPLETED, 1.0),  # s3 diff 0
    ]
    iv = session_block_bootstrap(_frame(rows), "B3", "B3_NO_QUEUE", "miss", 5000, seed=3)
    lo, hi = iv
    assert lo is not None and hi is not None  # not degenerate (values not all zero)
    assert lo <= hi


# --------------------------------------------------------------------------------------
# zero_event_session_upper_bound.
# --------------------------------------------------------------------------------------


def test_zero_event_bound_values() -> None:
    # D=1: 1 - alpha ** 1 = 1 - 0.05 = 0.95.
    assert abs(zero_event_session_upper_bound(1, 0.05) - 0.95) < 1e-15
    # Monotone decreasing in D.
    b1 = zero_event_session_upper_bound(1, 0.05)
    b10 = zero_event_session_upper_bound(10, 0.05)
    b100 = zero_event_session_upper_bound(100, 0.05)
    assert b1 > b10 > b100 > 0.0


def test_paired_effect_is_frozen() -> None:
    eff = PairedEffect(1.0, 2, 1, 5, {"s1": 1.0})
    try:
        eff.estimate = 2.0  # type: ignore[misc]  # frozen dataclass should reject assignment
    except AttributeError:
        pass
    else:
        raise AssertionError("PairedEffect must be frozen")
