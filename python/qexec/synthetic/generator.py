"""Seeded, deterministic synthetic limit-order-book event generator.

This module emits :class:`~qexec.core.types.CanonicalRecord` sequences that emulate the
Databento CME MBO convention described in the engineering contract section 2.1. It keeps
its own independent internal book (it never imports ``qexec.reference``) and produces data
suitable for software validation only -- never for market findings.

Market model (documented, intentionally simple)
------------------------------------------------
The book is maintained in integer *tick indices* (an absolute price is
``tick_index * tick_size_fixed``). A single discrete-event loop advances a monotone
``event_time_ns`` clock by exponentially distributed inter-arrival gaps drawn from a seeded
:class:`numpy.random.Generator`. At each step one of four economic actions fires, chosen by
fixed relative rates:

* **limit add** -- a new resting order arrives near the touch. Its depth away from the best
  price follows a geometric profile (:attr:`SyntheticParams.depth_decay`), its size is a
  geometric draw around :attr:`SyntheticParams.mean_level_qty`. Adds that would cross the
  book are clamped to join at (or just outside) the touch, keeping the spread near one tick.
* **cancel** -- an existing resting order is reduced. With probability
  :attr:`SyntheticParams.full_cancel_prob` the whole order is pulled; otherwise a partial
  quantity is removed (priority retained, per contract section 2.1).
* **modify** -- an existing resting order is modified. With probability
  :attr:`SyntheticParams.modify_priority_retain_prob` it is a pure size *decrease* (priority
  retained); otherwise it is a price move or size increase (priority lost, moved to the back
  of the new level), exercising both MODIFY branches.
* **market order** -- an aggressive order crosses the spread and sweeps one or more price
  levels (a trade-through when more than one level is consumed). Its side is drawn from a
  logistic tilt combining a latent persistent drift state and the current top-of-book
  imbalance, so that short-horizon mid moves are partially predictable from imbalance (the
  learnable signal the research pipeline needs).

A latent drift state ``d`` follows an AR(1)-like persistent process
(:attr:`SyntheticParams.drift_persistence`); the probability that a market order is a buy is
``sigmoid(drift_gain * d + imbalance_gain * imbalance)``. Whenever a side of the book becomes
thin, a refill add is injected so the book never stays one-sided and the spread stays near one
tick.

Occasionally the session enters a short trading **HALT**: a ``HALTED`` status event is emitted,
no order activity occurs for a sampled duration, then a ``TRADING`` status event resumes flow.

Record / event conventions (contract section 2.1)
--------------------------------------------------
* The session opens with a single event of ``SNAPSHOT``-flagged ``ADD`` records (priority
  order) closed by ``RecordFlag.LAST``.
* Every event is a run of records terminated by exactly one ``LAST``-flagged record.
* An aggressive execution is emitted, within one event, as: for each price level touched one
  ``TRADE`` (aggressor side, ``order_id`` 0, total qty at that level), then for each resting
  order filled a ``FILL`` (resting side, order id, qty) immediately followed by the ``CANCEL``
  reducing that resting order by the same qty. ``TRADE``/``FILL`` never mutate the internal
  book; only the ``CANCEL`` does.
* ``event_time_ns`` is strictly nondecreasing across events (constant within an event);
  ``capture_time_ns = event_time_ns + jitter`` with positive jitter, and capture times are
  nondecreasing. ``source_ordinal`` is the running record index. Order ids are unique.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

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

# Fixed epoch for study sessions: 2026-01-05T14:30:00Z in integer nanoseconds.
STUDY_EPOCH_NS: int = 1_767_623_400_000_000_000
_ONE_DAY_NS: int = 86_400 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class SyntheticParams:
    """Parameters for a single synthetic session.

    Every field is documented; defaults are calibrated (see module tests) so that a one-lot
    buy joined at the best bid fills within one second a meaningful fraction of the time, with
    both queue-depletion and trade-through fills occurring, at an event rate around
    50-300 events/second.

    Attributes
    ----------
    seed:
        Master seed for the deterministic :class:`numpy.random.Generator`. Identical seeds
        reproduce identical records; different seeds diverge.
    session_id:
        Session identifier stored in metadata (e.g. ``"SYN-0001"``).
    start_ns:
        Session start in integer nanoseconds since the Unix epoch. The first event time.
    duration_s:
        Session wall length in seconds. Generation stops once ``event_time`` passes this.
    tick_size_fixed:
        Tick size in fixed-point (1e-9) units. Default ``250_000_000`` == 0.25 (contract 4).
    multiplier:
        Contract multiplier (currency per quoted unit per contract). Default ``50``.
    initial_mid_ticks:
        Initial mid price expressed in whole ticks. The opening book is centred here.
    levels:
        Number of price levels seeded on each side of the opening snapshot.
    mean_level_qty:
        Mean resting quantity used for geometric size draws of adds and snapshot levels.
    depth_decay:
        Geometric decay in [0, 1) for how far from the touch new limit adds land; larger
        values place more adds deeper in the book.
    event_rate_hz:
        Target mean number of economic events per second; sets the exponential inter-arrival
        mean (``1 / event_rate_hz`` seconds).
    add_rate / modify_rate / market_rate:
        Unnormalised relative rates for add, modify, and market actions. ``cancel`` is handled
        separately by a per-order hazard (see ``cancel_hazard_per_order``) rather than a fixed
        rate, so cancellation pressure grows with the book and keeps resting depth stationary.
        The per-step action is drawn proportional to the four effective rates (restricted to
        currently feasible actions).
    cancel_hazard_per_order:
        Per-resting-order cancellation hazard. The effective cancel rate each step is
        ``cancel_hazard_per_order * n_resting_orders`` (a linear birth-death death rate), so
        total cancel intensity scales with the number of live orders. This makes the expected
        resting depth converge to a finite stationary level ``N* ~ add_arrival_rate /
        cancel_hazard_per_order`` instead of growing without bound. It is the primary knob for
        the stationary book size.
    full_cancel_prob:
        Probability a cancel removes the entire resting order (else a partial reduction).
    modify_priority_retain_prob:
        Probability a modify is a pure size decrease (priority retained); otherwise a price
        move or size increase (priority lost).
    market_mean_qty:
        Mean aggressive order size (geometric), controlling sweep depth / trade-throughs.
    drift_persistence:
        AR(1) persistence in [0, 1) of the latent drift state driving directional pressure.
    drift_vol:
        Standard deviation of the latent drift innovation each step.
    drift_gain:
        Logistic weight on the latent drift state in the market-order buy probability.
    imbalance_gain:
        Logistic weight on top-of-book imbalance in the market-order buy probability, and the
        weight by which imbalance tilts the fair-value increment each step. Positive values
        make a bid-heavy book predict upward mid moves (the learnable signal the research
        pipeline needs).
    refill_qty_threshold:
        Target minimum resting quantity maintained at each inside (touch) level. Keeping the
        touch reasonably deep makes a back-of-queue order non-trivial to deplete within one
        second, which is what places the fill rate in the intended band.
    jitter_mean_ns:
        Mean of the per-record capture jitter (exponential); capture_time = event_time + draw,
        clamped to stay nondecreasing. Always strictly positive.
    halt_rate_hz:
        Mean rate of trading halts (Poisson). Set to 0 to disable halts.
    halt_min_s / halt_max_s:
        Inclusive bounds (seconds) on sampled halt durations.
    warmup_guard_ticks:
        Minimum number of ticks kept between best bid and ask targets to avoid locked books
        during refills.
    max_touch_qty:
        Soft cap on resting quantity at the touch. Adds that would land on an already-heavy
        touch are pushed one tick deeper, bounding the queue so that a back-of-queue order
        remains depletable within the one-second horizon.
    sweep_prob:
        Probability a market order is sized as a large sweep that clears the touch and prices
        through at least one additional level (guarantees trade-through fills occur).
    fair_reversion:
        Mild mean-reversion coefficient pulling the latent fair value back toward
        ``initial_mid_ticks`` so the price does not wander off over a long session.
    fair_step_scale:
        Scales the per-step fair-value increment. Smaller values move the mid less often
        (fewer tick steps per event), which lowers the fraction of back-of-queue orders filled
        within one second; it is the primary knob for calibrating the fill rate.
    """

    seed: int
    session_id: str
    start_ns: int
    duration_s: int
    tick_size_fixed: int = 250_000_000
    multiplier: int = 50
    initial_mid_ticks: int = 20_000
    levels: int = 10
    mean_level_qty: float = 12.0
    depth_decay: float = 0.55
    event_rate_hz: float = 150.0
    add_rate: float = 1.0
    cancel_hazard_per_order: float = 0.012
    modify_rate: float = 0.18
    market_rate: float = 0.12
    full_cancel_prob: float = 0.35
    modify_priority_retain_prob: float = 0.5
    market_mean_qty: float = 6.0
    drift_persistence: float = 0.95
    drift_vol: float = 0.25
    drift_gain: float = 0.4
    imbalance_gain: float = 1.0
    refill_qty_threshold: int = 30
    max_touch_qty: int = 70
    sweep_prob: float = 0.004
    fair_reversion: float = 0.02
    fair_step_scale: float = 0.004
    jitter_mean_ns: int = 20_000
    halt_rate_hz: float = 0.02
    halt_min_s: float = 0.5
    halt_max_s: float = 2.0
    warmup_guard_ticks: int = 1

    def instrument(self) -> InstrumentDefinition:
        """Build the instrument definition (contract section 4: SYNTH.MBO / SYNZ6)."""
        return InstrumentDefinition(
            instrument_id=1,
            symbol="SYNZ6",
            tick_size_fixed=PriceFixed(self.tick_size_fixed),
            multiplier=self.multiplier,
            effective_time_ns=TimeNs(self.start_ns),
        )


# ---------------------------------------------------------------------------
# Internal book (independent of qexec.reference)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Order:
    order_id: int
    side: Side
    tick: int
    qty: int


class _Book:
    """A minimal FIFO limit-order book keyed by integer tick index.

    Each ``(side, tick)`` maps to a ``deque`` of :class:`_Order` in priority (arrival) order.
    The book tracks every live order by id for O(1) lookup during cancels and modifies.
    """

    def __init__(self) -> None:
        # Each level maps tick -> {order_id: _Order}; Python dicts preserve insertion (FIFO)
        # order, giving O(1) add/remove while keeping priority order on iteration.
        self.bids: dict[int, dict[int, _Order]] = {}
        self.asks: dict[int, dict[int, _Order]] = {}
        self.by_id: dict[int, _Order] = {}
        # Cached per-level totals so level_qty / top_qty are O(1).
        self._bid_level_qty: dict[int, int] = {}
        self._ask_level_qty: dict[int, int] = {}
        # Id list with position index for O(1) uniform random selection (swap-remove).
        self._id_list: list[int] = []
        self._id_pos: dict[int, int] = {}

    def _levels(self, side: Side) -> dict[int, dict[int, _Order]]:
        return self.bids if side is Side.BID else self.asks

    def _level_qty_map(self, side: Side) -> dict[int, int]:
        return self._bid_level_qty if side is Side.BID else self._ask_level_qty

    def add(self, order: _Order) -> None:
        level = self._levels(order.side).setdefault(order.tick, {})
        level[order.order_id] = order
        self.by_id[order.order_id] = order
        lq = self._level_qty_map(order.side)
        lq[order.tick] = lq.get(order.tick, 0) + order.qty
        self._id_pos[order.order_id] = len(self._id_list)
        self._id_list.append(order.order_id)

    def reduce(self, order_id: int, qty: int) -> None:
        """Reduce a resting order by ``qty``; remove it entirely when it hits zero."""
        order = self.by_id[order_id]
        if qty >= order.qty:
            # Full removal: _remove decrements the cached level total by the whole order.
            self._remove(order)
        else:
            order.qty -= qty
            self._level_qty_map(order.side)[order.tick] -= qty

    def _remove(self, order: _Order) -> None:
        level = self._levels(order.side)[order.tick]
        del level[order.order_id]
        lq = self._level_qty_map(order.side)
        lq[order.tick] -= order.qty
        if not level:
            del self._levels(order.side)[order.tick]
            lq.pop(order.tick, None)
        del self.by_id[order.order_id]
        # Swap-remove from the id list.
        pos = self._id_pos.pop(order.order_id)
        last = self._id_list.pop()
        if last != order.order_id:
            self._id_list[pos] = last
            self._id_pos[last] = pos

    def move(self, order: _Order, new_tick: int, new_qty: int) -> None:
        """Priority-losing modify: remove and re-add at the back of the new level."""
        self._remove(order)
        order.tick = new_tick
        order.qty = new_qty
        self.add(order)

    def best_bid_tick(self) -> int | None:
        return max(self.bids) if self.bids else None

    def best_ask_tick(self) -> int | None:
        return min(self.asks) if self.asks else None

    def level_qty(self, side: Side, tick: int) -> int:
        return self._level_qty_map(side).get(tick, 0)

    def top_qty(self, side: Side) -> int:
        tick = self.best_bid_tick() if side is Side.BID else self.best_ask_tick()
        return self.level_qty(side, tick) if tick is not None else 0

    def resting_orders(self, side: Side, tick: int) -> list[_Order]:
        level = self._levels(side).get(tick)
        return list(level.values()) if level else []


# ---------------------------------------------------------------------------
# Emission state
# ---------------------------------------------------------------------------


class _Emitter:
    r"""Accumulates :class:`CanonicalRecord`\ s with correct ordinals, times, and flags."""

    def __init__(self, params: SyntheticParams, rng: np.random.Generator) -> None:
        self._p = params
        self._rng = rng
        self.records: list[CanonicalRecord] = []
        self._ordinal = 0
        self._last_capture = 0
        self._pending: list[tuple[Action, Side, int, int, int, RecordFlag]] = []

    def stage(
        self,
        action: Action,
        side: Side,
        tick: int,
        qty: int,
        order_id: int,
        *,
        flags: RecordFlag = RecordFlag.NONE,
    ) -> None:
        """Queue a record for the current event (flushed by :meth:`flush_event`)."""
        self._pending.append((action, side, tick, qty, order_id, flags))

    def flush_event(self, event_time_ns: int) -> None:
        """Emit all staged records as one event, closing with a single ``LAST`` flag."""
        if not self._pending:
            return
        n = len(self._pending)
        for i, (action, side, tick, qty, order_id, base_flags) in enumerate(self._pending):
            flags = base_flags
            if i == n - 1:
                flags |= RecordFlag.LAST
            jitter = int(self._rng.exponential(self._p.jitter_mean_ns)) + 1
            capture = event_time_ns + jitter
            if capture <= self._last_capture:
                capture = self._last_capture + 1
            self._last_capture = capture
            self.records.append(
                CanonicalRecord(
                    dataset_id="SYNTH.MBO",
                    publisher_id=1,
                    instrument_id=1,
                    session_epoch=0,
                    source_ordinal=self._ordinal,
                    action=action,
                    side=side,
                    price_fixed=tick * self._p.tick_size_fixed,
                    quantity=qty,
                    order_id=order_id,
                    event_time_ns=TimeNs(event_time_ns),
                    capture_time_ns=TimeNs(capture),
                    flags=flags,
                    source_file_hash="",
                    source_record_offset=self._ordinal,
                )
            )
            self._ordinal += 1
        self._pending.clear()


def _sigmoid(x: float) -> float:
    # Numerically stable logistic.
    if x >= 0:
        z = float(np.exp(-x))
        return 1.0 / (1.0 + z)
    z = float(np.exp(x))
    return z / (1.0 + z)


def _geom_qty(rng: np.random.Generator, mean: float) -> int:
    """A geometric positive integer quantity with the given mean (>= 1)."""
    p = 1.0 / max(mean, 1.0)
    return int(rng.geometric(p))


class _Generator:
    """Stateful driver that runs the discrete-event loop and produces records."""

    def __init__(self, params: SyntheticParams) -> None:
        self._p = params
        self._rng = np.random.default_rng(params.seed)
        self._book = _Book()
        self._emit = _Emitter(params, self._rng)
        self._status_events: list[StatusEvent] = []
        self._next_order_id = 1
        self._drift = 0.0
        self._fair = float(params.initial_mid_ticks)
        # Trade-through / depletion telemetry (used by calibration, not persisted).
        self.n_trade_through = 0
        self.n_single_level_trades = 0

    # -- helpers ---------------------------------------------------------
    def _new_order_id(self) -> int:
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    def _imbalance(self) -> float:
        bq = self._book.top_qty(Side.BID)
        aq = self._book.top_qty(Side.ASK)
        total = bq + aq
        if total == 0:
            return 0.0
        return (bq - aq) / total

    # -- opening snapshot -------------------------------------------------
    def _emit_snapshot(self) -> None:
        p = self._p
        tb = p.initial_mid_ticks  # best bid tick == floor(fair)
        ta = tb + 1  # best ask one tick above: opening spread is one tick
        for depth in range(p.levels):
            bid_tick = tb - depth
            qty = _geom_qty(self._rng, p.mean_level_qty)
            oid = self._new_order_id()
            self._book.add(_Order(oid, Side.BID, bid_tick, qty))
            self._emit.stage(Action.ADD, Side.BID, bid_tick, qty, oid, flags=RecordFlag.SNAPSHOT)
        for depth in range(p.levels):
            ask_tick = ta + depth
            qty = _geom_qty(self._rng, p.mean_level_qty)
            oid = self._new_order_id()
            self._book.add(_Order(oid, Side.ASK, ask_tick, qty))
            self._emit.stage(Action.ADD, Side.ASK, ask_tick, qty, oid, flags=RecordFlag.SNAPSHOT)
        self._emit.flush_event(p.start_ns)

    # -- economic actions -------------------------------------------------
    def _do_add(self, event_time: int) -> None:
        p = self._p
        rng = self._rng
        side = Side.BID if rng.random() < 0.5 else Side.ASK
        bb, ba = self._book.best_bid_tick(), self._book.best_ask_tick()
        depth = int(rng.geometric(1.0 - p.depth_decay)) - 1
        if side is Side.BID:
            ref = bb if bb is not None else p.initial_mid_ticks - 1
            tick = ref - depth
            if ba is not None and tick >= ba:  # never cross
                tick = ba - 1
        else:
            ref = ba if ba is not None else p.initial_mid_ticks + 1
            tick = ref + depth
            if bb is not None and tick <= bb:
                tick = bb + 1
        # Bound touch accumulation: if this add would land on an already-heavy touch, push it
        # one tick deeper so the touch queue stays depletable (keeps fills achievable).
        touch = bb if side is Side.BID else ba
        if (
            touch is not None
            and tick == touch
            and self._book.level_qty(side, tick) >= p.max_touch_qty
        ):
            tick = tick - 1 if side is Side.BID else tick + 1
        qty = _geom_qty(rng, p.mean_level_qty)
        oid = self._new_order_id()
        self._book.add(_Order(oid, side, tick, qty))
        self._emit.stage(Action.ADD, side, tick, qty, oid)
        self._emit.flush_event(event_time)

    def _random_order_id(self) -> int:
        idx = int(self._rng.integers(0, len(self._book._id_list)))
        return self._book._id_list[idx]

    def _do_cancel(self, event_time: int) -> bool:
        if not self._book.by_id:
            return False
        rng = self._rng
        oid = self._random_order_id()
        order = self._book.by_id[oid]
        if rng.random() < self._p.full_cancel_prob or order.qty <= 1:
            qty = order.qty
        else:
            qty = int(rng.integers(1, order.qty))
        self._book.reduce(oid, qty)
        self._emit.stage(Action.CANCEL, order.side, order.tick, qty, oid)
        self._emit.flush_event(event_time)
        return True

    def _do_modify(self, event_time: int) -> bool:
        if not self._book.by_id:
            return False
        rng = self._rng
        oid = self._random_order_id()
        order = self._book.by_id[oid]
        if rng.random() < self._p.modify_priority_retain_prob and order.qty > 1:
            # Priority-retaining pure size decrease.
            new_qty = int(rng.integers(1, order.qty))
            order.qty = new_qty
            self._emit.stage(Action.MODIFY, order.side, order.tick, new_qty, oid)
        else:
            # Priority-losing: price move (one tick toward interior) or size increase.
            bb, ba = self._book.best_bid_tick(), self._book.best_ask_tick()
            if rng.random() < 0.5:
                new_qty = order.qty + _geom_qty(rng, self._p.mean_level_qty)
                new_tick = order.tick
            else:
                new_qty = order.qty
                if order.side is Side.BID:
                    new_tick = order.tick - 1
                    if ba is not None and new_tick >= ba:
                        new_tick = order.tick
                else:
                    new_tick = order.tick + 1
                    if bb is not None and new_tick <= bb:
                        new_tick = order.tick
                if new_tick == order.tick:
                    new_qty = order.qty + _geom_qty(rng, self._p.mean_level_qty)
            self._book.move(order, new_tick, new_qty)
            self._emit.stage(Action.MODIFY, order.side, new_tick, new_qty, oid)
        self._emit.flush_event(event_time)
        return True

    def _do_market(self, event_time: int) -> bool:
        """Aggressive order: TRADE per level, then FILL+CANCEL per resting order, one event."""
        rng = self._rng
        buy_prob = _sigmoid(
            self._p.drift_gain * self._drift + self._p.imbalance_gain * self._imbalance()
        )
        is_buy = rng.random() < buy_prob
        aggressor = Side.BID if is_buy else Side.ASK
        resting_side = Side.ASK if is_buy else Side.BID

        if rng.random() < self._p.sweep_prob:
            # Occasional large sweep: size to clear the touch and price through >= 1 level.
            touch_tick = self._book.best_ask_tick() if is_buy else self._book.best_bid_tick()
            touch_qty = (
                self._book.level_qty(resting_side, touch_tick) if touch_tick is not None else 0
            )
            remaining = touch_qty + _geom_qty(rng, self._p.market_mean_qty)
        else:
            remaining = _geom_qty(rng, self._p.market_mean_qty)
        levels_hit = 0
        while remaining > 0:
            tick = self._book.best_ask_tick() if is_buy else self._book.best_bid_tick()
            if tick is None:
                break
            resting = self._book.resting_orders(resting_side, tick)
            if not resting:
                break
            # Compute the per-order fills first so the TRADE quantity is exactly the sum of its
            # FILLs (robust by construction), then emit TRADE, then the FILL+CANCEL pairs.
            fills: list[tuple[int, int]] = []  # (order_id, fill_qty)
            left = remaining
            for order in resting:
                if left <= 0:
                    break
                fq = min(order.qty, left)
                fills.append((order.order_id, fq))
                left -= fq
            take_level = sum(fq for _, fq in fills)
            if take_level <= 0:
                break
            # One TRADE for this level: aggressor side, order_id 0, total qty taken.
            self._emit.stage(Action.TRADE, aggressor, tick, take_level, 0)
            for oid, fq in fills:
                # FILL (resting side, order id) then the CANCEL that reduces it.
                self._emit.stage(Action.FILL, resting_side, tick, fq, oid)
                self._emit.stage(Action.CANCEL, resting_side, tick, fq, oid)
                self._book.reduce(oid, fq)
            remaining -= take_level
            levels_hit += 1

        if levels_hit == 0:
            return False
        if levels_hit > 1:
            self.n_trade_through += 1
        else:
            self.n_single_level_trades += 1
        self._emit.flush_event(event_time)
        return True

    def _maintain_quotes(self, event_time: int) -> None:
        """Keep a two-sided book with a ~1-tick spread centred on the drifting fair value.

        The target inside is ``tb = floor(fair)`` on the bid and ``ta = tb + 1`` on the ask.
        As ``fair`` drifts the targets move, so new inside orders establish a new best price
        and the mid tracks ``fair``; because ``fair`` is pushed by imbalance, imbalance
        predicts the next mid move. Levels are also replenished whenever the touch is thin, so
        the book never stays one-sided and the spread stays near one tick.
        """
        p = self._p
        rng = self._rng
        tb = int(np.floor(self._fair))
        ta = tb + 1

        # Pull any resting bids at/above the target ask and asks at/below the target bid so the
        # new inside never crosses (these are stale quotes the moving market would clear).
        for stale_tick in [t for t in list(self._book.bids) if t >= ta]:
            for order in self._book.resting_orders(Side.BID, stale_tick):
                self._emit.stage(Action.CANCEL, Side.BID, stale_tick, order.qty, order.order_id)
                self._book.reduce(order.order_id, order.qty)
        for stale_tick in [t for t in list(self._book.asks) if t <= tb]:
            for order in self._book.resting_orders(Side.ASK, stale_tick):
                self._emit.stage(Action.CANCEL, Side.ASK, stale_tick, order.qty, order.order_id)
                self._book.reduce(order.order_id, order.qty)

        # Ensure the bid inside at tb and the ask inside at ta are populated.
        if self._book.level_qty(Side.BID, tb) <= p.refill_qty_threshold:
            qty = _geom_qty(rng, p.mean_level_qty)
            oid = self._new_order_id()
            self._book.add(_Order(oid, Side.BID, tb, qty))
            self._emit.stage(Action.ADD, Side.BID, tb, qty, oid)
        if self._book.level_qty(Side.ASK, ta) <= p.refill_qty_threshold:
            qty = _geom_qty(rng, p.mean_level_qty)
            oid = self._new_order_id()
            self._book.add(_Order(oid, Side.ASK, ta, qty))
            self._emit.stage(Action.ADD, Side.ASK, ta, qty, oid)
        self._emit.flush_event(event_time)

    # -- latent state -----------------------------------------------------
    def _advance_drift(self) -> None:
        # Persistent AR(1) drift state, mean-reverting to zero.
        self._drift = self._p.drift_persistence * self._drift + float(
            self._rng.normal(0.0, self._p.drift_vol)
        )
        # Fair value is a slow random walk whose per-step increment is tilted by the latent
        # drift and the current top-of-book imbalance, with mild reversion to the opening
        # level. The best bid/ask track floor(fair)/floor(fair)+1 (see _maintain_quotes), so
        # the mid follows fair and imbalance (which pushes fair) predicts the next mid move.
        step = (
            self._p.drift_gain * self._drift
            + self._p.imbalance_gain * self._imbalance()
            - self._p.fair_reversion * (self._fair - self._p.initial_mid_ticks)
        ) * self._p.fair_step_scale
        self._fair += step

    # -- main loop --------------------------------------------------------
    def run(self) -> tuple[list[CanonicalRecord], list[StatusEvent]]:
        p = self._p
        # Explicit opening status: consumers never treat UNKNOWN as continuous trading.
        self._status_events.append(
            StatusEvent(1, TradingStatus.TRADING, TimeNs(p.start_ns), TimeNs(p.start_ns))
        )
        self._emit_snapshot()
        end_ns = p.start_ns + p.duration_s * 1_000_000_000
        mean_gap_ns = 1_000_000_000.0 / p.event_rate_hz
        t = float(p.start_ns)
        next_halt = self._sample_next_halt(t) if p.halt_rate_hz > 0 else None

        last_event_time = p.start_ns
        while True:
            gap = float(self._rng.exponential(mean_gap_ns))
            t += gap
            event_time = int(t)
            if event_time <= last_event_time:
                event_time = last_event_time + 1
            if event_time >= end_ns:
                break

            if next_halt is not None and event_time >= next_halt:
                event_time = self._run_halt(event_time)
                last_event_time = event_time
                t = float(event_time)
                next_halt = self._sample_next_halt(t)
                continue

            self._advance_drift()
            self._step(event_time)
            self._maintain_quotes(event_time)
            last_event_time = event_time

        return self._emit.records, self._status_events

    def _step(self, event_time: int) -> None:
        p = self._p
        rng = self._rng
        # Cancel is a per-order hazard: its effective rate scales with the live order count, so
        # total cancel intensity grows with the book and the expected resting depth converges to
        # a finite stationary level (a linear birth-death death term) instead of accumulating
        # without bound. Add/modify/market keep their fixed unnormalised rates.
        n_orders = len(self._book.by_id)
        cancel_rate = p.cancel_hazard_per_order * n_orders
        rates = np.array([p.add_rate, cancel_rate, p.modify_rate, p.market_rate], dtype=float)
        total = rates.sum()
        if total <= 0.0:
            self._do_add(event_time)
            return
        rates = rates / total
        for _ in range(4):
            choice = int(rng.choice(4, p=rates))
            if choice == 0:
                self._do_add(event_time)
                return
            if choice == 1:
                if self._do_cancel(event_time):
                    return
            elif choice == 2:
                if self._do_modify(event_time):
                    return
            elif self._do_market(event_time):
                return
        # Fallback: an add is always possible.
        self._do_add(event_time)

    # -- halts ------------------------------------------------------------
    def _sample_next_halt(self, now: float) -> float:
        mean_gap = 1_000_000_000.0 / self._p.halt_rate_hz
        return now + float(self._rng.exponential(mean_gap))

    def _run_halt(self, event_time: int) -> int:
        p = self._p
        jitter = int(self._rng.exponential(p.jitter_mean_ns)) + 1
        self._status_events.append(
            StatusEvent(1, TradingStatus.HALTED, TimeNs(event_time), TimeNs(event_time + jitter))
        )
        dur_s = float(self._rng.uniform(p.halt_min_s, p.halt_max_s))
        resume = event_time + int(dur_s * 1_000_000_000)
        jitter2 = int(self._rng.exponential(p.jitter_mean_ns)) + 1
        self._status_events.append(
            StatusEvent(1, TradingStatus.TRADING, TimeNs(resume), TimeNs(resume + jitter2))
        )
        return resume


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_session(
    params: SyntheticParams,
) -> tuple[SessionMeta, InstrumentDefinition, list[CanonicalRecord], list[StatusEvent]]:
    """Generate one deterministic synthetic session.

    Returns the session metadata, instrument definition, the ordered record list, and the
    status-event list. Identical ``params`` (same ``seed``) reproduce identical output.
    """
    gen = _Generator(params)
    records, status = gen.run()
    end_ns = int(records[-1].event_time_ns) if records else params.start_ns
    meta = SessionMeta(
        session_id=params.session_id,
        dataset_id="SYNTH.MBO",
        start_ns=params.start_ns,
        end_ns=end_ns,
        params=_params_to_dict(params),
    )
    return meta, params.instrument(), records, status


def _params_to_dict(params: SyntheticParams) -> dict[str, Any]:
    return asdict(params)


def write_synthetic_session(params: SyntheticParams, out_dir: Path) -> Path:
    """Generate a session and write it to ``out_dir / session_id`` as an immutable directory."""
    meta, instrument, records, status = generate_session(params)
    directory = out_dir / params.session_id
    write_session(directory, meta, instrument, records, status)
    return directory


def generate_study(
    out_root: Path,
    n_sessions: int,
    base_seed: int,
    duration_s: int,
) -> list[Path]:
    """Generate ``n_sessions`` sessions on consecutive synthetic days.

    Session ids are ``"SYN-0001"``, ``"SYN-0002"``, ... Start times are spaced one day apart
    from :data:`STUDY_EPOCH_NS` (2026-01-05T14:30:00Z). Seeds are ``base_seed + index``.
    """
    paths: list[Path] = []
    for i in range(n_sessions):
        params = SyntheticParams(
            seed=base_seed + i,
            session_id=f"SYN-{i + 1:04d}",
            start_ns=STUDY_EPOCH_NS + i * _ONE_DAY_NS,
            duration_s=duration_s,
        )
        paths.append(write_synthetic_session(params, out_root))
    return paths
