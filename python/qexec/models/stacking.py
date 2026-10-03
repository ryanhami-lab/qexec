"""Forward-chained out-of-fold scoring (product section 8, architecture 10.3; T44).

To feed a shared price-model score into second-stage (B3) training without leakage, the
score for session ``k`` must come from a model fitted only on *earlier* sessions. This
module implements that chronological scheme generically: given sessions in chronological
order, a ``fit_fn`` and a ``predict_fn``, it fits on sessions ``< k`` (optionally leaving a
purge gap of ``purge`` sessions immediately before ``k``) and scores session ``k``. The
earliest session(s) with no eligible prior data receive ``NaN`` scores and are reported, so
an in-sample or future-trained score can never silently enter training.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

__all__ = ["ForwardChainedResult", "forward_chained_oof_scores"]


@dataclass(frozen=True, slots=True)
class ForwardChainedResult[TModel]:
    """Outcome of forward-chained scoring.

    Attributes:
        scores: one score array per input session (same order). Sessions with no eligible
            prior training data hold an empty float array (they are unscored).
        scored_sessions: indices of sessions that received real (non-NaN) scores.
        unscored_sessions: indices of sessions left NaN for lack of prior data (reported).
        train_index_log: for each session index, the sorted list of session indices used to
            fit the model that scored it (empty when unscored). Lets a test verify that no
            in-sample or future session was ever used (T44).
    """

    scores: tuple[np.ndarray, ...]
    scored_sessions: tuple[int, ...]
    unscored_sessions: tuple[int, ...]
    train_index_log: tuple[tuple[int, ...], ...]


def forward_chained_oof_scores[TSession, TModel](
    sessions: Sequence[TSession],
    fit_fn: Callable[[Sequence[TSession], Sequence[int]], TModel],
    predict_fn: Callable[[TModel, TSession], np.ndarray],
    *,
    purge: int = 0,
) -> ForwardChainedResult[TModel]:
    """Score each session using only chronologically prior sessions.

    Args:
        sessions: sessions in chronological order.
        fit_fn: called as ``fit_fn(sessions, train_indices)`` where ``train_indices`` is the
            strictly-earlier set of indices (after purging); returns a fitted model.
        predict_fn: called as ``predict_fn(model, session)``; returns a 1-D score array for
            that session's rows.
        purge: number of sessions immediately before ``k`` to exclude (label-window purge).
            ``purge=0`` uses all sessions ``< k``.

    For session ``k`` the training indices are ``[0, k - purge)``. Sessions where that set is
    empty are left NaN and reported. ``fit_fn`` is only called with a non-empty training set,
    so a test ``fit_fn`` recording the indices it sees proves no in-sample or future session
    is ever used.
    """
    if purge < 0:
        raise ValueError("purge must be nonnegative")
    n = len(sessions)

    # Unscored sessions (no eligible prior data) receive an empty float array and are listed
    # in ``unscored_sessions``. We never call ``predict_fn`` for them, which would require a
    # model fitted on no prior data (or, worse, a later model) and reintroduce leakage.
    scores: list[np.ndarray] = []
    scored: list[int] = []
    unscored: list[int] = []
    train_log: list[tuple[int, ...]] = []

    for k in range(n):
        upper = k - purge
        train_indices = list(range(max(0, upper)))
        if not train_indices:
            scores.append(np.array([], dtype=np.float64))
            unscored.append(k)
            train_log.append(())
            continue
        model = fit_fn(sessions, train_indices)
        session_scores = np.asarray(predict_fn(model, sessions[k]), dtype=np.float64)
        if session_scores.ndim != 1:
            raise ValueError("predict_fn must return a 1-D score array")
        scores.append(session_scores)
        scored.append(k)
        train_log.append(tuple(train_indices))

    return ForwardChainedResult(
        scores=tuple(scores),
        scored_sessions=tuple(scored),
        unscored_sessions=tuple(unscored),
        train_index_log=tuple(train_log),
    )
