"""Shared builders for WP2-OVERLAY tests.

The core helper :func:`drive_batch` runs a batch through a real
:class:`~qexec.reference.book.ReferenceBook` in the *exact engine order* required by the
contract: for each record ``overlay.on_record(rec, book)`` is called BEFORE
``book.apply_record(rec)``; at batch end ``book.commit_batch(batch)`` then
``overlay.on_batch_committed(batch, book)``. This guarantees the overlay always inspects
``book_before`` and the committed state exactly as the engine will drive it.
"""

from __future__ import annotations

from dataclasses import replace

from qexec.adapters.batches import BatchAssembler
from qexec.core.messages import CommandKind, ExchangeCommand, ExchangeReport
from qexec.core.types import (
    Action,
    CanonicalRecord,
    EventBatch,
    InstrumentDefinition,
    PriceFixed,
    RecordFlag,
    Side,
    TaskSide,
    TimeNs,
)
from qexec.reference.book import ReferenceBook
from qexec.sim.evidence import execution_groups
from qexec.sim.overlay import ExchangeOverlay

INSTRUMENT_ID = 1
TICK = 250_000_000  # 0.25 in 1e-9 units

# A canonical book around 100.00 / 100.25 (one tick wide).
BID = 100_000_000_000  # 100.0
BID2 = BID - TICK  # 99.75
BID3 = BID - 2 * TICK  # 99.50
ASK = 100_250_000_000  # 100.25
ASK2 = ASK + TICK  # 100.50


def instrument(instrument_id: int = INSTRUMENT_ID) -> InstrumentDefinition:
    return InstrumentDefinition(
        instrument_id=instrument_id,
        symbol="SYNZ6",
        tick_size_fixed=PriceFixed(TICK),
        multiplier=50,
        effective_time_ns=TimeNs(0),
    )


def rec(
    ordinal: int,
    action: Action,
    *,
    side: Side = Side.BID,
    price_fixed: int = BID,
    quantity: int = 1,
    order_id: int = 0,
    event_time_ns: int = 1_000,
    capture_time_ns: int | None = None,
    flags: RecordFlag = RecordFlag.NONE,
    instrument_id: int = INSTRUMENT_ID,
) -> CanonicalRecord:
    """Build a :class:`CanonicalRecord` with sensible defaults for overlay tests."""
    return CanonicalRecord(
        dataset_id="SYNTH.MBO",
        publisher_id=1,
        instrument_id=instrument_id,
        session_epoch=0,
        source_ordinal=ordinal,
        action=action,
        side=side,
        price_fixed=price_fixed,
        quantity=quantity,
        order_id=order_id,
        event_time_ns=TimeNs(event_time_ns),
        capture_time_ns=TimeNs(capture_time_ns if capture_time_ns is not None else event_time_ns),
        flags=flags,
        source_file_hash="hashA",
        source_record_offset=ordinal,
    )


def make_batch(records: list[CanonicalRecord], instrument_id: int = INSTRUMENT_ID) -> EventBatch:
    """Assemble exactly one batch from ``records``; the last record is marked LAST."""
    assembler = BatchAssembler(instrument_id)
    marked = list(records)
    marked[-1] = replace(marked[-1], flags=marked[-1].flags | RecordFlag.LAST)
    out: EventBatch | None = None
    for r in marked:
        emitted = assembler.feed(r)
        if emitted is not None:
            out = emitted
    assert out is not None, "records did not close a batch"
    return out


def drive_batch(
    overlay: ExchangeOverlay, book: ReferenceBook, records: list[CanonicalRecord]
) -> list[ExchangeReport]:
    """Drive ``records`` as one batch through ``overlay`` and ``book`` in engine order.

    Returns every report emitted (on_record reports in order, then on_batch_committed).
    """
    batch = make_batch(records)
    evidence = execution_groups(batch)
    book.begin_batch(batch)
    reports: list[ExchangeReport] = []
    for r in batch.records:
        reports.extend(overlay.on_record(r, book, evidence))
        book.apply_record(r)
    book.commit_batch(batch)
    reports.extend(overlay.on_batch_committed(batch, book))
    return reports


def seed_book(book: ReferenceBook, records: list[CanonicalRecord]) -> None:
    """Apply a priming batch directly to the book (no overlay yet), as one committed batch."""
    batch = make_batch(records)
    book.begin_batch(batch)
    for r in batch.records:
        book.apply_record(r)
    book.commit_batch(batch)


def passive_cmd(
    *,
    side: int,
    limit_price_fixed: int | None,
    arrival_time_ns: int = 100,
    command_id: str = "c1",
    task_id: str = "T1",
    quantity: int = 1,
) -> ExchangeCommand:
    return ExchangeCommand(
        command_id=command_id,
        task_id=task_id,
        kind=CommandKind.PASSIVE_LIMIT,
        side=TaskSide(side),
        quantity=quantity,
        arrival_time_ns=TimeNs(arrival_time_ns),
        limit_price_fixed=limit_price_fixed,
    )


def aggressive_cmd(
    *,
    side: int,
    arrival_time_ns: int = 200,
    command_id: str = "a1",
    task_id: str = "T1",
) -> ExchangeCommand:
    return ExchangeCommand(
        command_id=command_id,
        task_id=task_id,
        kind=CommandKind.AGGRESSIVE,
        side=TaskSide(side),
        quantity=1,
        arrival_time_ns=TimeNs(arrival_time_ns),
        limit_price_fixed=None,
    )


def cancel_cmd(
    *,
    side: int,
    target_command_id: str | None,
    arrival_time_ns: int = 300,
    command_id: str = "x1",
    task_id: str = "T1",
) -> ExchangeCommand:
    return ExchangeCommand(
        command_id=command_id,
        task_id=task_id,
        kind=CommandKind.CANCEL,
        side=TaskSide(side),
        quantity=1,
        arrival_time_ns=TimeNs(arrival_time_ns),
        limit_price_fixed=None,
        target_command_id=target_command_id,
    )
