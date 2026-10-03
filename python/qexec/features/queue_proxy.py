"""Observable own-order queue cohort proxy (architecture section 7.1).

At passive command creation the policy freezes, from the delivered **client** book, the set of
same-side same-price resting orders and their quantities -- the *cohort* assumed to sit ahead of
the hypothetical order. The proxy then tracks this cohort using only subsequently delivered
information:

* A cohort member's contribution is its *current* client-book quantity while it is still at the
  limit price **with retained priority**. Book reductions (partial cancels, execution-driven
  reductions) are reflected by the client book itself, so each reduction is counted exactly
  once. A member that leaves the limit price, is fully removed, or loses priority (a
  priority-losing ``MODIFY`` -- price change or size increase, detected as a change in the
  order's ``priority_seq``) is dropped from the cohort permanently.
* ADDs delivered at the limit price whose batch ``exchange_proxy_time`` lies in the modeled
  entry interval ``(last_delivered_proxy_ns, expected_arrival_ns]`` are **uncertain**: they may
  have reached the exchange before the hypothetical order and so may sit ahead of it. Adds whose
  batch proxy time is after ``expected_arrival_ns`` are definitely behind and are ignored.
  Uncertain adds are tracked by key and reduced by delivered book reductions exactly like the
  cohort.

This is a deterministic observable proxy, not a guaranteed rank or confidence bound; it never
inspects any overlay/simulator state (true queue position, unreported executions). It differs
from the simulator truth precisely when an add existed in the true book before the hypothetical
order's arrival but had not yet been delivered to the client at decision time (test T54).

Features (all finite floats)
----------------------------
* ``q_ahead_est``          -- current cohort quantity (the point estimate of quantity ahead).
* ``q_ahead_upper``        -- cohort quantity plus uncertain-add quantity (upper band).
* ``q_insertion_uncertain``-- current uncertain-add quantity.
* ``q_depleted``           -- initial cohort quantity minus current cohort quantity.
* ``q_depleted_frac``      -- ``q_depleted / initial_cohort_qty`` (``0.0`` if the initial
  cohort was empty).
"""

from __future__ import annotations

import math

from qexec.core.types import Action, BatchKind, EventBatch, OrderKey, TaskSide
from qexec.reference.book import ReferenceBook


class QueueCohortProxy:
    """Deterministic observable cohort proxy over delivered client-book information only."""

    def __init__(  # noqa: PLR0917 - signature is fixed by the engineering contract (WP-FEATURES)
        self,
        side: TaskSide,
        limit_price_fixed: int,
        client_book_at_decision: ReferenceBook,
        decision_time_ns: int,
        expected_arrival_ns: int,
        last_delivered_proxy_ns: int,
    ) -> None:
        self._side = side
        self._book_side = side.book_side
        self._limit_price_fixed = limit_price_fixed
        self._decision_time_ns = decision_time_ns
        self._expected_arrival_ns = expected_arrival_ns
        self._last_delivered_proxy_ns = last_delivered_proxy_ns

        # Freeze the cohort: same-side same-price resting orders from the CLIENT book, keyed by
        # OrderKey, remembering the priority_seq so a later priority-losing MODIFY is detectable.
        self._cohort_seq: dict[OrderKey, int] = {}
        initial_qty = 0
        for order in client_book_at_decision.orders_at(self._book_side, limit_price_fixed):
            self._cohort_seq[order.key] = order.priority_seq
            initial_qty += order.quantity
        self._initial_cohort_qty = initial_qty

        # Uncertain adds: keys discovered in the entry interval, with their frozen priority_seq.
        self._uncertain_seq: dict[OrderKey, int] = {}

    # -- ingestion -------------------------------------------------------------------

    def on_delivered(self, batch: EventBatch, client_book_after: ReferenceBook) -> None:
        """Consume one delivered ``batch`` committed into ``client_book_after``.

        Membership (cohort and uncertain) is pruned against ``client_book_after``; new uncertain
        adds are discovered when the batch proxy time falls in the entry interval.
        """
        if batch.kind is BatchKind.INITIALIZATION:
            # A snapshot rebuild is not economic order flow; it cannot add cohort members, and
            # membership is re-validated below against the committed book.
            self._prune(client_book_after)
            return

        proxy_time = int(batch.exchange_proxy_time)
        in_entry_interval = self._last_delivered_proxy_ns < proxy_time <= self._expected_arrival_ns
        if in_entry_interval:
            for rec in batch.records:
                if (
                    rec.action is Action.ADD
                    and rec.side is self._book_side
                    and rec.price_fixed == self._limit_price_fixed
                ):
                    key = rec.order_key()
                    # Only genuinely new keys (not already cohort members) become uncertain.
                    if key not in self._cohort_seq and key not in self._uncertain_seq:
                        order = client_book_after.get_order(key)
                        if order is not None:
                            self._uncertain_seq[key] = order.priority_seq

        self._prune(client_book_after)

    def _prune(self, book: ReferenceBook) -> None:
        """Drop cohort/uncertain members that left the limit, vanished, or lost priority."""
        for registry in (self._cohort_seq, self._uncertain_seq):
            for key in list(registry):
                order = book.get_order(key)
                if (
                    order is None
                    or order.price_fixed != self._limit_price_fixed
                    or order.side is not self._book_side
                    or order.priority_seq != registry[key]
                ):
                    del registry[key]

    # -- features --------------------------------------------------------------------

    @staticmethod
    def _registry_qty(registry: dict[OrderKey, int], book: ReferenceBook) -> int:
        total = 0
        for key in registry:
            order = book.get_order(key)
            if order is not None:
                total += order.quantity
        return total

    def features(self, client_book: ReferenceBook) -> dict[str, float]:
        """Compute the queue-proxy feature dict from the current ``client_book``."""
        cohort_qty = self._registry_qty(self._cohort_seq, client_book)
        uncertain_qty = self._registry_qty(self._uncertain_seq, client_book)
        q_depleted = self._initial_cohort_qty - cohort_qty
        q_depleted_frac = (
            q_depleted / self._initial_cohort_qty if self._initial_cohort_qty > 0 else 0.0
        )
        out = {
            "q_ahead_est": float(cohort_qty),
            "q_ahead_upper": float(cohort_qty + uncertain_qty),
            "q_insertion_uncertain": float(uncertain_qty),
            "q_depleted": float(q_depleted),
            "q_depleted_frac": float(q_depleted_frac),
        }
        assert all(math.isfinite(v) for v in out.values())
        return out

    # -- copy ------------------------------------------------------------------------

    def copy(self) -> QueueCohortProxy:
        """Return a deep, independent copy; mutating the copy never touches the original."""
        clone = QueueCohortProxy.__new__(QueueCohortProxy)
        clone._side = self._side
        clone._book_side = self._book_side
        clone._limit_price_fixed = self._limit_price_fixed
        clone._decision_time_ns = self._decision_time_ns
        clone._expected_arrival_ns = self._expected_arrival_ns
        clone._last_delivered_proxy_ns = self._last_delivered_proxy_ns
        clone._cohort_seq = dict(self._cohort_seq)
        clone._initial_cohort_qty = self._initial_cohort_qty
        clone._uncertain_seq = dict(self._uncertain_seq)
        return clone
