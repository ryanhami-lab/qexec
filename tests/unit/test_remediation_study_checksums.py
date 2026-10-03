"""R15 reproduction: checksum coverage and replay-validate quality-flag failure.

The external review found two holes:

1. ``verify_session`` iterated the *recorded* checksum map, so an **empty** ``checksums.json``
   verified vacuously -- a truncated session with no checksums passed.
2. ``qexec replay validate`` reported ``RESULT: OK`` on data carrying batch quality flags
   (TRUNCATED / corrupt), because only book anomalies/invariants gated the exit code.

The fix requires a checksum for *every* required session file, and fails replay validate (exit 1)
on any batch quality flag.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from qexec.core.records_io import (
    REQUIRED_SESSION_FILES,
    SessionMeta,
    verify_session,
    write_session,
)
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


def _rec(i: int, action: Action, flags: RecordFlag = RecordFlag.LAST) -> CanonicalRecord:
    return CanonicalRecord(
        dataset_id="SYNTH.MBO",
        publisher_id=1,
        instrument_id=7,
        session_epoch=0,
        source_ordinal=i,
        action=action,
        side=Side.BID,
        price_fixed=100_000_000_000,
        quantity=3,
        order_id=100 + i,
        event_time_ns=TimeNs(1_000 + i),
        capture_time_ns=TimeNs(2_000 + i),
        flags=flags,
        source_file_hash="",
        source_record_offset=i,
    )


def _write_session(tmp_path: Path) -> Path:
    d = tmp_path / "SYN-0001"
    inst = InstrumentDefinition(7, "SYN", PriceFixed(250_000_000), 50, TimeNs(0))
    recs = [_rec(0, Action.ADD, RecordFlag.SNAPSHOT), _rec(1, Action.ADD)]
    status = [StatusEvent(7, TradingStatus.TRADING, TimeNs(0), TimeNs(1))]
    meta = SessionMeta("SYN-0001", "SYNTH.MBO", 0, 10, {"seed": 1})
    write_session(d, meta, inst, recs, status)
    return d


def test_empty_checksums_json_is_rejected(tmp_path: Path) -> None:
    d = _write_session(tmp_path)
    (d / "checksums.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        verify_session(d)


def test_missing_required_file_checksum_is_rejected(tmp_path: Path) -> None:
    d = _write_session(tmp_path)
    checksums = json.loads((d / "checksums.json").read_text())
    # Drop one required file's checksum (simulate a truncated manifest).
    checksums.pop("records.parquet", None)
    (d / "checksums.json").write_text(json.dumps(checksums))
    with pytest.raises(ValueError, match=r"records\.parquet"):
        verify_session(d)


def test_required_session_files_are_declared() -> None:
    # The required-file set is explicit so a truncated session cannot pass vacuously.
    assert set(REQUIRED_SESSION_FILES) == {
        "records.parquet",
        "status.parquet",
        "instrument.json",
        "session.json",
    }


def test_good_session_still_verifies(tmp_path: Path) -> None:
    d = _write_session(tmp_path)
    assert set(verify_session(d)) >= set(REQUIRED_SESSION_FILES)
    # A full round-trip copy also verifies.
    copy = tmp_path / "copy"
    shutil.copytree(d, copy)
    verify_session(copy)
