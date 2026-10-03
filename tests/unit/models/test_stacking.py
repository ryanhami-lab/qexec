"""T44: forward-chained out-of-fold scoring never uses an in-sample or future-trained score.

Verified by a fit_fn that records exactly which session indices it was given.
"""

from __future__ import annotations

import numpy as np

from qexec.models.stacking import forward_chained_oof_scores


def test_fit_only_sees_strictly_earlier_sessions() -> None:
    sessions = ["s0", "s1", "s2", "s3"]
    seen: list[tuple[int, ...]] = []

    def fit_fn(all_sessions, train_indices):  # type: ignore[no-untyped-def]
        seen.append(tuple(train_indices))
        return tuple(train_indices)  # the "model" is just the index set it trained on

    def predict_fn(model, session):  # type: ignore[no-untyped-def]
        # Returns one score per row; here one row per session, value = max train index + 1.
        return np.array([float(max(model) + 1)]) if model else np.array([np.nan])

    result = forward_chained_oof_scores(sessions, fit_fn, predict_fn)

    # Session 0 has no prior data -> unscored, fit_fn not called for it.
    assert result.unscored_sessions == (0,)
    assert result.scored_sessions == (1, 2, 3)
    # Every training set used only strictly-earlier indices (no self, no future).
    for k, train_indices in enumerate(result.train_index_log):
        for idx in train_indices:
            assert idx < k
    assert seen == [(0,), (0, 1), (0, 1, 2)]
    # Unscored session carries an empty score array.
    assert result.scores[0].size == 0
    # Scored sessions carry one score each.
    assert result.scores[1].tolist() == [1.0]
    assert result.scores[2].tolist() == [2.0]
    assert result.scores[3].tolist() == [3.0]


def test_purge_gap_excludes_recent_sessions() -> None:
    sessions = list(range(5))
    logs: list[tuple[int, ...]] = []

    def fit_fn(all_sessions, train_indices):  # type: ignore[no-untyped-def]
        logs.append(tuple(train_indices))
        return tuple(train_indices)

    def predict_fn(model, session):  # type: ignore[no-untyped-def]
        return np.array([0.0])

    result = forward_chained_oof_scores(sessions, fit_fn, predict_fn, purge=1)
    # For session k, training indices are [0, k-1). So sessions 0 and 1 are unscored.
    assert result.unscored_sessions == (0, 1)
    assert result.scored_sessions == (2, 3, 4)
    assert result.train_index_log[2] == (0,)
    assert result.train_index_log[3] == (0, 1)
    assert result.train_index_log[4] == (0, 1, 2)
    # Purge guarantees the session immediately before k is never in its training set.
    for k, train_indices in enumerate(result.train_index_log):
        assert (k - 1) not in train_indices
