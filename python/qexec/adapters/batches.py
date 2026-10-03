"""Completed-event assembler (architecture section 4.2; validation T02, T03, T04).

An *event* is a run of :class:`CanonicalRecord` ending with a record whose flags include
``RecordFlag.LAST``. :class:`BatchAssembler` buffers records until it sees ``LAST`` and
then emits an :class:`EventBatch`. A non-mutating record (e.g. ``TRADE`` or ``NONE``) may
carry the boundary flag and still close the batch (T03). A trailing run with no ``LAST`` is
emitted by :meth:`finish` flagged :class:`QualityFlag.TRUNCATED` (that segment is invalid
until recovery, architecture 4.2).

Batch kind is :class:`BatchKind.INITIALIZATION` iff every record is ``SNAPSHOT``-flagged,
or the batch is a ``CLEAR`` followed only by ``SNAPSHOT``-flagged adds (contract section 3).
Otherwise it is :class:`BatchKind.LIVE`.

Quality flags:

* ``DECREASING_PROXY_TIME`` — this batch's ``exchange_proxy_time`` is below the previous
  batch's (nonmonotone exchange proxy time).
* ``CAPTURE_BEFORE_PROXY`` — ``capture_complete_time < exchange_proxy_time`` (capture must
  be at or after the event it captured).
* ``TRUNCATED`` — emitted by :meth:`finish` for a trailing run with no ``LAST``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from qexec.core.records_io import iter_records, read_instrument, verify_session
from qexec.core.types import (
    Action,
    BatchKind,
    CanonicalRecord,
    EventBatch,
    QualityFlag,
    RecordFlag,
    TimeNs,
)


def _classify_kind(records: tuple[CanonicalRecord, ...]) -> BatchKind:
    """INITIALIZATION iff all records are snapshot adds, or CLEAR + snapshot adds."""
    if not records:
        return BatchKind.LIVE
    if all(r.is_snapshot for r in records):
        return BatchKind.INITIALIZATION
    first = records[0]
    if first.action is Action.CLEAR and all(
        r.is_snapshot for r in records[1:] if r.action.mutates_book
    ):
        # A CLEAR that only re-initializes via snapshot adds is still initialization.
        non_clear = records[1:]
        if non_clear and all(r.is_snapshot or not r.action.mutates_book for r in non_clear):
            return BatchKind.INITIALIZATION
    return BatchKind.LIVE


class BatchAssembler:
    """Assemble :class:`CanonicalRecord` streams into completed :class:`EventBatch` events."""

    def __init__(self, instrument_id: int) -> None:
        self._instrument_id = instrument_id
        self._buffer: list[CanonicalRecord] = []
        self._next_batch_id: int = 0
        self._prev_proxy_time: int | None = None
        self._session_epoch: int | None = None

    def feed(self, record: CanonicalRecord) -> EventBatch | None:
        """Buffer ``record``; return a completed :class:`EventBatch` when it carries LAST."""
        if record.instrument_id != self._instrument_id:
            raise ValueError("mixed instrument stream: normalize one instrument per session")
        if self._session_epoch is not None and record.session_epoch != self._session_epoch:
            raise ValueError("mixed session epoch stream: normalize one epoch per session")
        self._session_epoch = record.session_epoch
        self._buffer.append(record)
        if record.is_last:
            return self._emit(truncated=False)
        return None

    def finish(self) -> EventBatch | None:
        """Emit any buffered trailing run (with no LAST) flagged TRUNCATED; else ``None``."""
        if not self._buffer:
            return None
        return self._emit(truncated=True)

    def _emit(self, *, truncated: bool) -> EventBatch:
        records = tuple(self._buffer)
        self._buffer.clear()
        batch_id = self._next_batch_id
        self._next_batch_id += 1

        proxy_time = max(r.event_time_ns for r in records)
        capture_time = max(r.capture_time_ns for r in records)

        flags: set[QualityFlag] = set()
        if truncated:
            flags.add(QualityFlag.TRUNCATED)
        if self._prev_proxy_time is not None and proxy_time < self._prev_proxy_time:
            flags.add(QualityFlag.DECREASING_PROXY_TIME)
        if any(r.capture_time_ns < r.event_time_ns for r in records):
            flags.add(QualityFlag.CAPTURE_BEFORE_PROXY)
        if any(
            r.event_time_ns < 0 or r.capture_time_ns < 0 or r.flags & RecordFlag.BAD_TS_RECV
            for r in records
        ):
            flags.add(QualityFlag.INVALID_TIMESTAMP)
        if any(
            (r.action in (Action.TRADE, Action.FILL) and r.quantity <= 0)
            or (r.is_snapshot and r.action not in (Action.ADD, Action.CLEAR, Action.NONE))
            for r in records
        ):
            flags.add(QualityFlag.INVALID_RECORD)
        if any(r.flags & RecordFlag.MAYBE_BAD_BOOK for r in records):
            flags.add(QualityFlag.MAYBE_BAD_BOOK)

        self._prev_proxy_time = proxy_time

        return EventBatch(
            batch_id=batch_id,
            instrument_id=self._instrument_id,
            records=records,
            exchange_proxy_time=TimeNs(proxy_time),
            capture_complete_time=TimeNs(capture_time),
            kind=_classify_kind(records),
            quality_flags=frozenset(flags),
        )


def iter_batches(records: Iterable[CanonicalRecord], instrument_id: int) -> Iterator[EventBatch]:
    """Yield completed :class:`EventBatch` events, including a final TRUNCATED one."""
    assembler = BatchAssembler(instrument_id)
    for record in records:
        batch = assembler.feed(record)
        if batch is not None:
            yield batch
    trailing = assembler.finish()
    if trailing is not None:
        yield trailing


def iter_session_batches(session_dir: Path) -> Iterator[EventBatch]:
    """Verify session checksums, then yield batches for the session's instrument."""
    verify_session(session_dir)
    instrument = read_instrument(session_dir)
    yield from iter_batches(iter_records(session_dir), instrument.instrument_id)
