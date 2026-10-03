"""Policy-independent task quality map (engine spec section 3, product section 5.3).

A task is ``TECHNICALLY_UNEVALUABLE`` for *every* policy if its evaluation window
``[arrival, deadline]`` overlaps an **invalid interval**: a span of lost book continuity that
runs from a corrupting event until a *trusted recovery* (architecture section 5, external review
R5). This replaces the earlier instant-based rule, which flagged a window only when a corrupting
batch commit fell *inside* it and so wrongly treated a task arriving *after* an unrecovered
corruption as evaluable.

Corrupting events (each opens an invalid interval if one is not already open):

* a committed batch carrying any ``quality_flags`` (TRUNCATED, DECREASING_PROXY_TIME,
  CAPTURE_BEFORE_PROXY, MAYBE_BAD_BOOK, INVALID_TIMESTAMP),
* a ``CLEAR`` record (loss of book continuity), or
* a historical-book anomaly recorded while committing a batch in the pass, or
* malformed execution groups or unsupported resting allocations.

Trusted recovery (closes an open invalid interval): an ``INITIALIZATION`` snapshot batch that
occurs after the corruption and itself carries **no** quality flags, invalid execution evidence,
or new anomaly -- a validated snapshot that re-establishes continuity (architecture section 5:
resume only after a trusted reconstruction path, such as a validated snapshot plus replay). An
``INITIALIZATION`` batch while no interval is open (e.g. the opening snapshot) is just that
recovery mechanism and does not itself open an interval. A combined ``CLEAR`` plus clean
snapshot recovers atomically. Every snapshot also records a queue-continuity boundary: tasks
with ``arrival < reset <= deadline`` are excluded for all policies, even those already filled.
A task arriving exactly at a clean reset sees the recovered state. An unrecovered interval
extends to the session end.

This is computed once, before any policy runs, from the historical replay, and persisted. It is
policy independent: it depends only on the tape and the task window, never on any policy's
actions (product 5.3: the status "must be applied by a policy-independent rule, counted, and
disclosed"). An overlay ``TECHNICAL_RESET`` is a strict subset -- it is triggered by exactly the
CLEAR / snapshot-while-working conditions this map already covers -- so a world that sees a
technical reset is always already unevaluable here.
"""

from __future__ import annotations

from dataclasses import dataclass

from qexec.core.types import Action, BatchKind, EventBatch
from qexec.reference.book import ReferenceBook
from qexec.sim.evidence import execution_groups

# Sentinel upper bound for an invalid interval whose recovery never arrived and which was never
# finalized with an explicit session end. Any realistic task window lies below this.
_UNBOUNDED_END = 1 << 62


@dataclass(frozen=True, slots=True)
class QualityWindow:
    """A half-open-free evaluation window ``[arrival_ns, deadline_ns]`` for one task grid slot."""

    arrival_ns: int
    deadline_ns: int


@dataclass(frozen=True, slots=True)
class _InvalidInterval:
    """A span ``[start_ns, end_ns)`` of lost book continuity, with the opening reason.

    ``end_ns`` is the proxy time of the trusted recovery that closed the interval, or the session
    end (set by :meth:`finalize`) for an unrecovered interval.
    """

    start_ns: int
    end_ns: int
    reason: str


class QualityMap:
    """Maps an evaluation window to whether it overlaps an invalid interval.

    Invalid intervals are built incrementally from the historical replay via :meth:`note_batch`
    (called in commit order) and closed either by a trusted recovery or by :meth:`finalize` at
    the session end. ``is_unevaluable`` reports whether a window overlaps any interval.
    """

    def __init__(self) -> None:
        self._intervals: list[_InvalidInterval] = []
        # The currently open interval's (start_ns, reason), or None when continuity is intact.
        self._open: tuple[int, str] | None = None
        self._finalized_end: int | None = None
        self._resets: list[int] = []

    def note_batch(
        self, batch: EventBatch, anomaly_added: bool, *, invalid_allocation: bool = False
    ) -> None:
        """Fold a committed ``batch`` into the invalid-interval ledger (commit order).

        ``anomaly_added`` is ``True`` iff committing this batch appended a new historical-book
        anomaly. Opens an interval on a corrupting event; closes the open interval on a trusted
        recovery (a clean ``INITIALIZATION`` snapshot after the corruption).
        ``invalid_allocation`` reports a FILL that failed the pre-mutation resting-book check.
        """
        proxy = int(batch.exchange_proxy_time)
        invalid_execution = invalid_allocation or execution_groups(batch).invalid
        corrupting_reason = self._corrupting_reason(batch, anomaly_added, invalid_execution)
        is_clean_init = (
            batch.kind is BatchKind.INITIALIZATION
            and not batch.quality_flags
            and not anomaly_added
            and not invalid_execution
        )

        if batch.kind is BatchKind.INITIALIZATION:
            # A snapshot can restore source state, but cannot restore the hypothetical
            # order's queue priority across the boundary. Every crossing policy world
            # must therefore be excluded, including those already filled before reset.
            self._resets.append(proxy)
        if is_clean_init:
            if self._open is not None:
                # CLEAR + SNAPSHOT is one atomic trusted reconstruction. Its CLEAR
                # must not immediately reopen the interval the snapshot just closed.
                start, reason = self._open
                self._intervals.append(_InvalidInterval(start, proxy, reason))
                self._open = None
            return

        if corrupting_reason is not None and self._open is None:
            # Open a new invalid interval at this corrupting event.
            self._open = (proxy, corrupting_reason)

    def finalize(self, session_end_ns: int) -> None:
        """Close any still-open invalid interval at ``session_end_ns`` (unrecovered to EOD)."""
        self._finalized_end = int(session_end_ns)
        if self._open is not None:
            start, reason = self._open
            end = max(int(session_end_ns), start) + 1  # inclusive of the session's final instant
            self._intervals.append(_InvalidInterval(start, end, reason))
            self._open = None

    @staticmethod
    def _corrupting_reason(
        batch: EventBatch, anomaly_added: bool, invalid_execution: bool
    ) -> str | None:
        if batch.quality_flags:
            return "QUALITY_FLAGS:" + ",".join(sorted(f.value for f in batch.quality_flags))
        if any(r.action is Action.CLEAR for r in batch.records):
            return "CLEAR_RECORD"
        if anomaly_added:
            return "BOOK_ANOMALY"
        if invalid_execution:
            return "INVALID_EXECUTION_EVIDENCE"
        return None

    def _effective_intervals(self) -> list[_InvalidInterval]:
        """All closed intervals plus any still-open interval extended to its effective end."""
        intervals = list(self._intervals)
        if self._open is not None:
            start, reason = self._open
            end = (self._finalized_end + 1) if self._finalized_end is not None else _UNBOUNDED_END
            intervals.append(_InvalidInterval(start, max(end, start + 1), reason))
        intervals.sort(key=lambda iv: iv.start_ns)
        return intervals

    def is_unevaluable(self, window: QualityWindow) -> tuple[bool, str | None]:
        """Return ``(unevaluable, reason)`` for ``window``.

        A window ``[arrival, deadline]`` is unevaluable iff it overlaps an invalid interval
        ``[start, end)``: ``arrival < end`` and ``deadline >= start`` (the deadline is in the
        frozen window per the on-time rule). The reason is the opening reason of the first (by
        start time) overlapping interval, or SNAPSHOT_RESET for a crossing reset boundary.
        """
        for interval in self._effective_intervals():
            if window.arrival_ns < interval.end_ns and window.deadline_ns >= interval.start_ns:
                return True, interval.reason
        if any(window.arrival_ns < reset <= window.deadline_ns for reset in self._resets):
            return True, "SNAPSHOT_RESET"
        return False, None

    @property
    def reset_times(self) -> tuple[int, ...]:
        """Snapshot discontinuities; arrival exactly at a clean reset sees recovered state."""
        return tuple(sorted(self._resets))

    @property
    def corrupting_times(self) -> list[int]:
        """Start times of every invalid interval (the corruption instants), ascending.

        Preserved for callers that count corrupting events (e.g. the ``tasks build`` CLI
        summary); each invalid interval corresponds to exactly one opening corruption.
        """
        return [iv.start_ns for iv in self._effective_intervals()]

    @property
    def intervals(self) -> list[tuple[int, int, str]]:
        """The invalid intervals as ``(start_ns, end_ns, reason)`` tuples (ascending)."""
        return [(iv.start_ns, iv.end_ns, iv.reason) for iv in self._effective_intervals()]


def anomaly_count(book: ReferenceBook) -> int:
    """Current number of recorded anomalies on ``book`` (used to detect new ones per batch)."""
    return len(book.anomalies)
