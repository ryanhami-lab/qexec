"""Python reference order book (architecture section 5).

The reference book maintains an order map ``OrderKey -> RestingOrder`` and, per side, a
set of priority-ordered price levels. Price levels are kept in a dict keyed by
``price_fixed``; the best price on each side is found with ``bisect`` over a sorted list
of the active prices on that side, so no third-party ordered container is needed.

Within a price level, resting orders are stored in a plain ``dict`` keyed by ``OrderKey``;
Python dict insertion order *is* the FIFO priority order. A monotone ``priority_seq``
counter is also stored on every :class:`RestingOrder` so that priority is explicit and
survives copies: a lower ``priority_seq`` is ahead. Insertion appends to the back of the
level, and ``priority_seq`` is strictly increasing over the life of the book.

Source semantics (architecture 4.2, engineering contract 2.1):

* ``ADD`` inserts a new resting order at the back of its level.
* ``CANCEL`` removes ``quantity`` from an existing order (partial retains priority;
  removal to zero deletes the order).
* ``MODIFY`` sets a new absolute price and quantity. A price change or a size *increase*
  loses priority (moves to the back of the new level). A pure size *decrease* retains
  priority. ``MODIFY`` of an unknown order is an anomaly unless ``modify_unknown_as_add``.
* ``CLEAR`` removes every resting order.
* Each normalized ``INITIALIZATION`` batch is one complete replacement snapshot.
* ``TRADE``/``FILL``/``NONE`` never mutate the book.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field, replace

from qexec.adapters.diagnostics import Anomaly
from qexec.core.types import (
    Action,
    BatchKind,
    BookLevel,
    BookSnapshot,
    CanonicalRecord,
    EventBatch,
    InstrumentDefinition,
    OrderKey,
    Side,
    TradingStatus,
)


@dataclass(frozen=True, slots=True)
class RestingOrder:
    """One resting order. ``priority_seq`` is a monotone sequence; lower is ahead."""

    key: OrderKey
    side: Side
    price_fixed: int
    quantity: int
    priority_seq: int


@dataclass(slots=True)
class _Level:
    """A single price level: FIFO-ordered orders keyed by :class:`OrderKey`.

    Dict insertion order is the priority order. ``total_quantity`` is cached so snapshots
    and invariant checks do not have to re-sum on every call.
    """

    price_fixed: int
    orders: dict[OrderKey, RestingOrder] = field(default_factory=dict)
    total_quantity: int = 0

    def add(self, order: RestingOrder) -> None:
        self.orders[order.key] = order
        self.total_quantity += order.quantity

    def remove(self, key: OrderKey) -> RestingOrder:
        order = self.orders.pop(key)
        self.total_quantity -= order.quantity
        return order

    def is_empty(self) -> bool:
        return not self.orders


class ReferenceBook:
    """Mutable reference book over a single instrument.

    Bids are ordered best (highest price) first; asks best (lowest price) first. The
    per-side ``_bid_prices`` / ``_ask_prices`` lists stay sorted ascending so ``bisect``
    can insert/locate prices; best-of-side is the list end (bids) or start (asks).
    """

    def __init__(
        self,
        instrument: InstrumentDefinition,
        *,
        modify_unknown_as_add: bool = False,
        commit_validation: str = "boundary",
    ) -> None:
        """Create an empty book.

        ``commit_validation`` selects the invariant check run by :meth:`commit_batch`:

        * ``"boundary"`` (default): crossing check plus structural checks on every price
          level touched by the batch. Cost is proportional to the batch, not the book, so a
          full-session replay stays linear.
        * ``"full"``: :meth:`validate_invariants` over the whole book on every commit
          (audit mode; quadratic over a session).
        """
        if commit_validation not in ("boundary", "full"):
            raise ValueError("commit_validation must be 'boundary' or 'full'")
        self._commit_validation = commit_validation
        self._instrument = instrument
        self._modify_unknown_as_add = modify_unknown_as_add
        self._orders: dict[OrderKey, RestingOrder] = {}
        self._bid_levels: dict[int, _Level] = {}
        self._ask_levels: dict[int, _Level] = {}
        self._bid_prices: list[int] = []  # sorted ascending; best bid is the last element
        self._ask_prices: list[int] = []  # sorted ascending; best ask is the first element
        self._priority_counter: int = 0
        self._status: TradingStatus = TradingStatus.UNKNOWN
        self._anomalies: list[Anomaly] = []
        self._current_batch_id: int = -1
        self._touched: set[tuple[Side, int]] = set()
        self._touched_clear: bool = False

    # -- side helpers ----------------------------------------------------------------

    def _levels_for(self, side: Side) -> tuple[dict[int, _Level], list[int]]:
        if side is Side.BID:
            return self._bid_levels, self._bid_prices
        if side is Side.ASK:
            return self._ask_levels, self._ask_prices
        raise ValueError(f"book side must be BID or ASK, got {side!r}")

    def _get_or_create_level(self, side: Side, price_fixed: int) -> _Level:
        levels, prices = self._levels_for(side)
        level = levels.get(price_fixed)
        if level is None:
            level = _Level(price_fixed)
            levels[price_fixed] = level
            bisect.insort(prices, price_fixed)
        return level

    def _drop_level_if_empty(self, side: Side, price_fixed: int) -> None:
        levels, prices = self._levels_for(side)
        level = levels.get(price_fixed)
        if level is not None and level.is_empty():
            del levels[price_fixed]
            idx = bisect.bisect_left(prices, price_fixed)
            if idx < len(prices) and prices[idx] == price_fixed:
                prices.pop(idx)

    def _next_priority(self) -> int:
        self._priority_counter += 1
        return self._priority_counter

    def _record_anomaly(self, kind: str, record: CanonicalRecord, detail: str) -> None:
        self._anomalies.append(
            Anomaly(
                kind=kind,
                batch_id=self._current_batch_id,
                source_ordinal=record.source_ordinal,
                detail=detail,
            )
        )

    # -- mutation --------------------------------------------------------------------

    def apply_record(self, record: CanonicalRecord) -> None:
        """Apply one record. Only ADD/MODIFY/CANCEL/CLEAR mutate; others are no-ops."""
        action = record.action
        if action.mutates_book:
            # Track every level a mutation can touch (including a MODIFY's old level) for
            # boundary-mode validation at commit.
            if action is Action.CLEAR:
                self._touched_clear = True
            else:
                existing = self._orders.get(record.order_key())
                if existing is not None:
                    self._touched.add((existing.side, existing.price_fixed))
                if record.side in (Side.BID, Side.ASK):
                    self._touched.add((record.side, record.price_fixed))
        if action is Action.ADD:
            self._apply_add(record)
        elif action is Action.CANCEL:
            self._apply_cancel(record)
        elif action is Action.MODIFY:
            self._apply_modify(record)
        elif action is Action.CLEAR:
            self._apply_clear()
        # TRADE/FILL/NONE never mutate the book (contract 2.1).

    def _apply_add(self, record: CanonicalRecord) -> None:
        key = record.order_key()
        if record.side not in (Side.BID, Side.ASK):
            self._record_anomaly("ADD_INVALID_SIDE", record, f"side={record.side!r}")
            return
        if record.quantity <= 0:
            self._record_anomaly("ADD_NONPOSITIVE_QTY", record, f"quantity={record.quantity}")
            return
        if key in self._orders:
            self._record_anomaly("DUPLICATE_ADD", record, f"order_id={record.order_id}")
            return
        order = RestingOrder(
            key=key,
            side=record.side,
            price_fixed=record.price_fixed,
            quantity=record.quantity,
            priority_seq=self._next_priority(),
        )
        self._orders[key] = order
        self._get_or_create_level(record.side, record.price_fixed).add(order)

    def _apply_cancel(self, record: CanonicalRecord) -> None:
        key = record.order_key()
        existing = self._orders.get(key)
        if existing is None:
            self._record_anomaly("CANCEL_UNKNOWN", record, f"order_id={record.order_id}")
            return
        remove_qty = record.quantity
        if remove_qty <= 0:
            self._record_anomaly("CANCEL_NONPOSITIVE_QTY", record, f"quantity={remove_qty}")
            return
        if remove_qty > existing.quantity:
            self._record_anomaly(
                "CANCEL_OVER_QTY",
                record,
                f"cancel {remove_qty} > resting {existing.quantity}; removing order",
            )
            remove_qty = existing.quantity
        remaining = existing.quantity - remove_qty
        level = self._levels_for(existing.side)[0][existing.price_fixed]
        if remaining == 0:
            # Full removal.
            del self._orders[key]
            level.remove(key)
            self._drop_level_if_empty(existing.side, existing.price_fixed)
        else:
            # Partial cancel retains priority (same priority_seq, same dict position).
            updated = replace(existing, quantity=remaining)
            self._orders[key] = updated
            level.orders[key] = updated
            level.total_quantity -= remove_qty

    def _apply_modify(self, record: CanonicalRecord) -> None:
        key = record.order_key()
        existing = self._orders.get(key)
        if record.quantity <= 0:
            self._record_anomaly("MODIFY_NONPOSITIVE_QTY", record, f"quantity={record.quantity}")
            return
        if existing is None:
            if self._modify_unknown_as_add:
                self._apply_add(record)
            else:
                self._record_anomaly(
                    "MODIFY_UNKNOWN",
                    record,
                    f"order_id={record.order_id} (not treated as add)",
                )
            return

        new_price = record.price_fixed
        new_qty = record.quantity
        price_changed = new_price != existing.price_fixed
        size_increased = new_qty > existing.quantity
        loses_priority = price_changed or size_increased

        if not loses_priority:
            # Pure size decrease -> retains priority_seq and FIFO (dict) position. Mutate
            # in place without removing from the level so the dict ordering is unchanged.
            level = self._levels_for(existing.side)[0][existing.price_fixed]
            updated = replace(existing, quantity=new_qty)
            self._orders[key] = updated
            level.orders[key] = updated
            level.total_quantity += new_qty - existing.quantity
            return

        # Price change or size increase -> back of the (new) level, fresh priority.
        old_level = self._levels_for(existing.side)[0][existing.price_fixed]
        old_level.remove(key)
        self._drop_level_if_empty(existing.side, existing.price_fixed)
        updated = RestingOrder(
            key=key,
            side=existing.side,
            price_fixed=new_price,
            quantity=new_qty,
            priority_seq=self._next_priority(),
        )
        self._orders[key] = updated
        self._get_or_create_level(existing.side, new_price).add(updated)

    def _apply_clear(self) -> None:
        self._orders.clear()
        self._bid_levels.clear()
        self._ask_levels.clear()
        self._bid_prices.clear()
        self._ask_prices.clear()

    # -- batch application -----------------------------------------------------------

    def begin_batch(self, batch: EventBatch) -> None:
        """Begin one atomic batch, replacing resting state for a complete snapshot.

        The frozen normalized synthetic protocol admits each ``INITIALIZATION`` batch as
        a *complete* snapshot, not a fragment to append. Clear old orders and levels before
        applying its records, including when its first record is not an explicit CLEAR.
        Preserve trading status, the anomaly ledger, and the monotone priority counter:
        new snapshot orders receive fresh priorities in their source insertion order.

        Record-driven replay callers must call this once before the batch's first record,
        then apply the records and commit. ``apply_batch`` performs that sequence itself.
        Live batches retain every resting order and its priority. Source-quality flags
        remain the evaluator's responsibility; replacing state never certifies recovery.
        """
        self._current_batch_id = batch.batch_id
        self._touched.clear()
        self._touched_clear = batch.kind is BatchKind.INITIALIZATION
        if self._touched_clear:
            self._apply_clear()

    def apply_batch(self, batch: EventBatch) -> BookSnapshot:
        """Apply every record in ``batch`` then commit, returning the committed snapshot."""
        self.begin_batch(batch)
        for record in batch.records:
            self.apply_record(record)
        return self.commit_batch(batch)

    def commit_batch(self, batch: EventBatch) -> BookSnapshot:
        """Validate boundary invariants and return the committed snapshot.

        Spread/crossing invariants are only meaningful at a committed boundary and in an
        eligible trading state (architecture section 5); structural invariants are always
        checked. A violation is recorded as an anomaly but does not raise, so a corrupt
        feed is diagnosed rather than silently crashing the replay.
        """
        self._current_batch_id = batch.batch_id
        if self._commit_validation == "full":
            violations = self.validate_invariants()
        else:
            violations = self._validate_touched(batch)
        for detail in violations:
            self._anomalies.append(
                Anomaly(
                    kind="INVARIANT_VIOLATION",
                    batch_id=batch.batch_id,
                    source_ordinal=-1,
                    detail=detail,
                )
            )
        return self._build_snapshot(
            batch_id=batch.batch_id,
            exchange_proxy_time=batch.exchange_proxy_time,
            capture_complete_time=batch.capture_complete_time,
            depth=0,
        )

    # -- queries ---------------------------------------------------------------------

    def _ordered_prices(self, side: Side) -> list[int]:
        """Prices best-first for the side: bids descending, asks ascending."""
        _, prices = self._levels_for(side)
        return list(reversed(prices)) if side is Side.BID else list(prices)

    def _build_snapshot(
        self,
        *,
        batch_id: int,
        exchange_proxy_time: int,
        capture_complete_time: int,
        depth: int,
    ) -> BookSnapshot:
        bids = self._levels_as_tuple(Side.BID, depth)
        asks = self._levels_as_tuple(Side.ASK, depth)
        return BookSnapshot(
            batch_id=batch_id,
            exchange_proxy_time=exchange_proxy_time,  # type: ignore[arg-type]
            capture_complete_time=capture_complete_time,  # type: ignore[arg-type]
            bids=bids,
            asks=asks,
            status=self._status,
        )

    def _levels_as_tuple(self, side: Side, depth: int) -> tuple[BookLevel, ...]:
        levels, _ = self._levels_for(side)
        ordered = self._ordered_prices(side)
        if depth > 0:
            ordered = ordered[:depth]
        out: list[BookLevel] = []
        for price in ordered:
            level = levels[price]
            out.append(
                BookLevel(
                    price_fixed=price,
                    quantity=level.total_quantity,
                    order_count=len(level.orders),
                )
            )
        return tuple(out)

    def snapshot(self, depth: int = 10) -> BookSnapshot:
        """Point-in-time snapshot (best level first), limited to ``depth`` levels/side."""
        return self._build_snapshot(
            batch_id=self._current_batch_id,
            exchange_proxy_time=0,
            capture_complete_time=0,
            depth=depth,
        )

    def best_bid_ask(self) -> tuple[BookLevel | None, BookLevel | None]:
        bid = self._best_level(Side.BID)
        ask = self._best_level(Side.ASK)
        return bid, ask

    def _best_level(self, side: Side) -> BookLevel | None:
        _, prices = self._levels_for(side)
        if not prices:
            return None
        price = prices[-1] if side is Side.BID else prices[0]
        level = self._levels_for(side)[0][price]
        return BookLevel(
            price_fixed=price, quantity=level.total_quantity, order_count=len(level.orders)
        )

    def get_order(self, key: OrderKey) -> RestingOrder | None:
        return self._orders.get(key)

    def orders_at(self, side: Side, price_fixed: int) -> tuple[RestingOrder, ...]:
        """Resting orders at a price in priority order (ahead first)."""
        levels, _ = self._levels_for(side)
        level = levels.get(price_fixed)
        if level is None:
            return ()
        return tuple(level.orders.values())

    def visible_quantity_ahead(self, key: OrderKey) -> int:
        """Total resting quantity strictly ahead of ``key`` at its price level.

        Ahead means a strictly smaller ``priority_seq`` on the same side and price. Returns
        0 if the order is unknown or is at the front of its level.
        """
        order = self._orders.get(key)
        if order is None:
            return 0
        levels, _ = self._levels_for(order.side)
        level = levels.get(order.price_fixed)
        if level is None:
            return 0
        ahead = 0
        for other in level.orders.values():
            if other.priority_seq < order.priority_seq:
                ahead += other.quantity
        return ahead

    # -- invariants ------------------------------------------------------------------

    def _validate_touched(self, batch: EventBatch) -> list[str]:
        """Boundary-mode check: crossing plus structure of levels touched since last commit.

        ``batch`` is accepted for interface symmetry; touched levels are recorded during
        :meth:`apply_record`, so a price-changing MODIFY's old level is included.
        """
        del batch
        touched = self._touched
        self._touched = set()
        if self._touched_clear:
            self._touched_clear = False
            return self.validate_invariants()
        problems: list[str] = []
        for side, price in sorted(touched, key=lambda t: (t[0].value, t[1])):
            levels, prices = self._levels_for(side)
            level = levels.get(price)
            idx = bisect.bisect_left(prices, price)
            listed = idx < len(prices) and prices[idx] == price
            if level is None:
                if listed:
                    problems.append(f"{side.name} price {price} listed without a level")
                continue
            if not listed:
                problems.append(f"{side.name} level {price} missing from price list")
            if level.is_empty():
                problems.append(f"{side.name} level {price} is present but empty")
            summed = 0
            last_seq = -1
            for o in level.orders.values():
                if o.quantity <= 0:
                    problems.append(f"order {o.key} has nonpositive quantity {o.quantity}")
                if o.price_fixed != price:
                    problems.append(f"order {o.key} at level {price} has price {o.price_fixed}")
                if o.priority_seq <= last_seq:
                    problems.append(f"{side.name} level {price} orders not in priority order")
                last_seq = o.priority_seq
                summed += o.quantity
            if summed != level.total_quantity:
                problems.append(
                    f"{side.name} level {price} total {level.total_quantity} != "
                    f"sum of orders {summed}"
                )
        if self._status is TradingStatus.TRADING and self._bid_prices and self._ask_prices:
            best_bid = self._bid_prices[-1]
            best_ask = self._ask_prices[0]
            if best_bid >= best_ask:
                problems.append(
                    f"crossed book: best bid {best_bid} >= best ask {best_ask} while TRADING"
                )
        return problems

    def validate_invariants(self) -> list[str]:
        """Return a list of invariant-violation messages; empty means OK."""
        problems: list[str] = []

        for key, order in self._orders.items():
            if order.quantity <= 0:
                problems.append(f"order {key} has nonpositive quantity {order.quantity}")
            if order.key != key:
                problems.append(f"order map key {key} does not match order.key {order.key}")

        for side in (Side.BID, Side.ASK):
            levels, prices = self._levels_for(side)
            if sorted(prices) != prices:
                problems.append(f"{side.name} price list not sorted ascending")
            if len(set(prices)) != len(prices):
                problems.append(f"{side.name} price list has duplicate prices")
            for price in prices:
                level = levels[price]
                if level.is_empty():
                    problems.append(f"{side.name} level {price} is present but empty")
                summed = sum(o.quantity for o in level.orders.values())
                if summed != level.total_quantity:
                    problems.append(
                        f"{side.name} level {price} total {level.total_quantity} != "
                        f"sum of orders {summed}"
                    )
                seqs = [o.priority_seq for o in level.orders.values()]
                if seqs != sorted(seqs):
                    problems.append(f"{side.name} level {price} orders not in priority order")
                for o in level.orders.values():
                    if o.price_fixed != price:
                        problems.append(f"order {o.key} at level {price} has price {o.price_fixed}")

        # Spread/crossing invariant: only meaningful in an eligible trading state.
        if self._status is TradingStatus.TRADING and self._bid_prices and self._ask_prices:
            best_bid = self._bid_prices[-1]
            best_ask = self._ask_prices[0]
            if best_bid >= best_ask:
                problems.append(
                    f"crossed book: best bid {best_bid} >= best ask {best_ask} while TRADING"
                )

        return problems

    # -- status ----------------------------------------------------------------------

    def set_status(self, status: TradingStatus) -> None:
        self._status = status

    @property
    def status(self) -> TradingStatus:
        return self._status

    @property
    def anomalies(self) -> list[Anomaly]:
        return self._anomalies

    # -- copy ------------------------------------------------------------------------

    def copy(self) -> ReferenceBook:
        """Deep, independent copy (used for checkpoint/restore).

        :class:`RestingOrder` is frozen, so order objects can be shared, but every mutable
        container (order map, levels, price lists, anomaly list) is rebuilt so that
        mutating the copy never touches the original.
        """
        clone = ReferenceBook(
            self._instrument,
            modify_unknown_as_add=self._modify_unknown_as_add,
            commit_validation=self._commit_validation,
        )
        clone._touched = set(self._touched)
        clone._orders = dict(self._orders)
        clone._bid_prices = list(self._bid_prices)
        clone._ask_prices = list(self._ask_prices)
        clone._priority_counter = self._priority_counter
        clone._status = self._status
        clone._anomalies = list(self._anomalies)
        clone._current_batch_id = self._current_batch_id
        for src_levels, dst_levels in (
            (self._bid_levels, clone._bid_levels),
            (self._ask_levels, clone._ask_levels),
        ):
            for price, level in src_levels.items():
                new_level = _Level(price)
                new_level.orders = dict(level.orders)
                new_level.total_quantity = level.total_quantity
                dst_levels[price] = new_level
        return clone
