"""Deterministic event scheduler for the session replay engine (architecture section 6.3).

Every event is keyed ``(time_ns, event_class, seq)`` and popped in that order:

* ``time_ns`` -- the integer-nanosecond effective time of the event.
* ``event_class`` -- the within-timestamp priority declared by the contract (section 2.2 /
  architecture 6.3):

    0. historical batch commit,
    1. exchange command arrival,
    2. client delivery (market observation or status observation or report),
    3. timers (arrival, checkpoint, cutoff, deadline) and policy decisions.

* ``seq`` -- a global monotone counter assigned **when the event is scheduled**. This is the
  *causal microstep*: a zero-delay descendant scheduled while handling an earlier event is
  given a strictly larger ``seq`` than its cause, so it is always ordered after it even at an
  identical ``(time_ns, event_class)``.

The scheduler itself is agnostic to payload type: it stores an opaque ``payload`` object and
returns it to the engine, which owns all event semantics. A binary heap over the
``(time_ns, event_class, seq)`` tuple gives ``O(log n)`` push/pop with a fully deterministic
total order (``seq`` breaks every tie, and ``seq`` values are unique).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from enum import IntEnum


class EventClass(IntEnum):
    """Within-timestamp ordering classes (lower runs first)."""

    HISTORICAL_BATCH = 0
    EXCHANGE_COMMAND = 1
    CLIENT_DELIVERY = 2
    TIMER = 3


@dataclass(frozen=True, slots=True, order=True)
class ScheduledEvent[P]:
    """One scheduled event. Ordering is by ``(time_ns, event_class, seq)`` only."""

    time_ns: int
    event_class: int
    seq: int
    payload: P = field(compare=False)


class Scheduler[P]:
    """A deterministic min-heap priority queue keyed ``(time_ns, event_class, seq)``."""

    def __init__(self) -> None:
        self._heap: list[ScheduledEvent[P]] = []
        self._seq: int = 0
        # The time of the most recently popped event; the engine's logical "now". No event may
        # be scheduled strictly before it (contract: effective times are causal and never move
        # backwards -- see R2 in docs/remediation.md). ``-1`` before the first pop admits any
        # nonnegative schedule time done during setup.
        self._current_time_ns: int = -1

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    @property
    def next_seq(self) -> int:
        """The seq that the next :meth:`schedule` call will assign (read-only, for tests)."""
        return self._seq

    @property
    def current_time_ns(self) -> int:
        """Time of the most recently popped event (the engine's logical ``now``).

        ``-1`` before the first :meth:`pop`. :meth:`schedule` refuses any ``time_ns`` strictly
        less than this value so no event is ever scheduled into the past (engine spec section 2;
        remediation R2).
        """
        return self._current_time_ns

    def schedule(self, time_ns: int, event_class: EventClass, payload: P) -> int:
        """Schedule ``payload`` at ``time_ns`` in ``event_class``; return its assigned seq.

        The returned ``seq`` is strictly larger than every previously assigned one, so an
        event scheduled later (a causal descendant) always sorts after an earlier one at an
        equal ``(time_ns, event_class)``.

        Raises :class:`ValueError` if ``time_ns`` is strictly before the current logical time
        (the time of the most recently popped event). A zero-delay descendant at exactly the
        current time is allowed (ordered after its cause by ``seq``); only a strictly earlier
        time -- which would require processing an event in the past -- is rejected (R2).
        """
        if time_ns < self._current_time_ns:
            raise ValueError(
                f"cannot schedule an event at {time_ns} ns before the current time "
                f"{self._current_time_ns} ns (would process an event in the past)"
            )
        seq = self._seq
        self._seq += 1
        heapq.heappush(self._heap, ScheduledEvent(time_ns, int(event_class), seq, payload))
        return seq

    def pop(self) -> ScheduledEvent[P]:
        """Pop the earliest event and advance the logical time. Raises on an empty heap."""
        event = heapq.heappop(self._heap)
        self._current_time_ns = event.time_ns
        return event

    def peek_time(self) -> int | None:
        """Return the time of the earliest event without popping, or ``None`` if empty."""
        return self._heap[0].time_ns if self._heap else None

    def pending_events(self) -> tuple[ScheduledEvent[P], ...]:
        """Return a snapshot of all currently queued (not-yet-popped) events.

        Read-only: the returned tuple does not alias the heap, and mutating it does not affect the
        scheduler. Used by the engine to duplicate a parent world's pending report deliveries and
        command arrivals onto its forked branches (engine spec section 8; remediation R1).
        """
        return tuple(self._heap)
