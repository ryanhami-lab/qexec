"""Untrusted source metadata must not bypass session and causal-timing gates."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest

from qexec.adapters.batches import iter_batches
from qexec.core.config import ExperimentConfig
from qexec.core.records_io import (
    STATUS_SCHEMA,
    SessionMeta,
    read_instrument,
    read_meta,
    read_status,
    verify_session,
    write_session,
)
from qexec.core.types import (
    Action,
    CanonicalRecord,
    InstrumentDefinition,
    PriceFixed,
    QualityFlag,
    RecordFlag,
    Side,
    TimeNs,
)


def _record(ordinal: int = 0) -> CanonicalRecord:
    return CanonicalRecord(
        dataset_id="SYNTH.MBO",
        publisher_id=1,
        instrument_id=1,
        session_epoch=0,
        source_ordinal=ordinal,
        action=Action.ADD,
        side=Side.BID,
        price_fixed=100_000_000_000,
        quantity=1,
        order_id=ordinal + 1,
        event_time_ns=TimeNs(100),
        capture_time_ns=TimeNs(110),
        flags=RecordFlag.LAST,
        source_file_hash="fixture",
        source_record_offset=ordinal,
    )


def _session(tmp_path: Path) -> Path:
    directory = tmp_path / "session"
    write_session(
        directory,
        SessionMeta("session", "SYNTH.MBO", 0, 1000, {}),
        InstrumentDefinition(1, "SYN", PriceFixed(250_000_000), 50, TimeNs(0)),
        [_record()],
        [],
    )
    return directory


@pytest.mark.parametrize("payload", [[], None, "bad", 42])
def test_checksum_manifest_requires_an_object(tmp_path: Path, payload: object) -> None:
    directory = _session(tmp_path)
    (directory / "checksums.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="checksum"):
        verify_session(directory)


def test_checksum_manifest_cannot_read_outside_session(tmp_path: Path) -> None:
    directory = _session(tmp_path)
    path = directory / "checksums.json"
    checksums = json.loads(path.read_text())
    checksums["../outside.txt"] = "0" * 64
    path.write_text(json.dumps(checksums))
    with pytest.raises(ValueError, match=r"unexpected|filename"):
        verify_session(directory)


def test_mixed_instrument_stream_is_rejected() -> None:
    with pytest.raises(ValueError, match="instrument"):
        list(iter_batches([_record(), replace(_record(1), instrument_id=2)], 1))


def test_mixed_epoch_stream_is_rejected() -> None:
    with pytest.raises(ValueError, match="epoch"):
        list(iter_batches([_record(), replace(_record(1), session_epoch=1)], 1))


def test_bad_receive_timestamp_flag_invalidates_batch() -> None:
    record = replace(_record(), flags=RecordFlag.LAST | RecordFlag.BAD_TS_RECV)
    assert QualityFlag.INVALID_TIMESTAMP in next(iter_batches([record], 1)).quality_flags


def test_negative_timestamp_is_not_hidden_by_batch_maximum() -> None:
    records = [replace(_record(), event_time_ns=TimeNs(-1), flags=RecordFlag.NONE), _record(1)]
    assert QualityFlag.INVALID_TIMESTAMP in next(iter_batches(records, 1)).quality_flags


def test_per_record_capture_before_event_is_not_hidden_by_batch_maximum() -> None:
    records = [replace(_record(), capture_time_ns=TimeNs(90), flags=RecordFlag.NONE), _record(1)]
    assert QualityFlag.CAPTURE_BEFORE_PROXY in next(iter_batches(records, 1)).quality_flags


@pytest.mark.parametrize("key", ["tick_size_fixed", "multiplier"])
def test_invalid_instrument_units_are_rejected(tmp_path: Path, key: str) -> None:
    directory = _session(tmp_path)
    path = directory / "instrument.json"
    raw = json.loads(path.read_text())
    raw[key] = 0
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=key):
        read_instrument(directory)


@pytest.mark.parametrize(
    "changes",
    [
        {"horizon_ns": 1e9},
        {"warmup_ns": -1},
        {"sides": ()},
        {"sides": (1, 1)},
        {"session_window_start_s": -1},
        {"task_seed": True},
    ],
)
def test_execution_config_rejects_ambiguous_or_invalid_inputs(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ExperimentConfig.from_dict({"experiment_id": "fixture", **changes})


@pytest.mark.parametrize(
    "changes", [{"instrument_id": 2}, {"event_time_ns": -1}, {"capture_time_ns": 50}]
)
def test_status_records_cannot_bypass_instrument_or_time_validation(
    tmp_path: Path, changes: dict[str, int]
) -> None:
    directory = _session(tmp_path)
    row = {
        "instrument_id": 1,
        "status": "TRADING",
        "event_time_ns": 100,
        "capture_time_ns": 110,
        **changes,
    }
    pl.DataFrame([row], schema=STATUS_SCHEMA).write_parquet(directory / "status.parquet")
    with pytest.raises(ValueError, match="status"):
        read_status(directory)


@pytest.mark.parametrize(
    "changes", [{"session_id": "../outside"}, {"end_ns": -1}, {"start_ns": 0.5}, {"params": None}]
)
def test_session_metadata_is_validated(tmp_path: Path, changes: dict[str, object]) -> None:
    directory = _session(tmp_path)
    path = directory / "session.json"
    raw = json.loads(path.read_text())
    raw.update(changes)
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="session"):
        read_meta(directory)
