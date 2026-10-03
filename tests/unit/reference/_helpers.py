"""Shared builders for WP1-BOOK tests (records, batches, instrument)."""

from __future__ import annotations

from dataclasses import replace

from qexec.adapters.batches import BatchAssembler
from qexec.core.types import (
    Action,
    CanonicalRecord,
    EventBatch,
    InstrumentDefinition,
    PriceFixed,
    RecordFlag,
    Side,
    TimeNs,
)

INSTRUMENT_ID = 1
TICK = 250_000_000  # 0.25 in 1e-9 units


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
    price_fixed: int = 100_000_000_000,
    quantity: int = 1,
    order_id: int = 0,
    event_time_ns: int = 1_000,
    capture_time_ns: int | None = None,
    flags: RecordFlag = RecordFlag.NONE,
    instrument_id: int = INSTRUMENT_ID,
    source_file_hash: str = "hashA",
    source_record_offset: int | None = None,
) -> CanonicalRecord:
    """Build a :class:`CanonicalRecord` with sensible defaults for book tests."""
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
        source_file_hash=source_file_hash,
        source_record_offset=(
            source_record_offset if source_record_offset is not None else ordinal
        ),
    )


def single_batch(records: list[CanonicalRecord], instrument_id: int = INSTRUMENT_ID) -> EventBatch:
    """Assemble exactly one batch from ``records``; the last record is marked LAST."""
    assembler = BatchAssembler(instrument_id)
    marked = list(records)
    last = marked[-1]
    marked[-1] = _with_last(last)
    out: EventBatch | None = None
    for r in marked:
        emitted = assembler.feed(r)
        if emitted is not None:
            out = emitted
    assert out is not None, "records did not close a batch"
    return out


def _with_last(record: CanonicalRecord) -> CanonicalRecord:
    return replace(record, flags=record.flags | RecordFlag.LAST)
