"""R9 reproduction (labels plane): status events create their own samples on a merged
status/book timeline, and ``price_direction_label`` returns None beyond coverage or inside a
halt.

External review R9: ``MidSeries`` applied status only when a book batch arrived, so a halt that
began in a quiet gap (no batches during the gap) was ignored until the next batch; and
``price_direction_label`` used ``mid_at`` (last valid at-or-before ``t``), so a label whose
``t*+tau`` landed inside a halt or beyond the last sample returned a stale UNCHANGED/valid mid
instead of ``None``.

Fix: both the series and the manifest use a merged status/book timeline. A status event creates
its own sample (mid ``None`` while not TRADING). ``mid_at`` reports the *state at* ``t`` (``None``
if that state is invalid). ``price_direction_label`` returns ``None`` if ``t*+tau`` is beyond the
last sample time / session end, or either endpoint's state is invalid.
"""

from __future__ import annotations

from pathlib import Path

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
from qexec.labels.price import MidSeries, price_direction_label

TICK = 250_000_000
S = 1_000_000_000
MS = 1_000_000
BID = 100_000_000_000
ASK = 100_250_000_000
INSTRUMENT_ID = 1


def _instrument() -> InstrumentDefinition:
    return InstrumentDefinition(
        instrument_id=INSTRUMENT_ID,
        symbol="SYNZ6",
        tick_size_fixed=PriceFixed(TICK),
        multiplier=50,
        effective_time_ns=TimeNs(0),
    )


def _rec(
    ordinal: int,
    action: Action,
    side: Side,
    price: int,
    qty: int,
    oid: int,
    *,
    t: int,
    flags: RecordFlag = RecordFlag.NONE,
) -> CanonicalRecord:
    return CanonicalRecord(
        dataset_id="SYNTH.MBO",
        publisher_id=1,
        instrument_id=INSTRUMENT_ID,
        session_epoch=0,
        source_ordinal=ordinal,
        action=action,
        side=side,
        price_fixed=price,
        quantity=qty,
        order_id=oid,
        event_time_ns=TimeNs(t),
        capture_time_ns=TimeNs(t),
        flags=flags | RecordFlag.LAST,
        source_file_hash="",
        source_record_offset=ordinal,
    )


# ----------------------------------------------------------- mid_at: state at t, status samples


def test_mid_at_reports_state_at_t_none_during_halt() -> None:
    # Merged timeline: t=10 valid 200, t=20 HALT sample (None), t=30 TRADING sample 210.
    # mid_at(25) must be None (the state at 25 is the halt), not the stale 200.
    s = MidSeries([10, 20, 30], [200, None, 210])
    assert s.mid_at(10) == 200
    assert s.mid_at(15) == 200
    assert s.mid_at(20) is None
    assert s.mid_at(25) is None  # state at 25 is the halt (regression: previously returned 200)
    assert s.mid_at(30) == 210


# ----------------------------------------------------------- labels beyond coverage / in halt


def test_label_none_when_target_beyond_last_sample() -> None:
    s = MidSeries([0, 100], [100, 110])
    assert price_direction_label(s, 0, 150, TICK) is None  # 0+150 = 150 > 100 (last sample)


def test_label_none_when_target_inside_halt() -> None:
    s = MidSeries([0, 200], [100, None])
    assert price_direction_label(s, 0, 200, TICK) is None  # target lands on the halt sample


def test_label_none_when_origin_inside_halt() -> None:
    s = MidSeries([0, 200], [None, 100])
    assert price_direction_label(s, 0, 50, TICK) is None  # origin state invalid


def test_normal_unchanged_case_is_still_zero() -> None:
    s = MidSeries([0, 100, 200], [100, 100, 100])
    assert price_direction_label(s, 0, 100, TICK) == 0


# --------------------------------------------- quiet-gap halt from a hand-built session (merged)


def _session_with_quiet_gap_halt(tmp_path: Path) -> tuple[Path, int, int]:
    """Trade, HALT during a quiet gap (no batches), then resume. Returns (dir, trading, halt)."""
    start = 1_000_000_000_000
    recs: list[CanonicalRecord] = []
    status: list[StatusEvent] = []
    o = 0
    status.append(StatusEvent(INSTRUMENT_ID, TradingStatus.TRADING, TimeNs(start), TimeNs(start)))
    # Opening snapshot (initialization batch): two-sided book.
    for price, side, oid in ((BID, Side.BID, 1), (ASK, Side.ASK, 2)):
        recs.append(_rec(o, Action.ADD, side, price, 10, oid, t=start, flags=RecordFlag.SNAPSHOT))
        o += 1
    trading_t = start + 1 * S
    recs.append(_rec(o, Action.ADD, Side.BID, BID - TICK, 1, 100, t=trading_t))
    o += 1
    recs.append(_rec(o, Action.CANCEL, Side.BID, BID - TICK, 1, 100, t=trading_t + 10 * MS))
    o += 1
    # HALT begins in a quiet gap: no batches between halt_t and resume_t.
    halt_t = start + 5 * S
    status.append(StatusEvent(INSTRUMENT_ID, TradingStatus.HALTED, TimeNs(halt_t), TimeNs(halt_t)))
    resume_t = start + 20 * S
    status.append(
        StatusEvent(INSTRUMENT_ID, TradingStatus.TRADING, TimeNs(resume_t), TimeNs(resume_t))
    )
    recs.append(_rec(o, Action.ADD, Side.BID, BID - TICK, 1, 200, t=resume_t))
    o += 1
    recs.append(_rec(o, Action.CANCEL, Side.BID, BID - TICK, 1, 200, t=resume_t + 10 * MS))
    o += 1
    meta = SessionMeta("SYN-R9GAP", "SYNTH.MBO", start, resume_t + 10 * MS, {})
    sd = tmp_path / "SYN-R9GAP"
    write_session(sd, meta, _instrument(), recs, status)
    return sd, trading_t, halt_t


def test_series_has_status_sample_in_quiet_gap(tmp_path: Path) -> None:
    sd, trading_t, halt_t = _session_with_quiet_gap_halt(tmp_path)
    series = MidSeries.from_session(sd)
    assert series.mid_at(trading_t) is not None  # trading before the halt
    # The halt begins in a quiet gap; the merged timeline must create a None sample at halt_t,
    # so a lookup inside the gap (no batches) is None, not the stale pre-halt mid.
    assert series.mid_at(halt_t + 1 * S) is None
    # A label whose target lands inside the quiet-gap halt returns None.
    tau = (halt_t + 2 * S) - trading_t
    assert price_direction_label(series, trading_t, tau, TICK) is None
