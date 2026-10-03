"""Execution evidence ledger (architecture section 8.2).

The admitted synthetic execution group is one positive ``TRADE`` followed by matching
``FILL``/``CANCEL`` pairs whose total quantity equals the print. Validation happens once
per complete atomic batch. Immutable attribution is shared by worlds; an overlay never
credits a standalone print or orphan fill, and checks each actual resting allocation
against the historical book before granting a virtual fill.

The ledger records candidate allocations against the virtual order. Entries retain the
source group's TRADE ordinal, allocation ordinal, eligible volume, and displaced order
key; multiple candidates in one source group share a group id. A world can consume at
most one contract, across all prices and mechanisms. Historical records remain immutable.

Conflicting ahead priority is logged as ambiguous and grants no fill. Invalid source
grouping or unsupported resting quantity is excluded by the policy-independent quality
map, rather than inventing missing aggressor volume.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from qexec.core.tasks import FillMechanism
from qexec.core.types import UNDEF_PRICE, Action, CanonicalRecord, EventBatch, OrderKey, Side
from qexec.reference.book import ReferenceBook


@dataclass(frozen=True, slots=True)
class SupportedAllocation:
    """One positive resting allocation linked to its fully reconciled source print."""

    trade: CanonicalRecord
    fill: CanonicalRecord


@dataclass(frozen=True, slots=True)
class BatchExecutionEvidence:
    """Immutable attribution for one complete atomic batch, shared across policy worlds."""

    allocations: Mapping[int, SupportedAllocation]
    invalid: bool


def execution_groups(batch: EventBatch) -> BatchExecutionEvidence:
    """Validate the admitted synthetic TRADE, (FILL, CANCEL)+ protocol.

    Each print has one positive volume budget. Every allocation must match the print's
    instrument, epoch, publisher, price and resting side; every reduction must exactly
    match its immediately preceding allocation. Summed allocations equal print volume.
    A standalone print or orphan fill is unsupported. No lookahead beyond this atomic
    source batch is used, and no records from distinct groups are combined.
    """
    allocations: dict[int, SupportedAllocation] = {}
    records = batch.records
    index = 0
    invalid = False
    while index < len(records):
        trade = records[index]
        if trade.action is not Action.TRADE:
            invalid |= trade.action is Action.FILL
            index += 1
            continue
        index += 1
        valid = (
            trade.quantity > 0
            and trade.side in (Side.BID, Side.ASK)
            and trade.order_id == 0
            and trade.price_fixed != UNDEF_PRICE
            and trade.instrument_id == batch.instrument_id
            and not trade.is_snapshot
        )
        fills: list[CanonicalRecord] = []
        previous_ordinal = trade.source_ordinal
        while index < len(records) and records[index].action is Action.FILL:
            fill = records[index]
            fills.append(fill)
            index += 1
            if index >= len(records) or records[index].action is not Action.CANCEL:
                valid = False
                break
            cancel = records[index]
            index += 1
            valid &= _allocation_matches(trade, fill, cancel, previous_ordinal)
            previous_ordinal = cancel.source_ordinal
        valid &= bool(fills) and sum(fill.quantity for fill in fills) == trade.quantity
        if not valid:
            invalid = True
        else:
            for fill in fills:
                if fill.source_ordinal in allocations:
                    invalid = True
                allocations[fill.source_ordinal] = SupportedAllocation(trade, fill)
    # Atomic source corruption excludes every policy, and cannot produce an overlay fill.
    if invalid or batch.quality_flags:
        allocations.clear()
    return BatchExecutionEvidence(MappingProxyType(allocations), invalid)


def _allocation_matches(
    trade: CanonicalRecord, fill: CanonicalRecord, cancel: CanonicalRecord, previous_ordinal: int
) -> bool:
    return (
        fill.quantity > 0
        and fill.order_id > 0
        and fill.side in (Side.BID, Side.ASK)
        and fill.side is not trade.side
        and fill.price_fixed == trade.price_fixed
        and fill.order_key().publisher_id == trade.publisher_id
        and fill.instrument_id == trade.instrument_id
        and fill.session_epoch == trade.session_epoch
        and not fill.is_snapshot
        and cancel.order_key() == fill.order_key()
        and cancel.side is fill.side
        and cancel.price_fixed == fill.price_fixed
        and cancel.quantity == fill.quantity
        and not cancel.is_snapshot
        and previous_ordinal < fill.source_ordinal < cancel.source_ordinal
    )


def valid_resting_allocation(record: CanonicalRecord, book_before: ReferenceBook) -> bool:
    """Validate resting quantity and historical price priority before book mutation.

    Direct execution cannot skip a better visible price, independently of the virtual
    order's own ahead cohort. Same-price queue priority remains the overlay's explicit
    ambiguity check; it is not silently discarded by this source-level predicate.
    """
    order = book_before.get_order(record.order_key())
    bid, ask = book_before.best_bid_ask()
    best = bid if record.side is Side.BID else ask
    return (
        record.action is Action.FILL
        and record.quantity > 0
        and order is not None
        and best is not None
        and record.price_fixed == best.price_fixed
        and record.side is order.side
        and record.price_fixed == order.price_fixed
        and record.quantity <= order.quantity
    )


class AmbiguityKind(Enum):
    """Why an execution group was tagged ambiguous (architecture section 8.2)."""

    NONE = "NONE"
    """No ambiguity; the evidence cleanly supports (or does not support) a fill."""
    BEHIND_FILL_WHILE_AHEAD_POSITIVE = "BEHIND_FILL_WHILE_AHEAD_POSITIVE"
    """A behind-us order filled while our computed quantity ahead was still positive;
    inconsistent with strict FIFO. Under ``AMBIGUITY_FILL = False`` no fill is granted."""
    TRADE_THROUGH_WHILE_AHEAD_POSITIVE = "TRADE_THROUGH_WHILE_AHEAD_POSITIVE"
    """A through-price allocation skipped still-resting quantity ahead of the virtual order."""


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """One economic execution group as evaluated against the virtual order.

    ``granted_fill`` records whether this group produced a virtual fill. ``mechanism`` is
    the classification of that fill (or the classification that *would* apply were a fill
    granted, for an ambiguous group). ``displaced_allocation`` identifies the historical
    allocation the virtual fill is credited against: the behind/at-price order's
    ``OrderKey`` for both queue-depletion and trade-through; ``trade_price_fixed`` also
    captures the print price for through-price execution.
    """

    group_id: str
    event_time_ns: int
    source_ordinal: int
    mechanism: FillMechanism
    granted_fill: bool
    ambiguity: AmbiguityKind
    quantity_ahead_before: int
    """Overlay-computed quantity ahead on ``book_before`` for this group (audit only)."""
    our_side: Side
    our_limit_price_fixed: int
    displaced_allocation: OrderKey | None = None
    """Historical order whose allocation supports the hypothetical fill."""
    trade_price_fixed: int | None = None
    """Print price of a through-price ``TRADE`` (trade-through), else ``None``."""
    detail: str = ""
    source_group_ordinal: int | None = None
    """Source ordinal of the reconciled TRADE print anchoring this execution group."""
    eligible_volume: int = 0
    """Positive historical allocation volume supporting this evidence (not invented residual)."""


@dataclass(slots=True)
class ExecutionEvidenceLedger:
    """Append-only list of :class:`ExecutionEvidence`, deep-copied with the overlay."""

    entries: list[ExecutionEvidence] = field(default_factory=list)

    def record(self, evidence: ExecutionEvidence) -> None:
        self.entries.append(evidence)

    @property
    def ambiguous(self) -> list[ExecutionEvidence]:
        """Entries tagged with any ambiguity (audit convenience)."""
        return [e for e in self.entries if e.ambiguity is not AmbiguityKind.NONE]

    @property
    def fills(self) -> list[ExecutionEvidence]:
        """Entries that granted a virtual fill."""
        return [e for e in self.entries if e.granted_fill]

    def copy(self) -> ExecutionEvidenceLedger:
        """Independent copy; :class:`ExecutionEvidence` is frozen so it may be shared."""
        return ExecutionEvidenceLedger(entries=list(self.entries))
