from __future__ import annotations

from pathlib import Path

import pytest

from qexec.core.records_io import (
    SessionMeta,
    iter_records,
    read_instrument,
    read_status,
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


def test_roundtrip_and_immutability(tmp_path: Path) -> None:
    inst = InstrumentDefinition(7, "SYN", PriceFixed(250_000_000), 50, TimeNs(0))
    recs = [_rec(0, Action.ADD, RecordFlag.SNAPSHOT), _rec(1, Action.ADD), _rec(2, Action.CANCEL)]
    status = [StatusEvent(7, TradingStatus.TRADING, TimeNs(0), TimeNs(1))]
    meta = SessionMeta("s1", "SYNTH.MBO", 0, 10, {"seed": 1})
    write_session(tmp_path, meta, inst, recs, status)

    assert list(iter_records(tmp_path, chunk_rows=2)) == recs
    assert read_instrument(tmp_path) == inst
    assert read_status(tmp_path) == status
    verify_session(tmp_path)

    with pytest.raises(FileExistsError):
        write_session(tmp_path, meta, inst, recs, status)

    (tmp_path / "instrument.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_session(tmp_path)
