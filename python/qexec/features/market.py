"""Causal market features from delivered batches (engineering contract WP-FEATURES).

:class:`MarketFeatureState` is fed every batch the client receives, in delivery order, together
with the client :class:`~qexec.reference.book.ReferenceBook` committed after that batch and the
*observation time* (the time the client saw the batch, i.e. its delivery time). All windows are
**trailing over observation time**: a sample taken at observation time ``o`` contributes to the
feature value computed at ``now_ns`` iff ``o <= now_ns`` and ``o`` is within the trailing window
``(now_ns - window_ns, now_ns]``. Because features never read any batch with observation time
after ``now_ns``, perturbing the delivered stream only after some time ``t`` cannot change any
feature value computed at a time ``<= t`` (leakage test T22).

Flow-statistic conventions
--------------------------
* ``INITIALIZATION`` batches (snapshot (re)builds) are never economic order flow and update no
  flow statistic, quote-age timestamp, or mid series.
* **Trade flow** is taken from ``TRADE`` records; the aggressor side signs the quantity, with
  ``BID`` (aggressor buying) ``+qty`` and ``ASK`` (aggressor selling) ``-qty``.
* **Cancel quantity** is taken from ``CANCEL`` records that are *not* execution-driven. An
  execution is emitted as a ``FILL`` immediately followed by the ``CANCEL`` that reduces the
  same resting order by the same quantity (contract 2.1); such a cancel is a book reduction
  caused by a trade, not a true cancellation, so it is excluded. The reduction is already
  reflected in the trade flow via its ``TRADE`` record, so it is counted once overall.
* **Add quantity** is taken from (non-snapshot) ``ADD`` records, bucketed by side.

Undefined-value convention
--------------------------
Every feature is a finite ``float``. When a quantity is undefined we use documented sentinels:

* ``imbalance_1``/``imbalance_3`` are ``(b - a) / (b + a)``; when both sides are empty the
  denominator is zero and the imbalance is defined to be ``0.0`` (balanced-by-convention).
* ``spread_ticks`` is defined only when both best levels exist; when either side is empty it is
  ``0.0`` by convention (a NaN sentinel is explicitly disallowed by the contract). A spread of
  ``0.0`` is otherwise unreachable in a non-crossed book (minimum real spread is one tick), so
  ``0.0`` unambiguously flags "one side missing".
* ``quote_age_ms`` before any delivered quote change is ``0.0``.
* ``mid_vol_5s`` with fewer than two committed mid samples in the window is ``0.0``.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable

from qexec.core.types import (
    Action,
    BatchKind,
    EventBatch,
    InstrumentDefinition,
    Side,
)
from qexec.reference.book import ReferenceBook


class MarketFeatureState:
    """Trailing-window causal market-feature accumulator fed delivered batches in order."""

    def __init__(
        self,
        instrument: InstrumentDefinition,
        *,
        flow_window_ns: int = 1_000_000_000,
        vol_window_ns: int = 5_000_000_000,
    ) -> None:
        self._instrument = instrument
        self._tick = instrument.tick_size_fixed
        self._flow_window_ns = flow_window_ns
        self._vol_window_ns = vol_window_ns
        # Each flow sample is (observation_time_ns, value). Trade flow stores signed qty; the
        # trade-count deque stores a sample per TRADE record. Cancel/add store per-side qty.
        self._trade_flow: deque[tuple[int, int]] = deque()
        self._trade_count: deque[tuple[int, int]] = deque()
        self._cancel_bid: deque[tuple[int, int]] = deque()
        self._cancel_ask: deque[tuple[int, int]] = deque()
        self._add_bid: deque[tuple[int, int]] = deque()
        self._add_ask: deque[tuple[int, int]] = deque()
        # Committed mid samples (observation_time_ns, mid2) for volatility and mid-change.
        self._mids: deque[tuple[int, int]] = deque()
        # Observation time of the last delivered batch that changed the best bid/ask price or
        # size; -1 means no quote change observed yet.
        self._last_quote_change_ns: int = -1
        # Last committed best (bid_price, bid_qty, ask_price, ask_qty) seen, to detect changes.
        self._last_best: tuple[int | None, int | None, int | None, int | None] = (
            None,
            None,
            None,
            None,
        )

    # -- ingestion -------------------------------------------------------------------

    def on_delivered(
        self,
        batch: EventBatch,
        client_book_after: ReferenceBook,
        observation_time_ns: int,
    ) -> None:
        """Consume one delivered ``batch`` committed into ``client_book_after``.

        ``observation_time_ns`` is the time the client observed the batch (its delivery time);
        it stamps every sample for trailing-window eviction. ``INITIALIZATION`` batches update
        nothing.
        """
        if batch.kind is BatchKind.INITIALIZATION:
            return

        trade_signed, trade_n, cancel_b, cancel_a, add_b, add_a = self._scan_records(batch.records)
        if trade_signed:
            self._trade_flow.append((observation_time_ns, trade_signed))
        if trade_n:
            self._trade_count.append((observation_time_ns, trade_n))
        if cancel_b:
            self._cancel_bid.append((observation_time_ns, cancel_b))
        if cancel_a:
            self._cancel_ask.append((observation_time_ns, cancel_a))
        if add_b:
            self._add_bid.append((observation_time_ns, add_b))
        if add_a:
            self._add_ask.append((observation_time_ns, add_a))

        self._update_quote_and_mid(client_book_after, observation_time_ns)

    def _scan_records(self, records: Iterable[object]) -> tuple[int, int, int, int, int, int]:
        """Return (signed_trade_qty, trade_count, cancel_bid, cancel_ask, add_bid, add_ask).

        A ``CANCEL`` immediately preceded by a ``FILL`` of the same order in the same batch is
        execution-driven and excluded from cancel quantity.
        """
        trade_signed = 0
        trade_n = 0
        cancel_b = cancel_a = 0
        add_b = add_a = 0
        prev_fill_key: object | None = None
        recs = list(records)
        for rec in recs:
            action = rec.action  # type: ignore[attr-defined]
            if action is Action.TRADE:
                side = rec.side  # type: ignore[attr-defined]
                qty = rec.quantity  # type: ignore[attr-defined]
                if side is Side.BID:
                    trade_signed += qty
                elif side is Side.ASK:
                    trade_signed -= qty
                trade_n += 1
                prev_fill_key = None
            elif action is Action.FILL:
                prev_fill_key = rec.order_key()  # type: ignore[attr-defined]
            elif action is Action.CANCEL:
                execution_driven = prev_fill_key is not None and prev_fill_key == (
                    rec.order_key()  # type: ignore[attr-defined]
                )
                if not execution_driven:
                    side = rec.side  # type: ignore[attr-defined]
                    qty = rec.quantity  # type: ignore[attr-defined]
                    if side is Side.BID:
                        cancel_b += qty
                    elif side is Side.ASK:
                        cancel_a += qty
                prev_fill_key = None
            elif action is Action.ADD:
                side = rec.side  # type: ignore[attr-defined]
                qty = rec.quantity  # type: ignore[attr-defined]
                if side is Side.BID:
                    add_b += qty
                elif side is Side.ASK:
                    add_a += qty
                prev_fill_key = None
            else:
                prev_fill_key = None
        return trade_signed, trade_n, cancel_b, cancel_a, add_b, add_a

    def _update_quote_and_mid(self, book: ReferenceBook, observation_time_ns: int) -> None:
        bid, ask = book.best_bid_ask()
        bid_price = bid.price_fixed if bid is not None else None
        bid_qty = bid.quantity if bid is not None else None
        ask_price = ask.price_fixed if ask is not None else None
        ask_qty = ask.quantity if ask is not None else None
        best = (bid_price, bid_qty, ask_price, ask_qty)
        if best != self._last_best:
            self._last_quote_change_ns = observation_time_ns
            self._last_best = best
        if bid_price is not None and ask_price is not None and bid_price < ask_price:
            self._mids.append((observation_time_ns, bid_price + ask_price))

    # -- feature computation ---------------------------------------------------------

    @staticmethod
    def _window_sum(samples: deque[tuple[int, int]], now_ns: int, window_ns: int) -> int:
        lo = now_ns - window_ns
        total = 0
        for obs, value in samples:
            if lo < obs <= now_ns:
                total += value
        return total

    def _evict(self, now_ns: int) -> None:
        """Drop samples whose observation time is strictly older than the widest window.

        Eviction is purely a memory bound; the per-feature window checks remain exact. Samples
        with observation time ``> now_ns`` are never evicted and never counted (causality).
        """
        widest = max(self._flow_window_ns, self._vol_window_ns)
        lo = now_ns - widest
        for dq in (
            self._trade_flow,
            self._trade_count,
            self._cancel_bid,
            self._cancel_ask,
            self._add_bid,
            self._add_ask,
            self._mids,
        ):
            while dq and dq[0][0] <= lo:
                dq.popleft()

    def features(self, now_ns: int, client_book: ReferenceBook) -> dict[str, float]:
        """Compute the causal feature dict at ``now_ns`` from the current ``client_book``."""
        self._evict(now_ns)

        bid, ask = client_book.best_bid_ask()
        bid_qty_1 = float(bid.quantity) if bid is not None else 0.0
        ask_qty_1 = float(ask.quantity) if ask is not None else 0.0

        if bid is not None and ask is not None:
            spread_ticks = (ask.price_fixed - bid.price_fixed) / self._tick
        else:
            spread_ticks = 0.0

        imbalance_1 = self._imbalance(bid_qty_1, ask_qty_1)

        depth_bid_3 = self._depth(client_book, Side.BID, 3)
        depth_ask_3 = self._depth(client_book, Side.ASK, 3)
        imbalance_3 = self._imbalance(depth_bid_3, depth_ask_3)

        if self._last_quote_change_ns < 0:
            quote_age_ms = 0.0
        else:
            quote_age_ms = max(now_ns - self._last_quote_change_ns, 0) / 1_000_000.0

        flow = self._flow_window_ns
        trade_flow_signed_1s = float(self._window_sum(self._trade_flow, now_ns, flow))
        trade_count_1s = float(self._window_sum(self._trade_count, now_ns, flow))
        cancel_qty_bid_1s = float(self._window_sum(self._cancel_bid, now_ns, flow))
        cancel_qty_ask_1s = float(self._window_sum(self._cancel_ask, now_ns, flow))
        add_qty_bid_1s = float(self._window_sum(self._add_bid, now_ns, flow))
        add_qty_ask_1s = float(self._window_sum(self._add_ask, now_ns, flow))

        mid_vol_5s = self._mid_vol(now_ns)
        mid_change_1s_ticks = self._mid_change(now_ns)

        out = {
            "spread_ticks": float(spread_ticks),
            "bid_qty_1": bid_qty_1,
            "ask_qty_1": ask_qty_1,
            "imbalance_1": imbalance_1,
            "depth_bid_3": float(depth_bid_3),
            "depth_ask_3": float(depth_ask_3),
            "imbalance_3": imbalance_3,
            "quote_age_ms": float(quote_age_ms),
            "trade_flow_signed_1s": trade_flow_signed_1s,
            "trade_count_1s": trade_count_1s,
            "cancel_qty_bid_1s": cancel_qty_bid_1s,
            "cancel_qty_ask_1s": cancel_qty_ask_1s,
            "add_qty_bid_1s": add_qty_bid_1s,
            "add_qty_ask_1s": add_qty_ask_1s,
            "mid_vol_5s": mid_vol_5s,
            "mid_change_1s_ticks": mid_change_1s_ticks,
        }
        # Every feature must be a finite float (contract WP-FEATURES).
        assert all(math.isfinite(v) for v in out.values())
        return out

    @staticmethod
    def _imbalance(bid_qty: float, ask_qty: float) -> float:
        denom = bid_qty + ask_qty
        if denom <= 0.0:
            return 0.0
        return (bid_qty - ask_qty) / denom

    @staticmethod
    def _depth(book: ReferenceBook, side: Side, levels: int) -> int:
        snap = book.snapshot(depth=levels)
        side_levels = snap.bids if side is Side.BID else snap.asks
        return sum(level.quantity for level in side_levels[:levels])

    def _mids_in_window(self, now_ns: int, window_ns: int) -> list[tuple[int, int]]:
        lo = now_ns - window_ns
        return [(obs, mid2) for obs, mid2 in self._mids if lo < obs <= now_ns]

    def _mid_vol(self, now_ns: int) -> float:
        samples = self._mids_in_window(now_ns, self._vol_window_ns)
        if len(samples) < 2:
            return 0.0
        # Consecutive committed mid changes in ticks (mid2 is twice the mid, so a change in
        # ticks is delta(mid2) / (2 * tick)).
        changes = [
            (samples[i][1] - samples[i - 1][1]) / (2.0 * self._tick) for i in range(1, len(samples))
        ]
        n = len(changes)
        mean = sum(changes) / n
        var = sum((c - mean) ** 2 for c in changes) / n  # population std (ddof = 0)
        return math.sqrt(var)

    def _mid_change(self, now_ns: int) -> float:
        samples = self._mids_in_window(now_ns, self._flow_window_ns)
        if len(samples) < 2:
            return 0.0
        first_mid2 = samples[0][1]
        last_mid2 = samples[-1][1]
        return (last_mid2 - first_mid2) / (2.0 * self._tick)

    # -- copy ------------------------------------------------------------------------

    def copy(self) -> MarketFeatureState:
        """Return a deep, independent copy; mutating the copy never touches the original."""
        clone = MarketFeatureState(
            self._instrument,
            flow_window_ns=self._flow_window_ns,
            vol_window_ns=self._vol_window_ns,
        )
        clone._trade_flow = deque(self._trade_flow)
        clone._trade_count = deque(self._trade_count)
        clone._cancel_bid = deque(self._cancel_bid)
        clone._cancel_ask = deque(self._cancel_ask)
        clone._add_bid = deque(self._add_bid)
        clone._add_ask = deque(self._add_ask)
        clone._mids = deque(self._mids)
        clone._last_quote_change_ns = self._last_quote_change_ns
        clone._last_best = self._last_best
        return clone
