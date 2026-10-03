"""Committed historical mid series and price-direction labels (engineering contract WP-FEATURES).

:class:`MidSeries` is the evaluator-side (direct, undelayed) committed mid price over time,
replayed through the reference book from a session directory. It is used to build price-direction
training labels; it is *not* the client (delayed) book.

Replay semantics
----------------
* Batches are produced by :func:`qexec.adapters.batches.iter_session_batches` (which verifies
  checksums). Status events are read with :func:`qexec.core.records_io.read_status` and applied
  by event time: the book's trading status is advanced to the latest status event whose
  ``event_time_ns <= batch.exchange_proxy_time`` before that batch is committed.
* A sample ``(exchange_proxy_time, mid2 | None)`` is recorded at every committed batch.
  Snapshots initialize quote references without contributing to order-flow features. The
  mid is the committed book ``mid2`` when the status is ``TRADING`` and both sides are present
  and uncrossed; otherwise it is ``None`` (no valid mid while halted / pre-open / closed /
  one-sided).

Lookup
------
:meth:`mid_at` returns the last committed valid mid at or before ``t`` using :func:`bisect`
over the stored times, or ``None`` if there is no sample at/before ``t`` or that sample's mid is
``None``.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from pathlib import Path

from qexec.adapters.batches import iter_session_batches
from qexec.core.records_io import read_instrument, read_status
from qexec.core.types import Action, TradingStatus
from qexec.reference.book import ReferenceBook
from qexec.sim.evidence import valid_resting_allocation
from qexec.sim.quality import QualityMap, QualityWindow, anomaly_count


class MidSeries:
    """Committed historical direct-book mid series with ``bisect`` point lookup."""

    def __init__(self, times_ns: Sequence[int], mid2: Sequence[int | None]) -> None:
        if len(times_ns) != len(mid2):
            raise ValueError("times_ns and mid2 must have equal length")
        times = list(times_ns)
        if any(times[i] < times[i - 1] for i in range(1, len(times))):
            raise ValueError("times_ns must be nondecreasing")
        self._times: list[int] = times
        self._mid2: list[int | None] = list(mid2)
        self._source_quality: QualityMap | None = None

    @classmethod
    def from_session(cls, session_dir: Path) -> MidSeries:
        """Replay ``session_dir`` through a :class:`ReferenceBook` and collect committed mids."""
        instrument = read_instrument(session_dir)
        status_events = sorted(read_status(session_dir), key=lambda s: int(s.event_time_ns))
        book = ReferenceBook(instrument)
        # A session's normal state is TRADING; status events only carry transitions (e.g. a
        # HALTED/TRADING pair around a halt), with no explicit opening event. Start TRADING so a
        # halt-free session yields valid committed mids; a HALTED event suspends it and the
        # paired resuming TRADING event restores it.
        book.set_status(TradingStatus.TRADING)

        times: list[int] = []
        mids: list[int | None] = []
        status_idx = 0
        n_status = len(status_events)
        quality = QualityMap()

        def _current_mid() -> int | None:
            """Committed book ``mid2`` under the current status (``None`` if not TRADING)."""
            snap = book.snapshot(depth=1)
            if snap.status is not TradingStatus.TRADING:
                return None
            return snap.mid2()

        # Merged status/book timeline (external review R9). A status event creates its *own*
        # sample so a halt that begins in a quiet gap (no committed batch during the gap) is
        # reflected immediately, with mid ``None`` while not TRADING -- instead of being ignored
        # until the next batch arrives. At each batch we first apply every status event at or
        # before the batch's proxy time (recording a sample for each one that falls strictly
        # before the batch, i.e. in a quiet gap), then commit the batch and record its sample.
        for batch in iter_session_batches(session_dir):
            proxy_time = int(batch.exchange_proxy_time)
            while (
                status_idx < n_status and int(status_events[status_idx].event_time_ns) <= proxy_time
            ):
                st = status_events[status_idx]
                st_time = int(st.event_time_ns)
                book.set_status(st.status)
                # Emit a standalone status sample only when the transition occurs strictly before
                # this batch (a quiet-gap transition). A transition coincident with the batch is
                # subsumed by the batch sample recorded below (same time, applied after).
                if st_time < proxy_time:
                    times.append(st_time)
                    mids.append(_current_mid())
                status_idx += 1
            before = anomaly_count(book)
            book.begin_batch(batch)
            invalid_allocation = False
            for record in batch.records:
                if record.action is Action.FILL:
                    invalid_allocation |= (
                        book.status is not TradingStatus.TRADING
                        or not valid_resting_allocation(record, book)
                    )
                book.apply_record(record)
            book.commit_batch(batch)
            quality.note_batch(
                batch, anomaly_count(book) > before, invalid_allocation=invalid_allocation
            )
            times.append(proxy_time)
            mids.append(_current_mid())

        # Any trailing status transitions after the last batch (e.g. an end-of-day halt in a
        # quiet gap) still create samples so lookups past the last batch see the correct state.
        while status_idx < n_status:
            st = status_events[status_idx]
            st_time = int(st.event_time_ns)
            book.set_status(st.status)
            if not times or st_time >= times[-1]:
                times.append(st_time)
                mids.append(_current_mid())
            status_idx += 1

        series = cls(times, mids)
        series._source_quality = quality
        return series

    def source_window_valid(self, start_ns: int, end_ns: int) -> bool:
        """Whether a label/markout window crosses no corruption or reconstruction boundary."""
        if end_ns < start_ns:
            raise ValueError("reference window end precedes its start")
        if self._source_quality is None:
            return True
        invalid, _ = self._source_quality.is_unevaluable(QualityWindow(start_ns, end_ns))
        return not invalid

    def mid_at(self, t_ns: int) -> int | None:
        """Return the committed mid *state at* ``t_ns`` (``None`` if none/invalid).

        Uses the merged status/book timeline: the last sample at or before ``t`` is the state at
        ``t`` (a status event in a quiet gap has its own ``None`` sample), so a lookup inside a
        halt returns ``None`` rather than a stale pre-halt mid.
        """
        idx = bisect.bisect_right(self._times, t_ns) - 1
        if idx < 0:
            return None
        return self._mid2[idx]

    def last_valid_mid_at(self, t_ns: int) -> tuple[int, int] | None:
        """Last sample with a *valid* mid at or before ``t_ns`` as ``(mid2, sample_time_ns)``.

        Unlike :meth:`mid_at` (which reports the state at ``t``, possibly invalid), this skips
        invalid samples. It is the deadline-valuation reference ``m_T`` of product section 8
        ("last valid committed reference midpoint at or before T"); callers record its age as
        ``t_ns - sample_time_ns``. ``None`` only if no valid sample exists at or before ``t``.
        """
        idx = bisect.bisect_right(self._times, t_ns) - 1
        while idx >= 0:
            mid = self._mid2[idx]
            if mid is not None:
                return mid, self._times[idx]
            idx -= 1
        return None

    def __len__(self) -> int:
        return len(self._times)

    @property
    def last_sample_time_ns(self) -> int | None:
        """Time of the last sample on the merged status/book timeline (``None`` if empty).

        Coverage upper bound for labelling: a price target at a time strictly after this is
        beyond the series' coverage (and beyond the session's last committed/observed state).
        """
        return self._times[-1] if self._times else None


def price_direction_label(
    series: MidSeries, t_star_ns: int, tau_ns: int, tick_size_fixed: int
) -> int | None:
    """Sign of ``m(t* + tau) - m(t*)`` in ``{-1, 0, 1}``, or ``None`` when it cannot be labelled.

    Returns ``None`` (external review R9) when:

    * ``t* + tau`` is strictly beyond the last sample on the merged status/book timeline (beyond
      coverage / past the session's last committed or observed state), or
    * either endpoint's state is invalid -- there is no sample at or before it, or the state at
      it is a halt / pre-open / closed / one-sided book (``mid_at`` returns ``None``).

    ``tick_size_fixed`` is accepted for interface symmetry with the feature schema (the sign is
    scale-invariant, so the comparison is on raw ``mid2``). An unchanged mid between two valid,
    in-coverage endpoints is class ``0`` and is retained (never collapsed to ``None``) -- T43.
    """
    del tick_size_fixed  # sign is scale-invariant; kept for schema symmetry
    target = t_star_ns + tau_ns
    last = series.last_sample_time_ns
    if last is None or target > last or t_star_ns > last:
        # Beyond the series' coverage: the target (or origin) is past the last known state.
        return None
    if not series.source_window_valid(t_star_ns, target):
        return None
    m0 = series.mid_at(t_star_ns)
    m1 = series.mid_at(target)
    if m0 is None or m1 is None:
        return None
    if m1 > m0:
        return 1
    if m1 < m0:
        return -1
    return 0
