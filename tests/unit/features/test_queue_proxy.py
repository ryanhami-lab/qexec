"""Tests for :class:`qexec.features.queue_proxy.QueueCohortProxy` (hand-computed, T54).

Scenarios:
* 5 ahead across two orders (3 + 2); cancel 3 of the first -> q_ahead_est 2, q_depleted 3,
  q_depleted_frac 0.6.
* A FILL+CANCEL execution triplet reduces the cohort exactly once (the book applies it once).
* T54: an add present in the (true) book before arrival but not yet delivered at decision time
  is counted only as uncertain, never as ahead; an add delivered after expected arrival is
  behind (ignored).
* A priority-losing MODIFY removes a cohort member.
* copy independence.
"""

from __future__ import annotations

from dataclasses import replace

from _fhelpers import TICK, instrument, make_batch, rec

from qexec.core.types import Action, Side, TaskSide, TimeNs
from qexec.features.queue_proxy import QueueCohortProxy
from qexec.reference.book import ReferenceBook

BID = 100_000_000_000  # buy task rests here
S = 1_000_000_000


def _buy_book_with_cohort() -> ReferenceBook:
    """Client book with two bid orders at the limit: order 1 qty 3, order 2 qty 2 (5 total)."""
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1))
    book.apply_record(rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=2, order_id=2))
    return book


# --------------------------------------------------------------------------- hand-computed cohort


def test_hand_computed_cohort_cancel_three_of_first() -> None:
    book = _buy_book_with_cohort()
    proxy = QueueCohortProxy(
        side=TaskSide.BUY,
        limit_price_fixed=BID,
        client_book_at_decision=book,
        decision_time_ns=10 * S,
        expected_arrival_ns=10 * S,  # no uncertain window
        last_delivered_proxy_ns=10 * S,
    )
    # Initial: cohort qty = 3 + 2 = 5.
    f0 = proxy.features(book)
    assert f0["q_ahead_est"] == 5.0
    assert f0["q_depleted"] == 0.0
    assert f0["q_depleted_frac"] == 0.0

    # Deliver a batch cancelling 3 of order 1 (full removal of the 3-lot order).
    book.apply_record(rec(2, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1))
    batch = make_batch(
        [rec(2, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1)],
        batch_id=1,
    )
    proxy.on_delivered(batch, book)

    f1 = proxy.features(book)
    # q_ahead_est 2 (only order 2's 2 lots remain), q_depleted 5 - 2 = 3, frac 3/5 = 0.6.
    assert f1["q_ahead_est"] == 2.0
    assert f1["q_depleted"] == 3.0
    assert f1["q_depleted_frac"] == 0.6
    # No uncertain adds in this scenario.
    assert f1["q_insertion_uncertain"] == 0.0
    assert f1["q_ahead_upper"] == 2.0


def test_fill_cancel_triplet_reduces_cohort_once() -> None:
    book = _buy_book_with_cohort()  # order 1 qty 3, order 2 qty 2
    proxy = QueueCohortProxy(
        side=TaskSide.BUY,
        limit_price_fixed=BID,
        client_book_at_decision=book,
        decision_time_ns=10 * S,
        expected_arrival_ns=10 * S,
        last_delivered_proxy_ns=10 * S,
    )
    # Execution triplet consuming order 1's full 3 lots: TRADE + FILL + CANCEL(qty 3). The book
    # applies only the CANCEL, so the cohort drops by exactly 3 (counted once).
    recs = [
        rec(2, Action.TRADE, side=Side.ASK, price_fixed=BID, quantity=3, order_id=0),
        rec(3, Action.FILL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
        rec(4, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1),
    ]
    for r in recs:
        book.apply_record(r)
    batch = make_batch(recs, batch_id=1)
    proxy.on_delivered(batch, book)
    f = proxy.features(book)
    assert f["q_ahead_est"] == 2.0  # 5 - 3, exactly once
    assert f["q_depleted"] == 3.0


# --------------------------------------------------------------------------- priority loss


def test_priority_losing_modify_removes_member() -> None:
    book = _buy_book_with_cohort()  # order 1 qty 3, order 2 qty 2
    proxy = QueueCohortProxy(
        side=TaskSide.BUY,
        limit_price_fixed=BID,
        client_book_at_decision=book,
        decision_time_ns=10 * S,
        expected_arrival_ns=10 * S,
        last_delivered_proxy_ns=10 * S,
    )
    # MODIFY order 1 with a size *increase* (3 -> 6) loses priority (new priority_seq) -> member
    # leaves the cohort entirely (it is now behind our hypothetical order).
    book.apply_record(rec(2, Action.MODIFY, side=Side.BID, price_fixed=BID, quantity=6, order_id=1))
    batch = make_batch(
        [rec(2, Action.MODIFY, side=Side.BID, price_fixed=BID, quantity=6, order_id=1)],
        batch_id=1,
    )
    proxy.on_delivered(batch, book)
    f = proxy.features(book)
    # Only order 2's 2 lots remain in the cohort; order 1 removed despite still being at the price.
    assert f["q_ahead_est"] == 2.0
    assert f["q_depleted"] == 3.0


def test_price_move_removes_member() -> None:
    book = _buy_book_with_cohort()
    proxy = QueueCohortProxy(
        side=TaskSide.BUY,
        limit_price_fixed=BID,
        client_book_at_decision=book,
        decision_time_ns=10 * S,
        expected_arrival_ns=10 * S,
        last_delivered_proxy_ns=10 * S,
    )
    # MODIFY order 2 to a worse price (down a tick) -> leaves the limit level entirely.
    book.apply_record(
        rec(2, Action.MODIFY, side=Side.BID, price_fixed=BID - TICK, quantity=2, order_id=2)
    )
    batch = make_batch(
        [rec(2, Action.MODIFY, side=Side.BID, price_fixed=BID - TICK, quantity=2, order_id=2)],
        batch_id=1,
    )
    proxy.on_delivered(batch, book)
    f = proxy.features(book)
    assert f["q_ahead_est"] == 3.0  # only order 1 remains


# --------------------------------------------------------------------------- T54


def test_t54_undelivered_prior_add_is_uncertain_not_ahead() -> None:
    """An add that exists in the TRUE book before arrival but was not yet delivered at decision
    time must be counted as uncertain, not as ahead; a later-delivered add after expected
    arrival is behind (ignored)."""
    # Client book at decision time: a single resting bid (order 1 qty 3) at the limit.
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1))

    decision_time = 10 * S
    last_delivered_proxy = 10 * S  # last batch the client has seen (proxy time)
    expected_arrival = 11 * S  # modeled arrival of our hypothetical order

    proxy = QueueCohortProxy(
        side=TaskSide.BUY,
        limit_price_fixed=BID,
        client_book_at_decision=book,
        decision_time_ns=decision_time,
        expected_arrival_ns=expected_arrival,
        last_delivered_proxy_ns=last_delivered_proxy,
    )
    # Cohort frozen at the delivered truth: 3 lots ahead.
    assert proxy.features(book)["q_ahead_est"] == 3.0

    # A historical ADD (order 7, qty 4) actually occurred at exchange proxy time 10.5s -- before
    # our expected arrival (11s) but AFTER the client's last delivered batch (10s). It is only
    # now delivered. Because its proxy time lies in (10s, 11s], it is UNCERTAIN, not ahead.
    book.apply_record(rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=7))
    uncertain_batch = make_batch(
        [rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=7)],
        batch_id=1,
    )
    # The batch's exchange_proxy_time is set via the record's event_time_ns.
    uncertain_batch = _retime(uncertain_batch, proxy_time=10 * S + 500_000_000)
    proxy.on_delivered(uncertain_batch, book)

    f = proxy.features(book)
    assert f["q_ahead_est"] == 3.0  # cohort unchanged: the add is NOT counted as ahead
    assert f["q_insertion_uncertain"] == 4.0  # tracked as uncertain
    assert f["q_ahead_upper"] == 7.0  # 3 + 4

    # Another ADD (order 8, qty 5) delivered with proxy time AFTER expected arrival (11.5s) is
    # definitely behind -> ignored by every band.
    book.apply_record(rec(2, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=8))
    behind_batch = make_batch(
        [rec(2, Action.ADD, side=Side.BID, price_fixed=BID, quantity=5, order_id=8)],
        batch_id=2,
    )
    behind_batch = _retime(behind_batch, proxy_time=11 * S + 500_000_000)
    proxy.on_delivered(behind_batch, book)

    f2 = proxy.features(book)
    assert f2["q_ahead_est"] == 3.0
    assert f2["q_insertion_uncertain"] == 4.0  # unchanged; order 8 is behind
    assert f2["q_ahead_upper"] == 7.0


def test_uncertain_add_then_cancelled_reduces_upper_band() -> None:
    book = ReferenceBook(instrument())
    book.apply_record(rec(0, Action.ADD, side=Side.BID, price_fixed=BID, quantity=3, order_id=1))
    proxy = QueueCohortProxy(
        side=TaskSide.BUY,
        limit_price_fixed=BID,
        client_book_at_decision=book,
        decision_time_ns=10 * S,
        expected_arrival_ns=11 * S,
        last_delivered_proxy_ns=10 * S,
    )
    book.apply_record(rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=7))
    b1 = _retime(
        make_batch(
            [rec(1, Action.ADD, side=Side.BID, price_fixed=BID, quantity=4, order_id=7)],
            batch_id=1,
        ),
        proxy_time=10 * S + 500_000_000,
    )
    proxy.on_delivered(b1, book)
    assert proxy.features(book)["q_insertion_uncertain"] == 4.0

    # The uncertain add is partially cancelled (4 -> 1); upper band tracks the book qty.
    book.apply_record(rec(2, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=7))
    b2 = _retime(
        make_batch(
            [rec(2, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=7)],
            batch_id=2,
        ),
        proxy_time=10 * S + 700_000_000,
    )
    proxy.on_delivered(b2, book)
    f = proxy.features(book)
    assert f["q_insertion_uncertain"] == 1.0
    assert f["q_ahead_upper"] == 4.0  # 3 cohort + 1 uncertain


# --------------------------------------------------------------------------- copy


def test_copy_is_independent() -> None:
    book = _buy_book_with_cohort()
    proxy = QueueCohortProxy(
        side=TaskSide.BUY,
        limit_price_fixed=BID,
        client_book_at_decision=book,
        decision_time_ns=10 * S,
        expected_arrival_ns=10 * S,
        last_delivered_proxy_ns=10 * S,
    )
    clone = proxy.copy()
    # Cancel order 1 (qty 3) and feed only the clone.
    book2 = book.copy()
    book2.apply_record(
        rec(2, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1)
    )
    batch = make_batch(
        [rec(2, Action.CANCEL, side=Side.BID, price_fixed=BID, quantity=3, order_id=1)],
        batch_id=1,
    )
    clone.on_delivered(batch, book2)
    # Original (evaluated against the untouched book) still sees the full cohort.
    assert proxy.features(book)["q_ahead_est"] == 5.0
    assert clone.features(book2)["q_ahead_est"] == 2.0


# --------------------------------------------------------------------------- helpers


def _retime(batch, *, proxy_time):  # type: ignore[no-untyped-def]
    return replace(batch, exchange_proxy_time=TimeNs(proxy_time))
