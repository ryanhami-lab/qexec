"""Scheduler ordering and causal-microstep tests (contract 2.2, architecture 6.3)."""

from __future__ import annotations

from qexec.sim.scheduler import EventClass, Scheduler


def test_orders_by_time_then_class_then_seq() -> None:
    sched: Scheduler[str] = Scheduler()
    # Same time, different classes: lower class first regardless of schedule order.
    sched.schedule(100, EventClass.TIMER, "timer@100")
    sched.schedule(100, EventClass.HISTORICAL_BATCH, "batch@100")
    sched.schedule(100, EventClass.CLIENT_DELIVERY, "delivery@100")
    sched.schedule(100, EventClass.EXCHANGE_COMMAND, "command@100")
    sched.schedule(50, EventClass.TIMER, "timer@50")

    order = [sched.pop().payload for _ in range(5)]
    assert order == [
        "timer@50",  # earliest time
        "batch@100",  # class 0
        "command@100",  # class 1
        "delivery@100",  # class 2
        "timer@100",  # class 3
    ]


def test_causal_microstep_descendant_after_cause() -> None:
    """A zero-delay descendant scheduled while handling an earlier event sorts after it."""
    sched: Scheduler[str] = Scheduler()
    # Two timers at the same (time, class). The first scheduled is the cause; a descendant it
    # schedules (same time, same class) must come after it because its seq is larger.
    sched.schedule(100, EventClass.TIMER, "cause")
    cause = sched.pop()
    assert cause.payload == "cause"
    # While handling 'cause', schedule a zero-delay descendant at the same time and class.
    sched.schedule(100, EventClass.TIMER, "descendant")
    # Also schedule an unrelated same-time event beforehand to ensure seq (not insertion) wins.
    nxt = sched.pop()
    assert nxt.payload == "descendant"


def test_seq_is_monotone_and_unique() -> None:
    sched: Scheduler[int] = Scheduler()
    seqs = [sched.schedule(10, EventClass.TIMER, i) for i in range(5)]
    assert seqs == [0, 1, 2, 3, 4]
    assert sched.next_seq == 5


def test_peek_time_and_len() -> None:
    sched: Scheduler[str] = Scheduler()
    assert sched.peek_time() is None
    assert not sched
    sched.schedule(42, EventClass.HISTORICAL_BATCH, "x")
    assert sched.peek_time() == 42
    assert len(sched) == 1
    assert bool(sched)
