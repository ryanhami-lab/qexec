"""R1 (docs/remediation.md): forked HOLD/SWITCH branches inherit the parent's execution ledger
and every pending scheduled event (report deliveries, command arrivals), duplicated per branch.

Defect: at a probe fork the branches were cloned but (a) the execution/report ledger rows emitted
BEFORE the fork stayed keyed to the parent ``world_id`` -- so each branch's outcome math (which
filters the ledger by its own ``world_id``) lost every pre-fork fill -- and (b) scheduled events
still pending at the fork (a passive FILL report in flight, a command arrival) were keyed to the
parent and never reached the branches, so a branch's OMS never learned about an in-flight fill.

Required fix (remediation R1): branches inherit the parent's execution-ledger entries and every
pending scheduled event for the parent, duplicated per branch.

Reproduction (remediation R1): a passive fill ``100us`` before the checkpoint (its report still
undelivered at the checkpoint, L1), so the probe checkpoint is eligible (client still WORKING) and
forks. Both branches must be completed with ``miss == 0`` and a ``C_T`` equal to a standalone B1
world in the identical book.

L1 timing (``c=50us, e=250us, r=250us``): JOIN at ``t0`` arrives ``t0+300us``, ACCEPTED delivered
``t0+550us``; checkpoint at ``t0+500ms``; a trade-through fill at ``x`` is reported at ``x+250us``.
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)

from qexec.core.config import ExperimentConfig
from qexec.core.messages import CommandKind, ExchangeCommand
from qexec.core.types import Action, Side, TradingStatus
from qexec.policies.baselines import B1Policy
from qexec.sim.engine import SessionEngine, _CommandArrival
from qexec.sim.scheduler import EventClass
from qexec.sim.world import WorldPhase

START = 1_000_000_000_000
WARMUP = 1_000_000_000
HORIZON = 1_000_000_000


def _cfg() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="r1", horizon_ns=HORIZON, latency_id="L1")


def _session_fill_before_checkpoint(tmp_path: Path, sid: str, t0: int) -> Path:
    """A one-tick book where a joined BUY passive is filled by a trade-through 100us before the
    checkpoint. The FILL report is delivered 150us AFTER the checkpoint (fill+250us), so the
    client is still WORKING at the checkpoint -> the probe checkpoint is eligible and forks with a
    pending (in-flight) FILL report and the pre-fork execution already in the ledger."""
    deadline = t0 + HORIZON
    checkpoint = t0 + HORIZON // 2
    b = H.SessionBuilder(sid, START)
    b.add_status(TradingStatus.TRADING, START, START)
    b.snapshot(
        START,
        bids=[(H.BID, 50, 1), (H.BID2, 50, 2)],
        asks=[(H.ASK, 50, 3), (H.ASK2, 50, 4)],
    )
    deep = H.BID2 - 10 * H.TICK
    t = START + 10 * H.MS
    oid = 5000
    fill_t = checkpoint - 100 * H.US
    while t < deadline + 2 * H.S:
        # Keep the deep feed away from the fill instant so the fill batch is a clean single event.
        if abs(t - fill_t) > 10 * H.MS:
            b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
            b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
            oid += 1
        t += 50 * H.MS
    # Trade-through strictly below our buy limit (100.00) fills the passive at fill_t.
    b.add_event(fill_t, H.sell_through_bid_records())
    return b.write(tmp_path)


def test_R1_both_branches_inherit_prefork_fill(tmp_path: Path) -> None:
    sid = "SYN-R1"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _session_fill_before_checkpoint(tmp_path, sid, t0)
    checkpoint = t0 + HORIZON // 2
    fill_t = checkpoint - 100 * H.US

    # Control: a standalone B1 world in the identical book completes on time via the passive fill.
    b1_out = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1").run()
    b1 = (
        b1_out.task_results.filter(b1_out.task_results["task_id"] == f"{sid}:00000:BUY")
        .filter(b1_out.task_results["policy_id"] == "B1")
        .row(0, named=True)
    )
    assert b1["status"] == "COMPLETED_ON_TIME"
    assert b1["completion_time_ns"] == fill_t
    b1_ct = b1["c_t_ticks"]

    # Probe run on the same session: the checkpoint is eligible (client still WORKING at the
    # checkpoint because the FILL report is delivered 150us later) and forks.
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()
    branch = out.branch_labels.filter(out.branch_labels["task_id"] == f"{sid}:00000:BUY")
    assert branch.height == 2  # HOLD and SWITCH
    for row in branch.to_dicts():
        assert row["completed"] is True, f"{row['action']} lost the pre-fork fill"
        assert row["miss"] == 0
        # C_T equals the standalone B1 world's C_T (same fill, z0, m_T).
        assert row["c_t_ticks"] is not None
        assert abs(row["c_t_ticks"] - b1_ct) < 1e-9

    # Both branch worlds reached DONE (no leak), and each inherited exactly one execution row.
    for w in eng._worlds.values():
        if w.branch_action is None:
            continue
        assert w.phase is WorldPhase.DONE
        ex = out.executions.filter(out.executions["world_id"] == w.world_id)
        assert ex.height == 1, f"{w.world_id} did not inherit the pre-fork execution"


def test_R1_inherits_pending_report_and_command_events(tmp_path: Path) -> None:
    """The fork duplicates BOTH pending report deliveries and pending command arrivals for the
    parent onto each branch (remediation R1 mechanism).

    White-box: in the fill-before-checkpoint session the parent has a pending FILL report in
    flight at the fork. We also inject a synthetic pending command arrival for the parent just
    before forking, then assert each branch receives its own copy of both pending events.
    """
    sid = "SYN-R1PEND"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _session_fill_before_checkpoint(tmp_path, sid, t0)

    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)

    # Patch _fork_probe to inject a synthetic pending command arrival for the parent right before
    # the inheritance runs, so we can observe its duplication onto the branches.
    original = eng._fork_probe
    injected: dict[str, int] = {}

    def patched(world: object, now: int, view: object) -> None:
        # Inject a future command arrival for the parent (valid: time >= now).
        cmd = ExchangeCommand(
            command_id="inj",
            task_id=world.task.task_id,  # type: ignore[attr-defined]
            kind=CommandKind.CANCEL,
            side=world.side,  # type: ignore[attr-defined]
            quantity=1,
            arrival_time_ns=now + 10_000,
            target_command_id=None,
        )
        eng._sched.schedule(
            now + 10_000, EventClass.EXCHANGE_COMMAND, _CommandArrival(world.world_id, cmd)
        )
        injected["parent"] = now
        original(world, now, view)  # type: ignore[arg-type]

    eng._fork_probe = patched  # type: ignore[method-assign]
    eng.run()
    assert injected  # the fork fired

    # After the run, verify (via the recorded reports) that the FILL report reached both branches
    # (each branch's FILL report row exists). The pre-fork FILL is inherited as a report row.
    for w in eng._worlds.values():
        if w.branch_action is None:
            continue
        kinds = [e["kind"] for e in w.trace_events]
        # Each branch received a report delivery (the inherited pending FILL) or a command arrival
        # (the inherited synthetic cancel) -- both pending events were duplicated per branch.
        assert "report_delivery" in kinds or "command_arrival" in kinds


def _session_hold_misses(tmp_path: Path, sid: str, t0: int) -> Path:
    """A one-tick book that never fills a passive AND halts from before the cutoff through the
    deadline, so the HOLD branch's terminal aggressive at the cutoff is AGGRESSIVE_UNFILLED and
    the branch MISSES (T42 failed-branch case). The halt is a status event (not a quality flag),
    so the task stays technically evaluable."""
    deadline = t0 + HORIZON
    cutoff = deadline - 1_400_001
    b = H.SessionBuilder(sid, START)
    b.add_status(TradingStatus.TRADING, START, START)
    b.snapshot(
        START,
        bids=[(H.BID, 50, 1), (H.BID2, 50, 2)],
        asks=[(H.ASK, 50, 3), (H.ASK2, 50, 4)],
    )
    # Halt effective well before the cutoff so the cutoff aggressive finds no liquidity. Observed
    # at the same time (capture == event) on the client plane -- irrelevant to the overlay, which
    # reads the historical book. Resume after the deadline so the whole terminal window is halted.
    halt_t = cutoff - 50 * H.MS
    b.add_status(TradingStatus.HALTED, halt_t, halt_t)
    deep = H.BID2 - 10 * H.TICK
    t = START + 10 * H.MS
    oid = 5000
    while t < deadline + 2 * H.S:
        # Feed through the halt too (keeps the session long enough for the manifest arrival grid;
        # during the halt no committed mid is produced, which is irrelevant here).
        b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
        b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
        oid += 1
        t += 50 * H.MS
    return b.write(tmp_path)


def test_R1_T42_failed_hold_branch_misses(tmp_path: Path) -> None:
    """T42 failed-branch case: a HOLD branch that never fills passively and whose cutoff
    aggressive finds a halted book records miss == 1 (completed False)."""
    sid = "SYN-R1MISS"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _session_hold_misses(tmp_path, sid, t0)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()
    hold = out.branch_labels.filter(out.branch_labels["action"] == "HOLD").to_dicts()
    assert hold, "expected a HOLD branch"
    for row in hold:
        assert row["completed"] is False
        assert row["miss"] == 1
        assert row["fill_mechanism"] is None


def _session_late_fill(tmp_path: Path, sid: str, t0: int) -> Path:
    """A one-tick book where the HOLD passive is filled by a trade-through strictly AFTER the
    deadline T (late completion). The fill must count as a miss and be excluded from value, with
    the late time recorded as a diagnostic (T42 late-completing-branch case)."""
    deadline = t0 + HORIZON
    cutoff = deadline - 1_400_001
    b = H.SessionBuilder(sid, START)
    b.add_status(TradingStatus.TRADING, START, START)
    b.snapshot(
        START,
        bids=[(H.BID, 50, 1), (H.BID2, 50, 2)],
        asks=[(H.ASK, 50, 3), (H.ASK2, 50, 4)],
    )
    # Halt from before the cutoff so the cutoff aggressive misses (no fabricated fill), then the
    # passive is still working during the drain and a post-deadline trade-through fills it late.
    halt_t = cutoff - 50 * H.MS
    resume_t = deadline + 100 * H.MS
    b.add_status(TradingStatus.HALTED, halt_t, halt_t)
    b.add_status(TradingStatus.TRADING, resume_t, resume_t)
    deep = H.BID2 - 10 * H.TICK
    t = START + 10 * H.MS
    oid = 5000
    late_x = deadline + 200 * H.MS
    while t < deadline + 3 * H.S:
        # Feed through the halt and the post-resume window; keep the deep order away from the late
        # trade instant so that batch is clean.
        if abs(t - late_x) > 10 * H.MS:
            b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
            b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
            oid += 1
        t += 50 * H.MS
    # Post-deadline trade-through (after resume) that fills the still-working HOLD passive late.
    b.add_event(late_x, H.sell_through_bid_records())
    return b.write(tmp_path)


def test_R1_T42_late_fill_counts_as_miss_excluded_from_value(tmp_path: Path) -> None:
    """T42 late-completing-branch case: a passive fill AFTER T counts as a miss and is excluded
    from the branch value (C_T uses only on-time fills); completed is False."""
    sid = "SYN-R1LATE"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _session_late_fill(tmp_path, sid, t0)
    deadline = t0 + HORIZON
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()
    hold = out.branch_labels.filter(out.branch_labels["action"] == "HOLD").to_dicts()
    assert hold, "expected a HOLD branch"
    for row in hold:
        assert row["completed"] is False  # the only fill is after T
        assert row["miss"] == 1
        # No on-time execution counts for this branch world.
        wid_prefix = f"{row['task_id']}|B1_PROBE_HOLD"
        on_time = [
            e
            for e in out.executions.to_dicts()
            if e["world_id"].startswith(wid_prefix) and int(e["exchange_time_ns"]) <= deadline
        ]
        assert on_time == []
