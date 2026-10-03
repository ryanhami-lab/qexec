"""Unit tests for the batch assembler (:mod:`qexec.adapters.batches`).

Covers validation matrix:
* T02 — equal timestamps, different source order: source order retained.
* T03 — non-mutating final boundary record closes the batch correctly.

Plus required extras: batch quality flags (DECREASING_PROXY_TIME, CAPTURE_BEFORE_PROXY,
TRUNCATED) and INITIALIZATION vs LIVE batch kind.
"""

from __future__ import annotations

from dataclasses import replace

from _helpers import rec

from qexec.adapters.batches import BatchAssembler, iter_batches
from qexec.core.types import (
    Action,
    BatchKind,
    QualityFlag,
    RecordFlag,
    Side,
)

BID = 100_000_000_000
ASK = 100_250_000_000


def _last(r):  # type: ignore[no-untyped-def]
    return replace(r, flags=r.flags | RecordFlag.LAST)


# --------------------------------------------------------------------------- T02


def test_equal_timestamps_retain_source_order() -> None:
    # Three adds at the same event_time, differing only in source_ordinal/order_id.
    recs = [
        rec(
            0,
            Action.ADD,
            side=Side.BID,
            price_fixed=BID,
            quantity=1,
            order_id=10,
            event_time_ns=5_000,
        ),
        rec(
            1,
            Action.ADD,
            side=Side.BID,
            price_fixed=BID,
            quantity=1,
            order_id=11,
            event_time_ns=5_000,
        ),
        _last(
            rec(
                2,
                Action.ADD,
                side=Side.BID,
                price_fixed=BID,
                quantity=1,
                order_id=12,
                event_time_ns=5_000,
            )
        ),
    ]
    batches = list(iter_batches(recs, instrument_id=1))
    assert len(batches) == 1
    # Source order (by source_ordinal) is retained exactly; we do not re-sort by time.
    assert [r.source_ordinal for r in batches[0].records] == [0, 1, 2]
    assert [r.order_id for r in batches[0].records] == [10, 11, 12]


# --------------------------------------------------------------------------- T03


def test_non_mutating_final_boundary_record_closes_batch() -> None:
    # ADD then a non-mutating TRADE record carries LAST and closes the event.
    recs = [
        rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1),
        _last(rec(1, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=5, order_id=0)),
    ]
    batches = list(iter_batches(recs, instrument_id=1))
    assert len(batches) == 1
    assert batches[0].records[-1].action is Action.TRADE
    assert batches[0].records[-1].is_last


# -------------------------------------------------- batch kind


def test_batch_kind_initialization_all_snapshot() -> None:
    recs = [
        rec(
            0,
            Action.ADD,
            side=Side.BID,
            price_fixed=BID,
            quantity=1,
            order_id=1,
            flags=RecordFlag.SNAPSHOT,
        ),
        _last(
            rec(
                1,
                Action.ADD,
                side=Side.ASK,
                price_fixed=ASK,
                quantity=1,
                order_id=2,
                flags=RecordFlag.SNAPSHOT,
            )
        ),
    ]
    batches = list(iter_batches(recs, instrument_id=1))
    assert batches[0].kind is BatchKind.INITIALIZATION


def test_batch_kind_initialization_clear_then_snapshot_adds() -> None:
    recs = [
        rec(0, Action.CLEAR, side=Side.NONE),
        rec(
            1,
            Action.ADD,
            side=Side.BID,
            price_fixed=BID,
            quantity=1,
            order_id=1,
            flags=RecordFlag.SNAPSHOT,
        ),
        _last(
            rec(
                2,
                Action.ADD,
                side=Side.ASK,
                price_fixed=ASK,
                quantity=1,
                order_id=2,
                flags=RecordFlag.SNAPSHOT,
            )
        ),
    ]
    batches = list(iter_batches(recs, instrument_id=1))
    assert batches[0].kind is BatchKind.INITIALIZATION


def test_batch_kind_live_when_non_snapshot_present() -> None:
    recs = [
        rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=1, order_id=1),
        _last(rec(1, Action.ADD, side=Side.ASK, price_fixed=ASK, quantity=1, order_id=2)),
    ]
    batches = list(iter_batches(recs, instrument_id=1))
    assert batches[0].kind is BatchKind.LIVE


# -------------------------------------------------- quality flags


def test_quality_flag_truncated_trailing_batch() -> None:
    assembler = BatchAssembler(instrument_id=1)
    # First complete batch.
    assert assembler.feed(_last(rec(0, Action.ADD, order_id=1))) is not None
    # Trailing records with no LAST.
    assert assembler.feed(rec(1, Action.ADD, order_id=2)) is None
    assert assembler.feed(rec(2, Action.ADD, order_id=3)) is None
    truncated = assembler.finish()
    assert truncated is not None
    assert QualityFlag.TRUNCATED in truncated.quality_flags
    # Fully-consumed assembler yields nothing more.
    assert assembler.finish() is None


def test_quality_flag_decreasing_proxy_time() -> None:
    recs = [
        _last(rec(0, Action.ADD, order_id=1, event_time_ns=10_000)),
        _last(rec(1, Action.ADD, order_id=2, event_time_ns=5_000)),  # proxy time goes down
    ]
    batches = list(iter_batches(recs, instrument_id=1))
    assert QualityFlag.DECREASING_PROXY_TIME not in batches[0].quality_flags
    assert QualityFlag.DECREASING_PROXY_TIME in batches[1].quality_flags


def test_quality_flag_capture_before_proxy() -> None:
    # capture_time < event_time within the batch.
    recs = [
        _last(rec(0, Action.ADD, order_id=1, event_time_ns=10_000, capture_time_ns=9_000)),
    ]
    batches = list(iter_batches(recs, instrument_id=1))
    assert QualityFlag.CAPTURE_BEFORE_PROXY in batches[0].quality_flags


def test_proxy_and_capture_times_are_maxima() -> None:
    recs = [
        rec(0, Action.ADD, order_id=1, event_time_ns=10_000, capture_time_ns=11_000),
        _last(rec(1, Action.ADD, order_id=2, event_time_ns=20_000, capture_time_ns=22_000)),
    ]
    batch = next(iter(iter_batches(recs, instrument_id=1)))
    assert batch.exchange_proxy_time == 20_000
    assert batch.capture_complete_time == 22_000
