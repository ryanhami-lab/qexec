"""Pre-replay anomaly diagnostics (architecture sections 4.3, 5; validation T35, T36).

These detectors run *before* the book is mutated. They find duplicate records and
overlapping acquisition files so a replay never double-counts a mutation or a fill. They
do not deduplicate by timestamp alone (a sequence number or timestamp is not a unique
record key, architecture section 4.1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from qexec.core.records_io import iter_records, read_meta
from qexec.core.types import CanonicalRecord


@dataclass(frozen=True, slots=True)
class Anomaly:
    """A diagnosed irregularity. ``source_ordinal`` is -1 when not tied to one record."""

    kind: str
    batch_id: int
    source_ordinal: int
    detail: str


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    """A group of records judged to be duplicates of one another."""

    reason: str
    instrument_id: int
    source_ordinals: tuple[int, ...]
    detail: str


def _identity_key(record: CanonicalRecord) -> tuple[int, int]:
    """``(instrument_id, source_ordinal)`` — the normalized per-instrument identity."""
    return (record.instrument_id, record.source_ordinal)


def _content_key(
    record: CanonicalRecord,
) -> tuple[int, int, str, int]:
    """Identical content *and* source offset => the same physical record seen twice.

    Keyed on ``(instrument_id, order_id, source_file_hash, source_record_offset)``: two
    records from the same file at the same byte offset are the same record, not two
    independent economic events that merely coincide in time.
    """
    return (
        record.instrument_id,
        record.order_id,
        record.source_file_hash,
        record.source_record_offset,
    )


def detect_duplicate_records(records: Iterable[CanonicalRecord]) -> list[Anomaly]:
    """Detect duplicate records: same ``(instrument, source_ordinal)`` or identical
    content at the same source offset. Order is preserved; never dedupe by timestamp.
    """
    anomalies: list[Anomaly] = []
    seen_identity: dict[tuple[int, int], int] = {}
    seen_content: dict[tuple[int, int, str, int], int] = {}
    for record in records:
        ident = _identity_key(record)
        if ident in seen_identity:
            anomalies.append(
                Anomaly(
                    kind="DUPLICATE_ORDINAL",
                    batch_id=-1,
                    source_ordinal=record.source_ordinal,
                    detail=(
                        f"instrument {record.instrument_id} source_ordinal "
                        f"{record.source_ordinal} already seen at ordinal "
                        f"{seen_identity[ident]}"
                    ),
                )
            )
        else:
            seen_identity[ident] = record.source_ordinal

        content = _content_key(record)
        # Only flag content duplicates when the offset is a real (non-sentinel) location.
        if content in seen_content and record.source_file_hash:
            anomalies.append(
                Anomaly(
                    kind="DUPLICATE_CONTENT",
                    batch_id=-1,
                    source_ordinal=record.source_ordinal,
                    detail=(
                        f"identical content+offset as ordinal {seen_content[content]} "
                        f"(file {record.source_file_hash[:8]}@{record.source_record_offset})"
                    ),
                )
            )
        elif record.source_file_hash:
            seen_content[content] = record.source_ordinal
    return anomalies


def detect_overlapping_sessions(session_dirs: Sequence[Path]) -> list[Anomaly]:
    """Detect acquisition files whose event-time windows overlap for one instrument.

    Overlapping windows mean the same market activity may have been captured twice; the
    manager must reconcile them before replay rather than silently concatenating.
    """
    anomalies: list[Anomaly] = []
    windows: list[tuple[Path, int, int, int]] = []  # (dir, instrument_id, start, end)
    for directory in session_dirs:
        meta = read_meta(directory)
        by_instrument: dict[int, tuple[int, int]] = {}
        for record in iter_records(directory):
            start, end = by_instrument.get(
                record.instrument_id, (record.event_time_ns, record.event_time_ns)
            )
            by_instrument[record.instrument_id] = (
                min(start, record.event_time_ns),
                max(end, record.event_time_ns),
            )
        for instrument_id, (start, end) in by_instrument.items():
            windows.append((directory, instrument_id, start, end))
        _ = meta  # meta loaded to validate the directory layout exists

    for i in range(len(windows)):
        dir_a, inst_a, start_a, end_a = windows[i]
        for j in range(i + 1, len(windows)):
            dir_b, inst_b, start_b, end_b = windows[j]
            if inst_a != inst_b:
                continue
            if start_a <= end_b and start_b <= end_a:
                anomalies.append(
                    Anomaly(
                        kind="OVERLAPPING_SESSIONS",
                        batch_id=-1,
                        source_ordinal=-1,
                        detail=(
                            f"instrument {inst_a}: {dir_a.name} [{start_a},{end_a}] overlaps "
                            f"{dir_b.name} [{start_b},{end_b}]"
                        ),
                    )
                )
    return anomalies
