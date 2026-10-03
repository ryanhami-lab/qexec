"""Unit tests for pre-replay diagnostics (:mod:`qexec.adapters.diagnostics`).

Covers validation matrix:
* T05 — missing records / invalid state: diagnostic and explicit treatment (here, the
  book's own anomaly path for an unknown cancel and an impossible reduction).
* T35 — duplicate records / overlapping files: explicit diagnostic; no double mutation.
"""

from __future__ import annotations

from pathlib import Path

from _helpers import instrument, rec

from qexec.adapters.diagnostics import (
    detect_duplicate_records,
    detect_overlapping_sessions,
)
from qexec.core.records_io import SessionMeta, write_session
from qexec.core.types import (
    Action,
    OrderKey,
    RecordFlag,
    Side,
    StatusEvent,
    TimeNs,
    TradingStatus,
)
from qexec.reference.book import ReferenceBook

BID = 100_000_000_000


# --------------------------------------------------------------------------- T35


def test_detect_duplicate_by_ordinal() -> None:
    recs = [
        rec(0, Action.ADD, order_id=1),
        rec(0, Action.ADD, order_id=1),  # same (instrument, source_ordinal)
    ]
    anomalies = detect_duplicate_records(recs)
    assert any(a.kind == "DUPLICATE_ORDINAL" for a in anomalies)


def test_detect_duplicate_by_content_and_offset() -> None:
    recs = [
        rec(0, Action.ADD, order_id=1, source_file_hash="h", source_record_offset=42),
        # Different ordinal but identical content at the same file offset -> same record.
        rec(1, Action.ADD, order_id=1, source_file_hash="h", source_record_offset=42),
    ]
    anomalies = detect_duplicate_records(recs)
    assert any(a.kind == "DUPLICATE_CONTENT" for a in anomalies)


def test_no_duplicate_for_distinct_records() -> None:
    recs = [
        rec(0, Action.ADD, order_id=1, source_record_offset=1),
        rec(1, Action.ADD, order_id=2, source_record_offset=2),
    ]
    assert detect_duplicate_records(recs) == []


def test_duplicate_detection_prevents_double_mutation() -> None:
    # If a duplicate ADD is applied twice, the book flags DUPLICATE_ADD and does not
    # double-count. This is the book-level guard complementing pre-replay detection.
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    assert book.best_bid_ask()[0].quantity == 5  # not 10 # type: ignore[union-attr]
    assert any(a.kind == "DUPLICATE_ADD" for a in book.anomalies)


def test_detect_overlapping_sessions(tmp_path: Path) -> None:
    def _write(name: str, start: int, end: int) -> Path:
        d = tmp_path / name
        inst = instrument()
        recs = [
            rec(0, Action.ADD, order_id=1, event_time_ns=start, capture_time_ns=start),
            rec(
                1,
                Action.ADD,
                order_id=2,
                event_time_ns=end,
                capture_time_ns=end,
                flags=RecordFlag.LAST,
            ),
        ]
        status = [StatusEvent(1, TradingStatus.TRADING, TimeNs(start), TimeNs(start))]
        meta = SessionMeta(name, "SYNTH.MBO", start, end, {})
        write_session(d, meta, inst, recs, status)
        return d

    a = _write("s_a", 1_000, 5_000)
    b = _write("s_b", 4_000, 9_000)  # overlaps [4000,5000] with a
    c = _write("s_c", 20_000, 25_000)  # disjoint

    overlaps = detect_overlapping_sessions([a, b, c])
    assert len(overlaps) == 1
    assert overlaps[0].kind == "OVERLAPPING_SESSIONS"


# --------------------------------------------------------------------------- T05


def test_unknown_cancel_is_diagnosed_not_silently_applied() -> None:
    book = ReferenceBook(instrument())
    book.apply_record(
        rec(0, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=5, order_id=99)
    )
    assert any(a.kind == "CANCEL_UNKNOWN" for a in book.anomalies)
    assert book.best_bid_ask() == (None, None)


def test_impossible_reduction_is_diagnosed_and_capped() -> None:
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1))
    # Cancel more than resting: diagnosed, order removed (capped to resting qty).
    book.apply_record(rec(1, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=5, order_id=1))
    assert any(a.kind == "CANCEL_OVER_QTY" for a in book.anomalies)
    assert book.get_order(OrderKey(1, 1, 0, 1)) is None
    assert book.best_bid_ask() == (None, None)
