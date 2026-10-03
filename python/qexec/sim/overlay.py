"""Exchange-side hypothetical order for one task (architecture sections 7.3 and 8).

The :class:`ExchangeOverlay` is a *pure exchange-side* state machine (no latency; the
engine owns delays). It observes the immutable historical feed through a read-only
:class:`~qexec.reference.book.ReferenceBook` and emits
:class:`~qexec.core.messages.ExchangeReport` plus :class:`~qexec.core.tasks.Execution`
objects. It never mutates the historical book (architecture section 8.1/8.3).

Driving protocol (engine order, architecture section 6.3 / contract 2.2)::

    evidence = execution_groups(batch)  # shared immutable positive-volume attribution
    for record in batch.records:
        reports += overlay.on_record(record, book, evidence)  # BEFORE the book applies it
        book.apply_record(record)
    book.commit_batch(batch)
    reports += overlay.on_batch_committed(batch, book)   # committed state

Commands arrive separately: ``overlay.on_command(cmd, book)`` where ``book`` is the
committed historical state at command arrival.

Semantics (contract WP2-OVERLAY; follow exactly)
================================================

Passive limit at arrival (:meth:`on_command`, ``PASSIVE_LIMIT``):

* status != ``TRADING`` -> ``REJECTED`` (reason ``STATUS``).
* limit not tick-aligned, or quantity != 1 -> ``REJECTED`` (reason ``INVALID_LIMIT`` /
  ``INVALID_QUANTITY``).
* marketable (buy: a best ask exists and ``limit >= best ask``; sell: a best bid exists
  and ``limit <= best bid``) -> ``FILL`` 1 at the *opposite best price*,
  ``Liquidity.AGGRESSIVE``, ``FillMechanism.AGGRESSIVE``. Does not rest.
* otherwise ``ACCEPTED`` resting at its limit *behind all currently resting orders* at
  that price and side. The ahead set is the keys of ``book.orders_at(side, limit)`` in
  priority order, each remembered with its current ``priority_seq``.

Queue-ahead quantity (audit; never exposed to policies) is the sum of the *current*
historical book quantities of ahead-set orders that are *still at the limit price on our
side with retained priority*. An ahead order that changes price or increases size loses
priority and leaves the ahead set permanently (detected by a changed ``priority_seq`` or a
move off ``(side, limit)``); a pure size decrease retains priority and keeps its (reduced)
quantity in the sum. FILL quantities are never subtracted by the overlay: the historical
feed's ``CANCEL`` reduces the resting quantity, so the book counts the reduction exactly
once (contract 2.1, T06).

Record-driven fills while WORKING (evaluated on ``book_before``, i.e. the state *before*
the record applies):

Only positive, matching resting allocations in a reconciled source execution group
are eligible. Standalone prints, orphan fills and malformed group budgets never fill.

(a) ``FILL`` on our side at our limit of an order NOT in the ahead set (a behind-us
    order). Compute quantity ahead on ``book_before`` excluding the filled order. If it is
    0 -> virtual ``FILL`` 1 at our limit, ``PASSIVE``, ``QUEUE_DEPLETION``. If it is > 0
    the behind-order fill is inconsistent with FIFO -> log ``AMBIGUOUS`` evidence and,
    under the frozen rule ``AMBIGUITY_FILL = False``, grant no fill.
(b) A linked positive ``FILL`` confirming a ``TRADE`` whose aggressor is the opposing
    side at a price strictly through our limit (buy: ``price < limit``; sell:
    ``price > limit``), with no surviving quantity ahead -> virtual ``FILL`` 1 at OUR
    limit, ``PASSIVE``, ``TRADE_THROUGH``. Surviving ahead quantity is conflicting FIFO
    evidence: log ambiguity and grant no fill.
(c) ``FILL`` of an ahead-set order -> no virtual fill (T07). Exact exhaustion of the
    quantity ahead without further eligible evidence -> no fill (T08). ``CANCEL`` / quote
    move / touch never fills (T09, T10).

At most one unit per execution group, and the order finishes after its single fill.

``CANCEL`` command: target working -> ``CANCELLED``; target already terminal ->
``CANCEL_REJECTED_TERMINAL`` with the target's ``cumulative_executed``; target
in-flight/unknown -> ``CANCEL_REJECTED_TERMINAL`` reason ``UNKNOWN_TARGET`` (the engine
guarantees cancels arrive after the passive arrives, so an unknown target is a
protocol/race case, surfaced rather than silently accepted).

``AGGRESSIVE`` command (SYNTHETIC_DIRECT_TOP): if ``TRADING`` and opposite best qty >= 1
-> ``FILL`` 1 at opposite best, ``AGGRESSIVE``/``AGGRESSIVE``; else ``AGGRESSIVE_UNFILLED``
(reason ``HALTED`` / ``NO_LIQUIDITY`` / ``STATUS``). Never rests, never retries. If the
virtual order is already executed, an aggressive command -> ``AGGRESSIVE_UNFILLED`` reason
``ALREADY_COMPLETE`` (prevents overexecution; T14).

``CLEAR`` record or an ``INITIALIZATION`` (snapshot) batch while WORKING -> a
``TECHNICAL_RESET`` report, ``technically_invalid = True``, and no further fills (T37).

Invariant: ``executed_quantity <= 1`` always (asserted on every fill).

Interpretations / conservative choices (reported in the final message)
======================================================================

1. *Marketability test uses the committed book at command arrival* (``on_command``'s
   ``book``), consistent with "committed historical state at arrival".
2. *A record-driven ``FILL`` must identify sufficient matching resting quantity*. Its
   paired reduction and positive source TRADE budget are validated as one atomic group.
   ``TRADE`` records carry ``order_id == 0`` but alone never authorize a fill.
3. *Ambiguity never fabricates a fill* (``AMBIGUITY_FILL = False``): the conservative rule.
4. *A ``CLEAR`` record is a reset even if it is itself the only record*; snapshot
   (``SNAPSHOT``-flagged / ``INITIALIZATION`` batch) rebuilds are likewise treated as a
   loss of queue continuity while working.
5. *``on_record`` evaluates against ``book_before``* so that a FILL and its following
   historical ``CANCEL`` (which reduces the resting quantity) are each seen once in the
   correct pre-effect state (contract 2.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qexec.core.messages import (
    CommandKind,
    ExchangeCommand,
    ExchangeReport,
    ReportKind,
)
from qexec.core.tasks import Execution, FillMechanism, Liquidity
from qexec.core.types import (
    Action,
    BatchKind,
    CanonicalRecord,
    EventBatch,
    InstrumentDefinition,
    OrderKey,
    Side,
    TaskSide,
    TradingStatus,
)
from qexec.reference.book import ReferenceBook
from qexec.sim.evidence import (
    AmbiguityKind,
    BatchExecutionEvidence,
    ExecutionEvidence,
    ExecutionEvidenceLedger,
    SupportedAllocation,
    valid_resting_allocation,
)

AMBIGUITY_FILL: bool = False
"""Frozen ambiguity rule: a behind-order fill while quantity ahead is still positive does
NOT grant a virtual fill (the conservative interpretation). The event is always logged as
``AMBIGUOUS`` evidence regardless (architecture section 8.2)."""


class _State(Enum):
    """Internal overlay lifecycle (not exposed; policies see reports only)."""

    IDLE = "IDLE"
    """No passive order submitted yet."""
    WORKING = "WORKING"
    """A passive order is resting and eligible for virtual fills."""
    TERMINAL = "TERMINAL"
    """The virtual order is done (filled, cancelled, rejected, or reset)."""


@dataclass(slots=True)
class _AheadMember:
    """An ahead-set order remembered at ACCEPTED time, with its priority at that moment."""

    priority_seq: int


class ExchangeOverlay:
    """Per-task exchange-side hypothetical order. See the module docstring for semantics."""

    def __init__(
        self,
        task_id: str,
        instrument: InstrumentDefinition,
        fee_per_contract_fixed: int,
    ) -> None:
        self._task_id = task_id
        self._instrument = instrument
        self._fee = fee_per_contract_fixed

        self._state: _State = _State.IDLE
        self._side: TaskSide | None = None
        self._limit: int | None = None
        self._passive_command_id: str | None = None

        # Ahead set frozen at ACCEPTED: key -> remembered priority_seq. A member leaves
        # permanently once it moves off (side, limit) or its priority_seq changes.
        self._ahead: dict[OrderKey, _AheadMember] = {}

        self._executed: int = 0
        self._technically_invalid: bool = False
        self._last_queue_ahead: int = 0

        self._report_counter: int = 0
        self._execution_counter: int = 0
        self._ledger = ExecutionEvidenceLedger()

    # -- identifiers -----------------------------------------------------------------

    def _next_report_id(self) -> str:
        rid = f"{self._task_id}:{self._report_counter}"
        self._report_counter += 1
        return rid

    def _next_execution_id(self) -> str:
        eid = f"{self._task_id}:exec:{self._execution_counter}"
        self._execution_counter += 1
        return eid

    # -- public read-only state ------------------------------------------------------

    @property
    def executed_quantity(self) -> int:
        assert self._executed <= 1, "overlay over-executed (invariant violated)"
        return self._executed

    @property
    def is_working(self) -> bool:
        return self._state is _State.WORKING

    @property
    def technically_invalid(self) -> bool:
        return self._technically_invalid

    @property
    def ledger(self) -> ExecutionEvidenceLedger:
        return self._ledger

    def true_queue_ahead(self) -> int | None:
        """Audit-only current quantity ahead; ``None`` when not working.

        Never exposed to policies (the client observes a delayed cohort proxy instead,
        WP-FEATURES). This returns the quantity ahead as of the last book the overlay was
        driven with (``on_command`` acceptance, ``on_record``, or an explicit
        :meth:`refresh_queue_ahead`). It is ``None`` once the order is terminal/idle.
        """
        if self._state is not _State.WORKING:
            return None
        return self._last_queue_ahead

    def refresh_queue_ahead(self, book: ReferenceBook) -> int | None:
        """Recompute and cache the audit queue-ahead against ``book`` (audit only)."""
        if self._state is not _State.WORKING:
            return None
        self._last_queue_ahead = self._prune_and_sum_ahead(book)
        return self._last_queue_ahead

    # -- queue-ahead -----------------------------------------------------------------

    def _book_side(self) -> Side:
        assert self._side is not None
        return self._side.book_side

    def _prune_and_sum_ahead(self, book: ReferenceBook) -> int:
        """Return current quantity ahead, pruning members that lost priority/left level.

        An ahead member is still ahead iff the book still has an order at that key, on our
        side, at our limit, with the *same* ``priority_seq`` recorded at ACCEPTED (a size
        decrease retains it; a price change or size increase changes it). Pruning is
        permanent, matching "leaves the ahead set permanently".
        """
        assert self._limit is not None
        side = self._book_side()
        total = 0
        for key in list(self._ahead):
            member = self._ahead[key]
            order = book.get_order(key)
            if (
                order is None
                or order.side is not side
                or order.price_fixed != self._limit
                or order.priority_seq != member.priority_seq
            ):
                del self._ahead[key]
                continue
            total += order.quantity
        self._last_queue_ahead = total
        return total

    # -- fill emission ---------------------------------------------------------------

    def _emit_fill(
        self,
        *,
        command_id: str,
        price_fixed: int,
        liquidity: Liquidity,
        mechanism: FillMechanism,
        exchange_time_ns: int,
    ) -> ExchangeReport:
        assert self._executed == 0, "second fill attempted (invariant violated)"
        self._executed = 1
        self._state = _State.TERMINAL
        execution = Execution(
            execution_id=self._next_execution_id(),
            task_id=self._task_id,
            quantity=1,
            price_fixed=price_fixed,
            fee_fixed=self._fee,
            exchange_time_ns=exchange_time_ns,  # type: ignore[arg-type]
            liquidity=liquidity,
            mechanism=mechanism,
        )
        return ExchangeReport(
            report_id=self._next_report_id(),
            command_id=command_id,
            task_id=self._task_id,
            kind=ReportKind.FILL,
            exchange_time_ns=exchange_time_ns,
            cumulative_executed=1,
            execution=execution,
            reason="",
        )

    # -- commands --------------------------------------------------------------------

    def on_command(self, cmd: ExchangeCommand, book: ReferenceBook) -> list[ExchangeReport]:
        """Process a command against the committed historical ``book`` at arrival."""
        if cmd.kind is CommandKind.PASSIVE_LIMIT:
            return self._on_passive(cmd, book)
        if cmd.kind is CommandKind.AGGRESSIVE:
            return self._on_aggressive(cmd, book)
        if cmd.kind is CommandKind.CANCEL:
            return self._on_cancel(cmd)
        raise ValueError(f"unknown command kind {cmd.kind!r}")  # pragma: no cover

    def _on_passive(self, cmd: ExchangeCommand, book: ReferenceBook) -> list[ExchangeReport]:
        t = cmd.arrival_time_ns
        if book.status is not TradingStatus.TRADING:
            self._state = _State.TERMINAL
            return [self._reject(cmd.command_id, t, "STATUS")]
        if cmd.quantity != 1:
            self._state = _State.TERMINAL
            return [self._reject(cmd.command_id, t, "INVALID_QUANTITY")]
        limit = cmd.limit_price_fixed
        if limit is None or not self._instrument.is_tick_aligned(limit):
            self._state = _State.TERMINAL
            return [self._reject(cmd.command_id, t, "INVALID_LIMIT")]

        self._side = cmd.side
        self._limit = limit
        self._passive_command_id = cmd.command_id
        book_side = cmd.side.book_side
        best_bid, best_ask = book.best_bid_ask()

        # Marketable on arrival?
        if cmd.side is TaskSide.BUY:
            marketable = best_ask is not None and limit >= best_ask.price_fixed
            opp_price = best_ask.price_fixed if best_ask is not None else None
        else:
            marketable = best_bid is not None and limit <= best_bid.price_fixed
            opp_price = best_bid.price_fixed if best_bid is not None else None
        if marketable and opp_price is not None:
            return [
                self._emit_fill(
                    command_id=cmd.command_id,
                    price_fixed=opp_price,
                    liquidity=Liquidity.AGGRESSIVE,
                    mechanism=FillMechanism.AGGRESSIVE,
                    exchange_time_ns=t,
                )
            ]

        # Rest behind all currently resting orders at (side, limit).
        self._ahead = {
            o.key: _AheadMember(priority_seq=o.priority_seq)
            for o in book.orders_at(book_side, limit)
        }
        self._state = _State.WORKING
        self._last_queue_ahead = sum(o.quantity for o in book.orders_at(book_side, limit))
        return [
            ExchangeReport(
                report_id=self._next_report_id(),
                command_id=cmd.command_id,
                task_id=self._task_id,
                kind=ReportKind.ACCEPTED,
                exchange_time_ns=t,
                cumulative_executed=0,
                execution=None,
                reason="",
            )
        ]

    def _on_aggressive(self, cmd: ExchangeCommand, book: ReferenceBook) -> list[ExchangeReport]:
        t = cmd.arrival_time_ns
        if self._executed >= 1:
            return [self._aggressive_unfilled(cmd.command_id, t, "ALREADY_COMPLETE")]
        if book.status is not TradingStatus.TRADING:
            self._state = _State.TERMINAL
            reason = "HALTED" if book.status is TradingStatus.HALTED else "STATUS"
            return [self._aggressive_unfilled(cmd.command_id, t, reason)]
        best_bid, best_ask = book.best_bid_ask()
        opp = best_ask if cmd.side is TaskSide.BUY else best_bid
        if opp is None or opp.quantity < 1:
            self._state = _State.TERMINAL
            return [self._aggressive_unfilled(cmd.command_id, t, "NO_LIQUIDITY")]
        return [
            self._emit_fill(
                command_id=cmd.command_id,
                price_fixed=opp.price_fixed,
                liquidity=Liquidity.AGGRESSIVE,
                mechanism=FillMechanism.AGGRESSIVE,
                exchange_time_ns=t,
            )
        ]

    def _on_cancel(self, cmd: ExchangeCommand) -> list[ExchangeReport]:
        t = cmd.arrival_time_ns
        target = cmd.target_command_id
        # Unknown / in-flight target (should not happen; engine orders cancels after the
        # passive arrives). Surface it rather than silently accept.
        if target is None or target != self._passive_command_id:
            return [
                ExchangeReport(
                    report_id=self._next_report_id(),
                    command_id=cmd.command_id,
                    task_id=self._task_id,
                    kind=ReportKind.CANCEL_REJECTED_TERMINAL,
                    exchange_time_ns=t,
                    cumulative_executed=self._executed,
                    execution=None,
                    reason="UNKNOWN_TARGET",
                )
            ]
        if self._state is _State.WORKING:
            self._state = _State.TERMINAL
            self._ahead.clear()
            return [
                ExchangeReport(
                    report_id=self._next_report_id(),
                    command_id=cmd.command_id,
                    task_id=self._task_id,
                    kind=ReportKind.CANCELLED,
                    exchange_time_ns=t,
                    cumulative_executed=self._executed,
                    execution=None,
                    reason="",
                )
            ]
        # Target already terminal (filled / cancelled / rejected / reset).
        return [
            ExchangeReport(
                report_id=self._next_report_id(),
                command_id=cmd.command_id,
                task_id=self._task_id,
                kind=ReportKind.CANCEL_REJECTED_TERMINAL,
                exchange_time_ns=t,
                cumulative_executed=self._executed,
                execution=None,
                reason="ALREADY_TERMINAL",
            )
        ]

    # -- records ---------------------------------------------------------------------

    def on_record(
        self,
        record: CanonicalRecord,
        book_before: ReferenceBook,
        evidence: BatchExecutionEvidence | None = None,
    ) -> list[ExchangeReport]:
        """Observe a record before mutation; fills require validated atomic batch evidence."""
        if self._state is not _State.WORKING:
            return []
        assert self._side is not None and self._limit is not None
        command_id = self._passive_command_id or ""

        if record.instrument_id != self._instrument.instrument_id:
            return []

        # CLEAR while working -> technical reset (T37).
        if record.action is Action.CLEAR:
            return [self._technical_reset(command_id, record.event_time_ns)]

        if record.action is Action.FILL:
            allocation = evidence.allocations.get(record.source_ordinal) if evidence else None
            if (
                allocation is None
                or allocation.fill != record
                or book_before.status is not TradingStatus.TRADING
                or not self._instrument.is_tick_aligned(record.price_fixed)
                or not valid_resting_allocation(record, book_before)
            ):
                return []
            if record.price_fixed == self._limit:
                return self._on_fill_record(record, book_before, command_id, allocation)
            return self._on_trade_record(record, book_before, command_id, allocation)
        # ADD / MODIFY / CANCEL / NONE do not themselves fill (T09, T10). Queue-ahead is
        # recomputed lazily from the book on the next eligible evidence, so no bookkeeping
        # is required here: pruning happens inside _prune_and_sum_ahead.
        return []

    def _on_fill_record(
        self,
        record: CanonicalRecord,
        book_before: ReferenceBook,
        command_id: str,
        allocation: SupportedAllocation,
    ) -> list[ExchangeReport]:
        assert self._side is not None and self._limit is not None
        side = self._book_side()
        # Only resting fills on our side, at our limit, attributable to an order id.
        if record.side is not side or record.price_fixed != self._limit or record.order_id == 0:
            return []
        key = record.order_key()
        # Prune ahead set to current reality first (permanent removals), then test.
        ahead_qty = self._prune_and_sum_ahead(book_before)
        if key in self._ahead:
            # FILL of an ahead-set order: no virtual fill (T07). The book's following
            # CANCEL reduces the quantity; we recount next time (count once, T06).
            return []
        # Behind-us order filled. Quantity ahead excluding the filled order == ahead_qty
        # (the key is not in the ahead set, so it was not counted).
        if ahead_qty == 0:
            evidence = ExecutionEvidence(
                group_id=f"{self._task_id}:source-group:{allocation.trade.source_ordinal}",
                event_time_ns=record.event_time_ns,
                source_ordinal=record.source_ordinal,
                mechanism=FillMechanism.QUEUE_DEPLETION,
                granted_fill=True,
                ambiguity=AmbiguityKind.NONE,
                quantity_ahead_before=0,
                our_side=side,
                our_limit_price_fixed=self._limit,
                displaced_allocation=key,
                detail="behind-order fill with zero quantity ahead",
                source_group_ordinal=allocation.trade.source_ordinal,
                eligible_volume=record.quantity,
            )
            self._ledger.record(evidence)
            return [
                self._emit_fill(
                    command_id=command_id,
                    price_fixed=self._limit,
                    liquidity=Liquidity.PASSIVE,
                    mechanism=FillMechanism.QUEUE_DEPLETION,
                    exchange_time_ns=record.event_time_ns,
                )
            ]
        # ahead_qty > 0: inconsistent with FIFO -> AMBIGUOUS, no fill (AMBIGUITY_FILL).
        evidence = ExecutionEvidence(
            group_id=f"{self._task_id}:source-group:{allocation.trade.source_ordinal}",
            event_time_ns=record.event_time_ns,
            source_ordinal=record.source_ordinal,
            mechanism=FillMechanism.AMBIGUOUS,
            granted_fill=AMBIGUITY_FILL,
            ambiguity=AmbiguityKind.BEHIND_FILL_WHILE_AHEAD_POSITIVE,
            quantity_ahead_before=ahead_qty,
            our_side=side,
            our_limit_price_fixed=self._limit,
            displaced_allocation=key,
            detail="behind-order fill while quantity ahead still positive",
            source_group_ordinal=allocation.trade.source_ordinal,
            eligible_volume=record.quantity,
        )
        self._ledger.record(evidence)
        if AMBIGUITY_FILL:  # pragma: no cover - frozen False
            return [
                self._emit_fill(
                    command_id=command_id,
                    price_fixed=self._limit,
                    liquidity=Liquidity.PASSIVE,
                    mechanism=FillMechanism.AMBIGUOUS,
                    exchange_time_ns=record.event_time_ns,
                )
            ]
        return []

    def _on_trade_record(
        self,
        record: CanonicalRecord,
        book_before: ReferenceBook,
        command_id: str,
        allocation: SupportedAllocation,
    ) -> list[ExchangeReport]:
        assert self._side is not None and self._limit is not None
        # Aggressor must be the opposing side and the print must be strictly through limit.
        opposing = self._book_side().opposite()
        if allocation.trade.side is not opposing:
            return []
        if self._side is TaskSide.BUY:
            through = record.price_fixed < self._limit
        else:
            through = record.price_fixed > self._limit
        if not through:
            return []
        ahead_qty = self._prune_and_sum_ahead(book_before)
        ambiguous = ahead_qty > 0
        evidence = ExecutionEvidence(
            group_id=f"{self._task_id}:source-group:{allocation.trade.source_ordinal}",
            event_time_ns=record.event_time_ns,
            source_ordinal=record.source_ordinal,
            mechanism=FillMechanism.AMBIGUOUS if ambiguous else FillMechanism.TRADE_THROUGH,
            granted_fill=not ambiguous,
            ambiguity=(
                AmbiguityKind.TRADE_THROUGH_WHILE_AHEAD_POSITIVE
                if ambiguous
                else AmbiguityKind.NONE
            ),
            quantity_ahead_before=ahead_qty,
            our_side=self._book_side(),
            our_limit_price_fixed=self._limit,
            displaced_allocation=record.order_key(),
            trade_price_fixed=record.price_fixed,
            detail="linked opposing allocation through our limit",
            source_group_ordinal=allocation.trade.source_ordinal,
            eligible_volume=record.quantity,
        )
        self._ledger.record(evidence)
        if ambiguous:
            return []
        return [
            self._emit_fill(
                command_id=command_id,
                price_fixed=self._limit,
                liquidity=Liquidity.PASSIVE,
                mechanism=FillMechanism.TRADE_THROUGH,
                exchange_time_ns=record.event_time_ns,
            )
        ]

    def on_batch_committed(
        self, batch: EventBatch, book_after: ReferenceBook
    ) -> list[ExchangeReport]:
        """Observe a committed batch; an INITIALIZATION (snapshot) batch while working is a
        loss of queue continuity -> technical reset (T37)."""
        if self._state is not _State.WORKING:
            return []
        if batch.kind is BatchKind.INITIALIZATION:
            command_id = self._passive_command_id or ""
            return [self._technical_reset(command_id, batch.exchange_proxy_time)]
        # Refresh the audit queue-ahead against the now-committed book so true_queue_ahead()
        # reflects cancels/modifies/adds that did not trigger a fill-path recompute. Pruning
        # here is permanent and matches what the next eligible-evidence recompute would see.
        self._prune_and_sum_ahead(book_after)
        return []

    # -- report builders -------------------------------------------------------------

    def _reject(self, command_id: str, t: int, reason: str) -> ExchangeReport:
        return ExchangeReport(
            report_id=self._next_report_id(),
            command_id=command_id,
            task_id=self._task_id,
            kind=ReportKind.REJECTED,
            exchange_time_ns=t,
            cumulative_executed=self._executed,
            execution=None,
            reason=reason,
        )

    def _aggressive_unfilled(self, command_id: str, t: int, reason: str) -> ExchangeReport:
        return ExchangeReport(
            report_id=self._next_report_id(),
            command_id=command_id,
            task_id=self._task_id,
            kind=ReportKind.AGGRESSIVE_UNFILLED,
            exchange_time_ns=t,
            cumulative_executed=self._executed,
            execution=None,
            reason=reason,
        )

    def _technical_reset(self, command_id: str, t: int) -> ExchangeReport:
        self._state = _State.TERMINAL
        self._technically_invalid = True
        self._ahead.clear()
        return ExchangeReport(
            report_id=self._next_report_id(),
            command_id=command_id,
            task_id=self._task_id,
            kind=ReportKind.TECHNICAL_RESET,
            exchange_time_ns=t,
            cumulative_executed=self._executed,
            execution=None,
            reason="TECHNICAL_RESET",
        )

    # -- copy ------------------------------------------------------------------------

    def copy(self) -> ExchangeOverlay:
        """Independent copy for checkpoint/restore (World.clone, T41)."""
        clone = ExchangeOverlay(self._task_id, self._instrument, self._fee)
        clone._state = self._state
        clone._side = self._side
        clone._limit = self._limit
        clone._passive_command_id = self._passive_command_id
        clone._ahead = {k: _AheadMember(v.priority_seq) for k, v in self._ahead.items()}
        clone._executed = self._executed
        clone._technically_invalid = self._technically_invalid
        clone._last_queue_ahead = self._last_queue_ahead
        clone._report_counter = self._report_counter
        clone._execution_counter = self._execution_counter
        clone._ledger = self._ledger.copy()
        return clone
