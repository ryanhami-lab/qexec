"""R15 reproduction (CLI): ``qexec replay validate`` fails on any batch quality flag.

A session whose final record lacks ``RecordFlag.LAST`` produces a ``TRUNCATED`` batch quality
flag. The reviewer found that replay validate still reported ``RESULT: OK`` because only book
anomalies / invariants gated the exit code. After the fix the command exits 1 and prints a
quality-flag result on such a session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qexec.cli.main import EXIT_FAILURE, EXIT_OK, main
from qexec.core.records_io import SessionMeta, write_session
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


def _rec(i: int, flags: RecordFlag) -> CanonicalRecord:
    return CanonicalRecord(
        dataset_id="SYNTH.MBO",
        publisher_id=1,
        instrument_id=7,
        session_epoch=0,
        source_ordinal=i,
        action=Action.ADD,
        side=Side.BID,
        price_fixed=100_000_000_000 - i,
        quantity=3,
        order_id=100 + i,
        event_time_ns=TimeNs(1_000 + i),
        capture_time_ns=TimeNs(2_000 + i),
        flags=flags,
        source_file_hash="",
        source_record_offset=i,
    )


def _write_truncated_session(root: Path) -> Path:
    """Write a session whose trailing run has no LAST record -> TRUNCATED quality flag."""
    d = root / "SYN-TRUNC"
    inst = InstrumentDefinition(7, "SYN", PriceFixed(250_000_000), 50, TimeNs(0))
    # First record snapshot-init closed; final record NOT flagged LAST -> truncated trailing run.
    recs = [
        _rec(0, RecordFlag.SNAPSHOT | RecordFlag.LAST),
        _rec(1, RecordFlag.NONE),  # no LAST -> trailing run truncated
    ]
    status = [StatusEvent(7, TradingStatus.TRADING, TimeNs(0), TimeNs(1))]
    meta = SessionMeta("SYN-TRUNC", "SYNTH.MBO", 0, 10, {"seed": 1})
    write_session(d, meta, inst, recs, status)
    return d


def test_replay_validate_fails_on_truncated_quality_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _write_truncated_session(tmp_path)
    code = main(["replay", "validate", "--session", str(session)])
    captured = capsys.readouterr()
    assert code == EXIT_FAILURE, captured.out
    assert "QUALITY FLAGS DETECTED" in captured.out
    assert "RESULT: OK" not in captured.out


def test_replay_validate_clean_session_still_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "SYN-CLEAN"
    inst = InstrumentDefinition(7, "SYN", PriceFixed(250_000_000), 50, TimeNs(0))
    recs = [
        _rec(0, RecordFlag.SNAPSHOT | RecordFlag.LAST),
        _rec(1, RecordFlag.LAST),
    ]
    status = [StatusEvent(7, TradingStatus.TRADING, TimeNs(0), TimeNs(1))]
    write_session(d, SessionMeta("SYN-CLEAN", "SYNTH.MBO", 0, 10, {}), inst, recs, status)
    code = main(["replay", "validate", "--session", str(d)])
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.out
    assert "batch quality flags: none" in captured.out
    assert "RESULT: OK" in captured.out
