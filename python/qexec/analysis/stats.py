"""Paired, session-blocked statistical protocol (product sections 7.3 and 10).

Operates on a :class:`polars.DataFrame` of persisted ``TaskResult`` rows. Required columns:
``task_id``, ``session_id``, ``policy_id``, ``scenario_id``, ``side``, ``status`` (string value of
:class:`qexec.core.tasks.TaskStatus`), plus whichever metric column is requested
(e.g. ``is_ticks_net``, ``c_t_ticks``).

Design decisions, all binding (``docs/engineering_contract.md`` WP-METRICS, product sections
7.3 and 10):

* Pairing key is ``(task_id, scenario_id)``. Buy/sell probes are distinct ``task_id`` values and
  are paired within their session block; they are not independent replicates.
* Common-completion for a cost comparison means **both** policies are ``COMPLETED_ON_TIME``.
* Miss analysis includes all technically evaluable tasks; only ``TECHNICALLY_UNEVALUABLE`` rows
  are excluded.
* Per-session paired differences are the default aggregation unit and **sessions are weighted
  equally** (product 7.3): a 100-task session and a 2-task session count the same.
* A session with no qualifying paired tasks is **undefined**, counted, and excluded from the
  estimate (never treated as zero) (T32).
* The bootstrap resamples whole sessions with replacement jointly so pairing is preserved; it
  returns ``None`` interval endpoints when fewer than two sessions are defined, or when the
  resampled statistics are all identical *and* every underlying session value is zero
  (degenerate: no spread evidence; T50).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

_COMPLETED = "COMPLETED_ON_TIME"
_DEADLINE_MISS = "DEADLINE_MISS"
_TECHNICALLY_UNEVALUABLE = "TECHNICALLY_UNEVALUABLE"

_PAIR_KEY = ("task_id", "scenario_id")


@dataclass(frozen=True, slots=True)
class PairedEffect:
    """Result of a per-session paired comparison.

    Attributes
    ----------
    estimate:
        Equal-weighted mean of the per-session paired statistics over **defined** sessions,
        or ``None`` when no session is defined.
    n_sessions_defined:
        Number of sessions with at least one qualifying paired task.
    n_sessions_undefined:
        Number of sessions present in the data but with no qualifying paired task (counted,
        not treated as zero).
    n_tasks:
        Total number of qualifying paired tasks contributing to the estimate.
    per_session:
        Mapping ``session_id -> per-session statistic`` for defined sessions (sorted by key for
        determinism).
    """

    estimate: float | None
    n_sessions_defined: int
    n_sessions_undefined: int
    n_tasks: int
    per_session: dict[str, float]


def _require_columns(results: pl.DataFrame, columns: tuple[str, ...]) -> None:
    missing = [c for c in columns if c not in results.columns]
    if missing:
        raise ValueError(f"results is missing required columns: {missing}")


def _policy_rows(results: pl.DataFrame, policy: str) -> pl.DataFrame:
    return results.filter(pl.col("policy_id") == policy)


def _validate_pairs(results: pl.DataFrame, baseline: str, candidate: str) -> None:
    """Task identifiers are globally unique; each compared policy contributes one row."""
    if baseline == candidate:
        raise ValueError("compared policies must be distinct")
    rows = results.filter(pl.col("policy_id").is_in([baseline, candidate]))
    keys = [*_PAIR_KEY, "policy_id"]
    if rows.select(keys).is_duplicated().any():
        raise ValueError("duplicate policy task keys would multiply paired evidence")
    metadata = [c for c in ("session_id", "side", "horizon_ns") if c in rows.columns]
    if metadata:
        counts = rows.group_by(list(_PAIR_KEY)).agg(pl.col(metadata).n_unique())
        if counts.filter(pl.any_horizontal(pl.col(metadata) != 1)).height:
            raise ValueError("paired task metadata must agree across policies")


def common_completion_set(
    results: pl.DataFrame,
    baseline: str,
    candidate: str,
) -> pl.DataFrame:
    """Return the paired rows (one per policy) for tasks both policies complete on time.

    The result has the baseline and candidate rows for every ``(task_id, scenario_id)`` on which
    **both** policies are ``COMPLETED_ON_TIME``. Columns are preserved; a ``policy_id`` column
    distinguishes the two rows. Order is deterministic (sorted by pair key then policy).
    """
    _require_columns(results, (*_PAIR_KEY, "policy_id", "status"))
    _validate_pairs(results, baseline, candidate)

    base = _policy_rows(results, baseline).filter(pl.col("status") == _COMPLETED)
    cand = _policy_rows(results, candidate).filter(pl.col("status") == _COMPLETED)

    base_keys = base.select(_PAIR_KEY).unique()
    cand_keys = cand.select(_PAIR_KEY).unique()
    common = base_keys.join(cand_keys, on=list(_PAIR_KEY), how="inner")

    paired = pl.concat(
        [
            base.join(common, on=list(_PAIR_KEY), how="inner"),
            cand.join(common, on=list(_PAIR_KEY), how="inner"),
        ]
    )
    return paired.sort([*_PAIR_KEY, "policy_id"])


def _all_sessions(results: pl.DataFrame, baseline: str, candidate: str) -> list[str]:
    """All session ids present for either compared policy, sorted deterministically."""
    rows = results.filter(pl.col("policy_id").is_in([baseline, candidate]))
    sessions = rows.get_column("session_id").unique().to_list()
    return sorted(sessions)


def _per_session_from_pairs(
    pairs: pl.DataFrame,
    all_sessions: list[str],
    value_col: str,
) -> PairedEffect:
    """Build a :class:`PairedEffect` from per-pair difference rows.

    ``pairs`` must contain ``session_id`` and ``value_col`` (the per-task baseline-candidate
    difference). Sessions in ``all_sessions`` with no row are counted as undefined.
    """
    per_session: dict[str, float] = {}
    n_tasks = 0
    if pairs.height > 0:
        grouped = (
            pairs.group_by("session_id")
            .agg(
                pl.col(value_col).mean().alias("session_mean"),
                pl.len().alias("n"),
            )
            .sort("session_id")
        )
        for session_id, session_mean, n in grouped.iter_rows():
            per_session[str(session_id)] = float(session_mean)
            n_tasks += int(n)

    defined_sessions = set(per_session)
    n_undefined = sum(1 for s in all_sessions if s not in defined_sessions)

    if per_session:
        estimate: float | None = float(np.mean(list(per_session.values())))
    else:
        estimate = None

    return PairedEffect(
        estimate=estimate,
        n_sessions_defined=len(per_session),
        n_sessions_undefined=n_undefined,
        n_tasks=n_tasks,
        per_session=per_session,
    )


def _paired_differences(
    results: pl.DataFrame,
    baseline: str,
    candidate: str,
    metric: str,
) -> pl.DataFrame:
    """Per-task (baseline - candidate) differences on the common-completion set.

    Returns a frame with ``session_id``, ``task_id``, ``scenario_id``, ``diff``.
    """
    common = common_completion_set(results, baseline, candidate)
    if common.filter(pl.col(metric).is_not_null() & ~pl.col(metric).is_finite()).height:
        raise ValueError("completed paired costs must be finite or explicitly missing")
    if common.height == 0:
        return pl.DataFrame(
            schema={
                "session_id": pl.Utf8,
                "task_id": pl.Utf8,
                "scenario_id": pl.Utf8,
                "diff": pl.Float64,
            }
        )

    base = common.filter(pl.col("policy_id") == baseline).select(
        "session_id", *_PAIR_KEY, pl.col(metric).alias("baseline_metric")
    )
    cand = common.filter(pl.col("policy_id") == candidate).select(
        *_PAIR_KEY, pl.col(metric).alias("candidate_metric")
    )
    joined = base.join(cand, on=list(_PAIR_KEY), how="inner")
    return (
        joined.filter(
            pl.col("baseline_metric").is_not_null() & pl.col("candidate_metric").is_not_null()
        )
        .select(
            "session_id",
            *_PAIR_KEY,
            (pl.col("baseline_metric") - pl.col("candidate_metric")).alias("diff"),
        )
        .sort([*_PAIR_KEY])
    )


def session_paired_effect(
    results: pl.DataFrame,
    baseline: str,
    candidate: str,
    metric: str = "is_ticks_net",
) -> PairedEffect:
    """Per-session mean of ``baseline - candidate`` on common-completion tasks.

    Positive favors the candidate (product 7.3: positive ``IS_baseline - IS_candidate`` favors
    the candidate). Sessions with no common-completion task are undefined and counted. Defined
    sessions are weighted equally.
    """
    _require_columns(results, (*_PAIR_KEY, "session_id", "policy_id", "status", metric))
    diffs = _paired_differences(results, baseline, candidate, metric)
    all_sessions = _all_sessions(results, baseline, candidate)
    return _per_session_from_pairs(diffs, all_sessions, "diff")


def paired_miss_difference(
    results: pl.DataFrame,
    baseline: str,
    candidate: str,
) -> PairedEffect:
    """Per-session paired deadline-miss-rate difference on all technically evaluable tasks.

    For each ``(task_id, scenario_id)`` present and technically evaluable for **both** policies,
    the per-task value is ``miss_candidate - miss_baseline`` where ``miss = 1`` iff the status is
    ``DEADLINE_MISS``. **Positive means the candidate is worse** (product 7.3). Only
    ``TECHNICALLY_UNEVALUABLE`` rows are excluded. Sessions with no such paired task are
    undefined and counted; defined sessions are weighted equally.
    """
    _require_columns(results, (*_PAIR_KEY, "session_id", "policy_id", "status"))
    _validate_pairs(results, baseline, candidate)

    def evaluable(policy: str) -> pl.DataFrame:
        return (
            _policy_rows(results, policy)
            .filter(pl.col("status") != _TECHNICALLY_UNEVALUABLE)
            .select(
                "session_id",
                *_PAIR_KEY,
                (pl.col("status") == _DEADLINE_MISS).cast(pl.Int8).alias("miss"),
            )
        )

    base = evaluable(baseline).rename({"miss": "baseline_miss"})
    cand = evaluable(candidate).rename({"miss": "candidate_miss"}).drop("session_id")

    all_sessions = _all_sessions(results, baseline, candidate)

    if base.height == 0 or cand.height == 0:
        empty = pl.DataFrame(
            schema={
                "session_id": pl.Utf8,
                "task_id": pl.Utf8,
                "scenario_id": pl.Utf8,
                "diff": pl.Float64,
            }
        )
        return _per_session_from_pairs(empty, all_sessions, "diff")

    joined = base.join(cand, on=list(_PAIR_KEY), how="inner")
    diffs = joined.select(
        "session_id",
        *_PAIR_KEY,
        (pl.col("candidate_miss") - pl.col("baseline_miss")).cast(pl.Float64).alias("diff"),
    ).sort([*_PAIR_KEY])
    return _per_session_from_pairs(diffs, all_sessions, "diff")


def session_block_bootstrap(  # noqa: PLR0917  (signature fixed by engineering contract WP-METRICS)
    results: pl.DataFrame,
    baseline: str,
    candidate: str,
    statistic: str = "cost",
    n_boot: int = 10_000,
    seed: int = 0,
    alpha: float = 0.05,
    metric: str = "is_ticks_net",
) -> tuple[float | None, float | None]:
    """Session-block percentile bootstrap interval for a paired effect.

    Whole sessions are resampled with replacement jointly for both policies, so the within-task
    pairing is preserved. The statistic is the equal-weighted mean of the per-session paired
    values (same quantity as :func:`session_paired_effect` / :func:`paired_miss_difference`).

    Parameters
    ----------
    statistic:
        ``"cost"`` for the common-completion metric effect, ``"miss"`` for the miss-rate
        difference.
    n_boot:
        Number of bootstrap resamples.
    seed:
        Seed for a :class:`numpy.random.Generator` (deterministic output).
    alpha:
        Two-sided level; the interval is the ``[alpha/2, 1 - alpha/2]`` percentile band.
    metric:
        Metric column for ``statistic="cost"``.

    Returns
    -------
    tuple[float | None, float | None]
        ``(lo, hi)``. Returns ``(None, None)`` when fewer than two sessions are defined, or when
        the degenerate all-zero condition holds (every defined-session value is zero and all
        resampled statistics are identical) so that a zero-width interval is never presented as a
        zero-risk claim (product 7.3, T50).
    """
    if n_boot <= 0:
        raise ValueError(f"n_boot must be positive, got {n_boot}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")

    if statistic == "cost":
        per_session = session_paired_effect(results, baseline, candidate, metric).per_session
    elif statistic == "miss":
        per_session = paired_miss_difference(results, baseline, candidate).per_session
    else:
        raise ValueError(f"statistic must be 'cost' or 'miss', got {statistic!r}")

    session_ids = sorted(per_session)
    values = np.array([per_session[s] for s in session_ids], dtype=np.float64)
    n_defined = values.size

    # Fewer than two defined sessions: cannot estimate spread (product 10).
    if n_defined < 2:
        return (None, None)

    rng = np.random.default_rng(seed)
    # Resample whole sessions with replacement; each resample's statistic is the equal-weighted
    # mean of the chosen sessions' paired values (pairing preserved by session-level resampling).
    idx = rng.integers(0, n_defined, size=(n_boot, n_defined))
    boot_stats = values[idx].mean(axis=1)

    all_zero = bool(np.all(values == 0.0))
    identical = bool(np.all(boot_stats == boot_stats[0]))
    # Degenerate: no spread AND the underlying observations are all exactly zero. A zero-width
    # interval here is not evidence of zero risk; report no interval instead (T50).
    if identical and all_zero:
        return (None, None)

    lo = float(np.quantile(boot_stats, alpha / 2.0, method="linear"))
    hi = float(np.quantile(boot_stats, 1.0 - alpha / 2.0, method="linear"))
    return (lo, hi)


def zero_event_session_upper_bound(n_sessions: int, alpha: float = 0.05) -> float:
    r"""Rule-of-three-style upper bound on a per-session event rate given zero observed events.

    With ``D >= 1`` independent sessions and zero observed events, the one-sided upper confidence
    bound on the per-session event probability at level ``alpha`` is

    .. math:: 1 - \alpha^{1/D}.

    Zero observed failures do not establish zero risk (product 7.3); this bound quantifies the
    residual risk the data cannot rule out.
    """
    if n_sessions < 1:
        raise ValueError(f"n_sessions (D) must be >= 1, got {n_sessions}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    return 1.0 - float(alpha ** (1.0 / n_sessions))
