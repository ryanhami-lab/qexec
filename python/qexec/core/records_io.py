"""Session storage format shared by the synthetic generator and the source adapter.

A session directory contains:

* ``records.parquet``   - CanonicalRecord rows in source order (``source_ordinal`` ascending)
* ``status.parquet``    - StatusEvent rows in time order
* ``instrument.json``   - InstrumentDefinition
* ``session.json``      - session metadata (session_id, start/end ns, generator params)
* ``checksums.json``    - SHA-256 of every file above

Raw session files are immutable once written; readers verify checksums.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from qexec.core.types import (
    Action,
    CanonicalRecord,
    InstrumentDefinition,
    PriceFixed,
    RecordFlag,
    Side,
    StatusEvent,
    TimeNs,
    TradingStatus,
)

RECORD_SCHEMA: dict[str, Any] = {
    "dataset_id": pl.Utf8,
    "publisher_id": pl.Int64,
    "instrument_id": pl.Int64,
    "session_epoch": pl.Int64,
    "source_ordinal": pl.Int64,
    "action": pl.Utf8,
    "side": pl.Utf8,
    "price_fixed": pl.Int64,
    "quantity": pl.Int64,
    "order_id": pl.Int64,
    "event_time_ns": pl.Int64,
    "capture_time_ns": pl.Int64,
    "flags": pl.Int64,
    "source_file_hash": pl.Utf8,
    "source_record_offset": pl.Int64,
    "channel_id": pl.Int64,
    "source_sequence": pl.Int64,
    "schema_version": pl.Int64,
}

STATUS_SCHEMA: dict[str, Any] = {
    "instrument_id": pl.Int64,
    "status": pl.Utf8,
    "event_time_ns": pl.Int64,
    "capture_time_ns": pl.Int64,
}

CHUNK_ROWS = 100_000

REQUIRED_SESSION_FILES: tuple[str, ...] = (
    "records.parquet",
    "status.parquet",
    "instrument.json",
    "session.json",
)
"""Every session file that must be present and checksummed. ``checksums.json`` itself is the
manifest (not self-hashed). A session is only valid if a checksum exists for each of these and
every digest matches (R15: an empty or partial ``checksums.json`` is rejected, not vacuously
accepted)."""


@dataclass(frozen=True, slots=True)
class SessionMeta:
    session_id: str
    dataset_id: str
    start_ns: int
    end_ns: int
    params: dict[str, Any]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_session(
    directory: Path,
    meta: SessionMeta,
    instrument: InstrumentDefinition,
    records: Iterable[CanonicalRecord],
    status: Iterable[StatusEvent],
) -> None:
    """Write an immutable session directory. Refuses to overwrite existing files."""
    directory.mkdir(parents=True, exist_ok=True)
    targets = list(REQUIRED_SESSION_FILES)
    for name in targets:
        if (directory / name).exists():
            raise FileExistsError(f"refusing to overwrite immutable session file {name}")

    rows = [
        {
            "dataset_id": r.dataset_id,
            "publisher_id": r.publisher_id,
            "instrument_id": r.instrument_id,
            "session_epoch": r.session_epoch,
            "source_ordinal": r.source_ordinal,
            "action": r.action.value,
            "side": r.side.value,
            "price_fixed": r.price_fixed,
            "quantity": r.quantity,
            "order_id": r.order_id,
            "event_time_ns": int(r.event_time_ns),
            "capture_time_ns": int(r.capture_time_ns),
            "flags": int(r.flags),
            "source_file_hash": r.source_file_hash,
            "source_record_offset": r.source_record_offset,
            "channel_id": r.channel_id,
            "source_sequence": r.source_sequence,
            "schema_version": r.schema_version,
        }
        for r in records
    ]
    pl.DataFrame(rows, schema=RECORD_SCHEMA).write_parquet(directory / "records.parquet")

    srows = [
        {
            "instrument_id": s.instrument_id,
            "status": s.status.value,
            "event_time_ns": int(s.event_time_ns),
            "capture_time_ns": int(s.capture_time_ns),
        }
        for s in status
    ]
    pl.DataFrame(srows, schema=STATUS_SCHEMA).write_parquet(directory / "status.parquet")

    (directory / "instrument.json").write_text(
        json.dumps(
            {
                "instrument_id": instrument.instrument_id,
                "symbol": instrument.symbol,
                "tick_size_fixed": int(instrument.tick_size_fixed),
                "multiplier": instrument.multiplier,
                "effective_time_ns": int(instrument.effective_time_ns),
            },
            indent=2,
        )
    )
    (directory / "session.json").write_text(
        json.dumps(
            {
                "session_id": meta.session_id,
                "dataset_id": meta.dataset_id,
                "start_ns": meta.start_ns,
                "end_ns": meta.end_ns,
                "params": meta.params,
            },
            indent=2,
            sort_keys=True,
        )
    )
    checksums = {name: _sha256(directory / name) for name in targets}
    (directory / "checksums.json").write_text(json.dumps(checksums, indent=2, sort_keys=True))


def verify_session(directory: Path) -> dict[str, str]:
    """Verify checksums; raise ``ValueError`` on any mismatch or missing coverage.

    Every file in :data:`REQUIRED_SESSION_FILES` must appear in ``checksums.json`` and match its
    recorded digest. An empty or partial manifest is rejected rather than passing vacuously
    (R15): a truncated session with no (or incomplete) checksums must never validate as OK.
    """
    checksums_path = directory / "checksums.json"
    if not checksums_path.is_file():
        raise ValueError(f"missing checksums.json for session {directory}")
    raw = json.loads(checksums_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"checksums.json for {directory} must contain a checksum object")
    expected: dict[str, str] = raw
    missing_coverage = [name for name in REQUIRED_SESSION_FILES if name not in expected]
    if missing_coverage:
        raise ValueError(
            f"checksums.json for {directory} is missing required file checksums: {missing_coverage}"
        )
    unexpected = sorted(set(expected) - set(REQUIRED_SESSION_FILES))
    if unexpected:
        raise ValueError(f"unexpected checksum filenames for {directory}: {unexpected}")
    for name, digest in expected.items():
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"invalid SHA-256 checksum for {name}")
        file_path = directory / name
        if not file_path.is_file():
            raise ValueError(f"checksummed file missing on disk: {file_path}")
        actual = _sha256(file_path)
        if actual != digest:
            raise ValueError(f"checksum mismatch for {file_path}")
    return expected


def read_meta(directory: Path) -> SessionMeta:
    raw = json.loads((directory / "session.json").read_text())
    if not isinstance(raw, dict):
        raise ValueError("session.json must contain an object")
    sid = raw.get("session_id")
    if not isinstance(sid, str) or not sid or Path(sid).name != sid or sid in (".", ".."):
        raise ValueError("session_id must be a single nonempty filename")
    dataset = raw.get("dataset_id")
    if not isinstance(dataset, str) or not dataset.strip():
        raise ValueError("session dataset_id must be nonempty")
    for name in ("start_ns", "end_ns"):
        value = raw.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"session {name} must be a nonnegative integer")
    if raw["end_ns"] < raw["start_ns"] or not isinstance(raw.get("params"), dict):
        raise ValueError("session requires end >= start and an object of parameters")
    return SessionMeta(
        raw["session_id"], raw["dataset_id"], raw["start_ns"], raw["end_ns"], raw["params"]
    )


def read_instrument(directory: Path) -> InstrumentDefinition:
    raw = json.loads((directory / "instrument.json").read_text())
    if not isinstance(raw, dict):
        raise ValueError("instrument.json must contain an object")
    for name in ("instrument_id", "tick_size_fixed", "multiplier"):
        value = raw.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if not isinstance(raw.get("symbol"), str) or not raw["symbol"].strip():
        raise ValueError("symbol must be a nonempty string")
    return InstrumentDefinition(
        instrument_id=raw["instrument_id"],
        symbol=raw["symbol"],
        tick_size_fixed=PriceFixed(raw["tick_size_fixed"]),
        multiplier=raw["multiplier"],
        effective_time_ns=TimeNs(raw["effective_time_ns"]),
    )


def read_status(directory: Path) -> list[StatusEvent]:
    df = pl.read_parquet(directory / "status.parquet")
    instrument_id = read_instrument(directory).instrument_id
    for row in df.iter_rows(named=True):
        if row["instrument_id"] != instrument_id:
            raise ValueError("status instrument_id does not match session instrument")
        event_time, capture_time = row["event_time_ns"], row["capture_time_ns"]
        if (
            not isinstance(event_time, int)
            or not isinstance(capture_time, int)
            or event_time < 0
            or capture_time < event_time
        ):
            raise ValueError("status requires nonnegative event time and capture >= event")
    return [
        StatusEvent(
            instrument_id=row["instrument_id"],
            status=TradingStatus(row["status"]),
            event_time_ns=TimeNs(row["event_time_ns"]),
            capture_time_ns=TimeNs(row["capture_time_ns"]),
        )
        for row in df.iter_rows(named=True)
    ]


def iter_records(directory: Path, chunk_rows: int = CHUNK_ROWS) -> Iterator[CanonicalRecord]:
    """Stream records in stored (source) order in bounded chunks."""
    lf = pl.scan_parquet(directory / "records.parquet")
    total = lf.select(pl.len()).collect().item()
    for offset in range(0, total, chunk_rows):
        chunk = lf.slice(offset, chunk_rows).collect()
        for row in chunk.iter_rows(named=True):
            yield CanonicalRecord(
                dataset_id=row["dataset_id"],
                publisher_id=row["publisher_id"],
                instrument_id=row["instrument_id"],
                session_epoch=row["session_epoch"],
                source_ordinal=row["source_ordinal"],
                action=Action(row["action"]),
                side=Side(row["side"]),
                price_fixed=row["price_fixed"],
                quantity=row["quantity"],
                order_id=row["order_id"],
                event_time_ns=TimeNs(row["event_time_ns"]),
                capture_time_ns=TimeNs(row["capture_time_ns"]),
                flags=RecordFlag(row["flags"]),
                source_file_hash=row["source_file_hash"],
                source_record_offset=row["source_record_offset"],
                channel_id=row["channel_id"],
                source_sequence=row["source_sequence"],
                schema_version=row["schema_version"],
            )
