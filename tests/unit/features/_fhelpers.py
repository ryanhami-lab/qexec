"""Shared record/batch builders for WP-FEATURES tests (owned by this package).

These mirror the minimal builders used elsewhere but live under the feature test tree so this
work package does not depend on another package's test helpers.
"""

from __future__ import annotations

from dataclasses import replace

from qexec.adapters.batches import BatchAssembler
from qexec.core.types import (
    Action,
    BatchKind,
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
) -> CanonicalRecord:
    """Build a :class:`CanonicalRecord` with sensible defaults for feature tests."""
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


def make_batch(
    records: list[CanonicalRecord],
    *,
    batch_id: int = 0,
    kind: BatchKind | None = None,
) -> EventBatch:
    """Assemble exactly one :class:`EventBatch` from ``records`` (last record marked LAST).

    If ``kind`` is given, it overrides the assembler's classification (used to force a LIVE or
    INITIALIZATION batch in tests). ``batch_id`` is overridden onto the emitted batch.
    """
    assembler = BatchAssembler(INSTRUMENT_ID)
    marked = list(records)
    marked[-1] = replace(marked[-1], flags=marked[-1].flags | RecordFlag.LAST)
    out: EventBatch | None = None
    for r in marked:
        emitted = assembler.feed(r)
        if emitted is not None:
            out = emitted
    assert out is not None, "records did not close a batch"
    overrides: dict[str, object] = {"batch_id": batch_id}
    if kind is not None:
        overrides["kind"] = kind
    return replace(out, **overrides)  # type: ignore[arg-type]
