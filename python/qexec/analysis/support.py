"""G2-S support gate: frozen horizon selection from support counts only (product 4.4).

The support gate decides whether a candidate horizon yields *enough observable queue dynamics*
to justify fitting the queue-aware action models. It reads **only** the per-session support
counts produced by the engine's B1 worlds (``SessionOutputs.support``): the number of
decision-eligible checkpoints and the number of post-checkpoint queue-depletion passive fills.
It never reads cost, markout, or outcome columns, so changing those cannot change the selection
(research spec T59).

The selection rule is frozen in the study config before any run: the primary horizon ``H`` is
the **shortest** candidate ladder horizon (valid under L1) for which *every* pilot session has

* ``decision_eligible_checkpoints >= support_min_eligible`` and
* ``post_checkpoint_queue_depletion >= support_min_queue_depletion``.

If no candidate horizon passes, the gate verdict is ``FAILED`` and the study stops.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import polars as pl

__all__ = ["HorizonSupport", "SupportGateResult", "select_support_horizon"]

# Columns the gate is allowed to read. Any other column (cost, markout, miss) is ignored by
# construction so it cannot influence the selection (T59).
_ELIGIBLE_COL = "decision_eligible_checkpoints"
_DEPLETION_COL = "post_checkpoint_queue_depletion"


@dataclass(frozen=True, slots=True)
class HorizonSupport:
    """Per-horizon support summary (support counts only)."""

    horizon_ns: int
    passes: bool
    """Whether every pilot session met both minima at this horizon."""
    min_eligible: int
    """Minimum over pilot sessions of ``decision_eligible_checkpoints``."""
    min_queue_depletion: int
    """Minimum over pilot sessions of ``post_checkpoint_queue_depletion``."""
    n_pilot_sessions: int
    per_session_eligible: dict[str, int]
    per_session_queue_depletion: dict[str, int]


@dataclass(frozen=True, slots=True)
class SupportGateResult:
    """Outcome of the frozen support-horizon selection."""

    verdict: str
    """``"PASSED"`` with a chosen horizon, or ``"FAILED"`` when no candidate passes."""
    chosen_horizon_ns: int | None
    support_min_eligible: int
    support_min_queue_depletion: int
    horizons: tuple[HorizonSupport, ...]
    """One entry per candidate horizon, in ascending horizon order (complete log)."""


def _per_session_counts(support: pl.DataFrame, column: str) -> dict[str, int]:
    if support.height == 0:
        return {}
    out: dict[str, int] = {}
    for row in support.select("session_id", column).iter_rows(named=True):
        out[str(row["session_id"])] = int(row[column])
    return out


def select_support_horizon(
    support_by_horizon: Mapping[int, pl.DataFrame],
    pilot_session_ids: Sequence[str],
    *,
    support_min_eligible: int,
    support_min_queue_depletion: int,
) -> SupportGateResult:
    """Select the primary horizon from pilot support counts (product 4.4, T59).

    Args:
        support_by_horizon: mapping ``horizon_ns -> support DataFrame`` (the concatenated B1
            ``support`` frames of the pilot sessions run at that horizon under L1).
        pilot_session_ids: the pilot session ids that must *each* meet both minima. A horizon
            missing a pilot session's row is treated as that session having zero counts (it
            cannot pass), so a session with no eligible checkpoints never silently passes.
        support_min_eligible: minimum decision-eligible checkpoints per pilot session.
        support_min_queue_depletion: minimum post-checkpoint queue-depletion fills per session.

    Returns:
        A :class:`SupportGateResult`. ``verdict`` is ``"PASSED"`` with the shortest passing
        horizon, else ``"FAILED"`` with ``chosen_horizon_ns = None``.
    """
    pilots = [str(s) for s in pilot_session_ids]
    n_pilot = len(pilots)
    horizons: list[HorizonSupport] = []
    for horizon_ns in sorted(support_by_horizon):
        support = support_by_horizon[horizon_ns]
        elig = _per_session_counts(support, _ELIGIBLE_COL)
        depl = _per_session_counts(support, _DEPLETION_COL)
        # A missing pilot session counts as zero (cannot pass).
        per_elig = {s: elig.get(s, 0) for s in pilots}
        per_depl = {s: depl.get(s, 0) for s in pilots}
        min_elig = min(per_elig.values()) if per_elig else 0
        min_depl = min(per_depl.values()) if per_depl else 0
        passes = (
            n_pilot > 0
            and min_elig >= support_min_eligible
            and min_depl >= support_min_queue_depletion
        )
        horizons.append(
            HorizonSupport(
                horizon_ns=horizon_ns,
                passes=passes,
                min_eligible=min_elig,
                min_queue_depletion=min_depl,
                n_pilot_sessions=n_pilot,
                per_session_eligible=per_elig,
                per_session_queue_depletion=per_depl,
            )
        )

    chosen = next((h.horizon_ns for h in horizons if h.passes), None)
    return SupportGateResult(
        verdict="PASSED" if chosen is not None else "FAILED",
        chosen_horizon_ns=chosen,
        support_min_eligible=support_min_eligible,
        support_min_queue_depletion=support_min_queue_depletion,
        horizons=tuple(horizons),
    )
