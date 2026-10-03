"""Boundary-mode commit validation (linear cost) still detects corruption it is meant to see."""

from __future__ import annotations

import pytest

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
    TradingStatus,
)
from qexec.reference.book import ReferenceBook

INST = InstrumentDefinition(1, "SYNZ6", PriceFixed(250_000_000), 50, TimeNs(0))
P100 = 100_000_000_000
P10025 = 100_250_000_000


def _rec(i: int, action: Action, side: Side, price: int, qty: int, oid: int) -> CanonicalRecord:
    return CanonicalRecord(
        "SYNTH.MBO", 1, 1, 0, i, action, side, price, qty, oid,
        TimeNs(i), TimeNs(i + 1), RecordFlag.LAST, "", i,
    )  # fmt: skip


def _batch(i: int, *recs: CanonicalRecord) -> EventBatch:
    return EventBatch(i, 1, recs, TimeNs(i), TimeNs(i + 1), BatchKind.LIVE)


def _violations(book: ReferenceBook) -> list[str]:
    return [a.detail for a in book.anomalies if a.kind == "INVARIANT_VIOLATION"]


def test_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        ReferenceBook(INST, commit_validation="sometimes")


@pytest.mark.parametrize("mode", ["boundary", "full"])
def test_clean_flow_has_no_violations(mode: str) -> None:
    book = ReferenceBook(INST, commit_validation=mode)
    book.set_status(TradingStatus.TRADING)
    book.apply_batch(_batch(1, _rec(1, Action.ADD, Side.BID, P100, 5, 10)))
    book.apply_batch(_batch(2, _rec(2, Action.ADD, Side.ASK, P10025, 5, 11)))
    # Priority-losing MODIFY moves order 10 from 100.00 to 99.75 (old level emptied).
    book.apply_batch(_batch(3, _rec(3, Action.MODIFY, Side.BID, P100 - 250_000_000, 5, 10)))
    assert _violations(book) == []
    assert book.validate_invariants() == []


def test_boundary_mode_detects_corruption_on_modify_old_level() -> None:
    book = ReferenceBook(INST)  # boundary mode by default
    book.set_status(TradingStatus.TRADING)
    book.apply_batch(_batch(1, _rec(1, Action.ADD, Side.BID, P100, 5, 10)))
    book.apply_batch(_batch(2, _rec(2, Action.ADD, Side.BID, P100, 3, 12)))
    # Corrupt the 100.00 level total directly (simulating an internal bug).
    book._bid_levels[P100].total_quantity += 1
    # A MODIFY moves order 10 away from 100.00; its OLD level must still be validated.
    book.apply_batch(_batch(3, _rec(3, Action.MODIFY, Side.BID, P100 - 250_000_000, 5, 10)))
    assert any("total" in v and str(P100) in v for v in _violations(book))


def test_boundary_mode_detects_crossing() -> None:
    book = ReferenceBook(INST)
    book.set_status(TradingStatus.TRADING)
    book.apply_batch(_batch(1, _rec(1, Action.ADD, Side.ASK, P100, 5, 11)))
    book.apply_batch(_batch(2, _rec(2, Action.ADD, Side.BID, P10025, 5, 10)))
    assert any("crossed" in v for v in _violations(book))


def test_copy_preserves_mode() -> None:
    book = ReferenceBook(INST, commit_validation="full")
    assert book.copy()._commit_validation == "full"
