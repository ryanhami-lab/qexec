"""Shared core contracts for QExec.

This module is the single code-level source of truth for record, price, time, and
enum conventions. Every package imports from here; no package may redefine these.

Conventions (architecture sections 4 and 6):

* Time is integer nanoseconds since the Unix epoch (``TimeNs``).
* Prices are integer fixed-point units of 1e-9 quoted price units (``PriceFixed``),
  matching the Databento convention. ``PRICE_SCALE`` converts to quoted units.
* A midpoint is represented exactly as ``Mid2 = bid + ask`` (twice the midpoint), so
  half-tick midpoints never require rounding.
* Quantities are positive integers (contracts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum, IntFlag
from typing import NewType

TimeNs = NewType("TimeNs", int)
PriceFixed = NewType("PriceFixed", int)
Mid2 = NewType("Mid2", int)
OrderId = NewType("OrderId", int)

PRICE_SCALE: int = 1_000_000_000
"""Fixed-point units per quoted price unit."""

UNDEF_PRICE: int = 2**63 - 1
"""Databento-style undefined price sentinel; preserved until validation."""

SCHEMA_VERSION: int = 1


class Side(Enum):
    """Book side or order side.

    For ``TRADE`` records ``side`` is the aggressor side; for ``FILL`` records it is the
    resting order's side (Databento CME convention). ``NONE`` means not applicable.
    """

    BID = "B"
    ASK = "A"
    NONE = "N"

    def opposite(self) -> Side:
        if self is Side.BID:
            return Side.ASK
        if self is Side.ASK:
            return Side.BID
        raise ValueError("Side.NONE has no opposite")


class TaskSide(IntEnum):
    """Execution task direction. The integer value is the sign ``s`` in all formulas."""

    BUY = 1
    SELL = -1

    @property
    def book_side(self) -> Side:
        """Side of the book on which this task's passive order rests."""
        return Side.BID if self is TaskSide.BUY else Side.ASK


class Action(Enum):
    """Normalized MBO actions (architecture section 4.2).

    ADD/MODIFY/CANCEL/CLEAR mutate the resting book. TRADE/FILL/NONE never mutate it;
    execution attribution is always accompanied by separate book-changing records.

    * ``CANCEL.quantity`` is the quantity removed (partial removal allowed).
    * ``MODIFY`` carries the order's new absolute price and quantity. A price change or
      size increase loses priority; a pure size decrease retains priority.
    * ``CLEAR`` removes every resting order for the instrument.
    """

    ADD = "A"
    MODIFY = "M"
    CANCEL = "C"
    CLEAR = "R"
    TRADE = "T"
    FILL = "F"
    NONE = "N"

    @property
    def mutates_book(self) -> bool:
        return self in (Action.ADD, Action.MODIFY, Action.CANCEL, Action.CLEAR)


class RecordFlag(IntFlag):
    """Record flags (Databento bit layout)."""

    NONE = 0
    MAYBE_BAD_BOOK = 1 << 2
    BAD_TS_RECV = 1 << 3
    MBP = 1 << 4
    SNAPSHOT = 1 << 5
    TOB = 1 << 6
    LAST = 1 << 7


class TradingStatus(Enum):
    """Instrument trading state. ``UNKNOWN`` is never treated as continuous trading."""

    PRE_OPEN = "PRE_OPEN"
    TRADING = "TRADING"
    HALTED = "HALTED"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class InstrumentDefinition:
    """Time-effective instrument metadata."""

    instrument_id: int
    symbol: str
    tick_size_fixed: PriceFixed
    """Tick size in fixed-point units, e.g. 0.25 -> 250_000_000."""
    multiplier: int
    """Currency per quoted price unit per contract (``M`` in the product spec)."""
    effective_time_ns: TimeNs

    def is_tick_aligned(self, price: int) -> bool:
        return price % self.tick_size_fixed == 0


@dataclass(frozen=True, slots=True)
class StatusEvent:
    instrument_id: int
    status: TradingStatus
    event_time_ns: TimeNs
    capture_time_ns: TimeNs


@dataclass(frozen=True, slots=True)
class CanonicalRecord:
    """One normalized source record (architecture section 4.1).

    ``source_sequence`` and ``channel_id`` are ``None`` when absent from the source;
    they are never fabricated. ``source_ordinal`` preserves normalized order exactly.
    """

    dataset_id: str
    publisher_id: int
    instrument_id: int
    session_epoch: int
    source_ordinal: int
    action: Action
    side: Side
    price_fixed: int
    quantity: int
    order_id: int
    event_time_ns: TimeNs
    capture_time_ns: TimeNs
    flags: RecordFlag
    source_file_hash: str
    source_record_offset: int
    channel_id: int | None = None
    source_sequence: int | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def is_last(self) -> bool:
        return bool(self.flags & RecordFlag.LAST)

    @property
    def is_snapshot(self) -> bool:
        return bool(self.flags & RecordFlag.SNAPSHOT)

    def order_key(self) -> OrderKey:
        return OrderKey(self.publisher_id, self.instrument_id, self.session_epoch, self.order_id)


@dataclass(frozen=True, slots=True, order=True)
class OrderKey:
    """Unique resting-order identity (architecture section 4.1)."""

    publisher_id: int
    instrument_id: int
    session_epoch: int
    order_id: int


class BatchKind(Enum):
    INITIALIZATION = "INITIALIZATION"
    LIVE = "LIVE"


class QualityFlag(Enum):
    TRUNCATED = "TRUNCATED"
    DECREASING_PROXY_TIME = "DECREASING_PROXY_TIME"
    CAPTURE_BEFORE_PROXY = "CAPTURE_BEFORE_PROXY"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_RECORD = "INVALID_RECORD"
    MAYBE_BAD_BOOK = "MAYBE_BAD_BOOK"


@dataclass(frozen=True, slots=True)
class EventBatch:
    """A completed source event, closed by a record carrying ``RecordFlag.LAST``.

    ``exchange_proxy_time`` is the maximum valid ``event_time_ns`` and
    ``capture_complete_time`` the maximum valid ``capture_time_ns`` within the batch
    (architecture section 6.1 baseline).
    """

    batch_id: int
    instrument_id: int
    records: tuple[CanonicalRecord, ...]
    exchange_proxy_time: TimeNs
    capture_complete_time: TimeNs
    kind: BatchKind
    quality_flags: frozenset[QualityFlag] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price_fixed: int
    quantity: int
    order_count: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Immutable committed book state (best level first on each side)."""

    batch_id: int
    exchange_proxy_time: TimeNs
    capture_complete_time: TimeNs
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    status: TradingStatus

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    def mid2(self) -> Mid2 | None:
        """Twice the midpoint, or ``None`` if either side is empty or the book is crossed."""
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None or bb.price_fixed >= ba.price_fixed:
            return None
        return Mid2(bb.price_fixed + ba.price_fixed)


def mid2_to_price(mid2: int) -> float:
    """Convert ``Mid2`` to quoted price units (for reporting only, never for ledgers)."""
    return mid2 / (2 * PRICE_SCALE)
