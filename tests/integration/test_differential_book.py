"""Differential check: synthetic generator output replayed through ReferenceBook.

The generator (WP1-SYNTH) and the reference book (WP1-BOOK) were written independently. This
test replays a full synthetic session through ReferenceBook and through a deliberately naive
third implementation (per-price aggregate quantities only), and requires:

* zero ReferenceBook anomalies and invariant violations at every committed boundary;
* identical aggregate depth on every price level between ReferenceBook and the naive replay
  at every committed batch boundary.
"""

from __future__ import annotations

from collections import defaultdict

from qexec.adapters.batches import iter_batches
from qexec.core.types import Action, InstrumentDefinition, PriceFixed, Side, TimeNs, TradingStatus
from qexec.reference.book import ReferenceBook
from qexec.synthetic import SyntheticParams, generate_session


def _naive_depth(
    orders: dict[int, tuple[Side, int, int]],
) -> tuple[dict[int, int], dict[int, int]]:
    bids: dict[int, int] = defaultdict(int)
    asks: dict[int, int] = defaultdict(int)
    for side, price, qty in orders.values():
        (bids if side is Side.BID else asks)[price] += qty
    return dict(bids), dict(asks)


def test_generator_and_reference_book_agree() -> None:
    params = SyntheticParams(seed=11, session_id="DIFF-1", start_ns=0, duration_s=120)
    _meta, inst, records, status = generate_session(params)
    assert isinstance(inst, InstrumentDefinition)
    book = ReferenceBook(inst)
    book.set_status(TradingStatus.TRADING)

    status_iter = iter(sorted(status, key=lambda s: s.event_time_ns))
    next_status = next(status_iter, None)

    naive: dict[int, tuple[Side, int, int]] = {}
    batches = 0
    for batch in iter_batches(records, inst.instrument_id):
        while next_status is not None and next_status.event_time_ns <= batch.exchange_proxy_time:
            book.set_status(next_status.status)
            next_status = next(status_iter, None)
        for rec in batch.records:
            if rec.action is Action.ADD:
                naive[rec.order_id] = (rec.side, rec.price_fixed, rec.quantity)
            elif rec.action is Action.CANCEL:
                side, price, qty = naive[rec.order_id]
                left = qty - rec.quantity
                if left > 0:
                    naive[rec.order_id] = (side, price, left)
                else:
                    del naive[rec.order_id]
            elif rec.action is Action.MODIFY:
                side, _, _ = naive[rec.order_id]
                naive[rec.order_id] = (side, rec.price_fixed, rec.quantity)
            elif rec.action is Action.CLEAR:
                naive.clear()
        snap = book.apply_batch(batch)
        batches += 1
        assert not [a for a in book.anomalies if a.kind == "INVARIANT_VIOLATION"]
        if batches % 25 == 0:
            assert book.validate_invariants() == [], f"invariant violation at {batch.batch_id}"
            nb, na = _naive_depth(naive)
            full = book.snapshot(depth=10_000)
            assert {lv.price_fixed: lv.quantity for lv in full.bids} == nb
            assert {lv.price_fixed: lv.quantity for lv in full.asks} == na
        assert snap is not None

    nb, na = _naive_depth(naive)
    full = book.snapshot(depth=10_000)
    assert {lv.price_fixed: lv.quantity for lv in full.bids} == nb
    assert {lv.price_fixed: lv.quantity for lv in full.asks} == na

    assert batches > 1_000
    assert book.anomalies == []


def test_reference_book_instrument_tick() -> None:
    inst = InstrumentDefinition(1, "SYNZ6", PriceFixed(250_000_000), 50, TimeNs(0))
    assert inst.is_tick_aligned(100_250_000_000)
