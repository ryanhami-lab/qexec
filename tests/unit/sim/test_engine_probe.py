"""Label-probe forking tests: clone identity/independence, branch labels (engine spec 8).

Covers T41 (clone identical before the fork, branches independent), T42 (the engine forks a
B1_PROBE world at its eligible checkpoint), and T57 (a branch's deadline-horizon value equals a
recomputation from that branch's executions with ``analysis.metrics.deadline_value_ticks``).
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)

from qexec.analysis.metrics import deadline_value_ticks
from qexec.core.config import ExperimentConfig
from qexec.core.messages import ExchangeReport, ReportKind
from qexec.core.tasks import Execution, FillMechanism, Liquidity, Task
from qexec.core.types import Action, Side, TaskSide, TimeNs, TradingStatus
from qexec.policies.baselines import B1Policy
from qexec.sim.controller import TerminalController
from qexec.sim.engine import SessionEngine
from qexec.sim.oms import ClientOMS
from qexec.sim.overlay import ExchangeOverlay
from qexec.sim.world import World, WorldPhase

START = 1_000_000_000_000
WARMUP = 1_000_000_000
HORIZON = 1_000_000_000


def _cfg() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="p", horizon_ns=HORIZON, latency_id="L1")


def _make_world(wid: str = "W") -> World:
    inst = H.instrument()
    task = Task(
        task_id="T",
        session_id="S",
        instrument_id=1,
        side=TaskSide.BUY,
        quantity=1,
        arrival_time_ns=TimeNs(START),
        deadline_ns=TimeNs(START + HORIZON),
        arrival_reference_mid2=H.BID + H.ASK,
        eligibility_rule_version="v1",
        task_manifest_id="mid",
    )
    return World(
        world_id=wid,
        task=task,
        side=TaskSide.BUY,
        policy=B1Policy(),
        overlay=ExchangeOverlay("T", inst, 0),
        oms=ClientOMS("T"),
        controller=TerminalController("T"),
        checkpoint_ns=START + HORIZON // 2,
        cutoff_ns=START + HORIZON - 1_400_001,
        deadline_ns=START + HORIZON,
        phase=WorldPhase.LIVE,
    )


def test_T41_clone_identical_before_fork() -> None:
    """A clone has identical observable state to its parent at the fork."""
    w = _make_world()
    w.oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    w.oms.apply(
        ExchangeReport(
            "r0",
            "c0",
            "T",
            ReportKind.ACCEPTED,
            exchange_time_ns=START + 300_000,
            cumulative_executed=0,
        )
    )
    w.own_limit_price_fixed = H.BID
    clone = w.clone("W2")
    assert clone.oms.state is w.oms.state
    assert clone.oms.reported_executed == w.oms.reported_executed
    assert clone.own_limit_price_fixed == w.own_limit_price_fixed
    assert clone.overlay.is_working == w.overlay.is_working
    assert clone.controller.aggressive_used == w.controller.aggressive_used


def test_T41_branches_independent() -> None:
    """Mutating one clone's OMS/controller does not affect the other (independent state)."""
    w = _make_world()
    w.oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    hold = w.clone("HOLD")
    switch = w.clone("SWITCH")
    # Resolve HOLD with a fill; SWITCH must be unaffected.
    ex = Execution(
        "e",
        "T",
        1,
        H.BID,
        0,
        TimeNs(START + 400_000),
        Liquidity.PASSIVE,
        FillMechanism.QUEUE_DEPLETION,
    )
    hold.oms.apply(
        ExchangeReport(
            "r1",
            "c0",
            "T",
            ReportKind.FILL,
            exchange_time_ns=START + 400_000,
            cumulative_executed=1,
            execution=ex,
        )
    )
    assert hold.oms.reported_executed == 1
    assert switch.oms.reported_executed == 0
    # Enter the switch controller; HOLD controller stays inactive.
    switch.controller.enter(switch.oms.state, switch.oms.reported_executed)
    assert switch.controller.active
    assert not hold.controller.active


def test_T41_two_clones_identical_at_fork() -> None:
    """Both branches have identical state at the fork (compare the two clones directly)."""
    w = _make_world()
    w.oms.reserve("c0", "PASSIVE_LIMIT", is_passive=True)
    w.oms.apply(
        ExchangeReport(
            "r0",
            "c0",
            "T",
            ReportKind.ACCEPTED,
            exchange_time_ns=START + 300_000,
            cumulative_executed=0,
        )
    )
    w.own_limit_price_fixed = H.BID
    hold = w.clone("HOLD")
    switch = w.clone("SWITCH")
    assert hold.oms.state is switch.oms.state
    assert hold.oms.reported_executed == switch.oms.reported_executed
    assert hold.oms.reserved == switch.oms.reserved
    assert hold.overlay.is_working == switch.overlay.is_working
    assert hold.overlay.executed_quantity == switch.overlay.executed_quantity
    assert hold.own_limit_price_fixed == switch.own_limit_price_fixed
    assert hold.controller.aggressive_used == switch.controller.aggressive_used


def _static_session(tmp_path: Path, sid: str) -> Path:
    """A static one-tick book that never fills, so a joined passive stays working through the
    checkpoint (making the probe checkpoint eligible and triggering a fork)."""
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    deadline = t0 + HORIZON
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
    while t < deadline + 2 * H.S:
        b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
        b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
        oid += 1
        t += 100 * H.MS
    return b.write(tmp_path)


def test_T42_engine_forks_probe_at_eligible_checkpoint(tmp_path: Path) -> None:
    sid = "SYN-T42"
    sd = _static_session(tmp_path, sid)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()
    # Each task that reaches an eligible probe checkpoint yields two branch rows (HOLD, SWITCH).
    assert out.branch_labels.height > 0
    actions = set(out.branch_labels["action"].to_list())
    assert actions == {"HOLD", "SWITCH"}
    # Each (task, side) with branches has exactly one HOLD and one SWITCH.
    by_task = out.branch_labels.group_by(["task_id", "side"]).agg(
        [__import__("polars").col("action").n_unique().alias("n")]
    )
    assert by_task["n"].min() == 2


def test_T57_branch_value_equals_recomputation(tmp_path: Path) -> None:
    """T57: a branch's stored c_t_ticks equals a recomputation from that branch's executions
    with ``deadline_value_ticks`` (same z0, m_T, fees stored on the branch row)."""
    sid = "SYN-T57"
    sd = _static_session(tmp_path, sid)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()
    assert out.branch_labels.height > 0
    tick = H.TICK
    mult = 50
    exec_rows = out.executions.to_dicts()
    checked = 0
    for row in out.branch_labels.to_dicts():
        if row["c_t_ticks"] is None:
            continue
        action = row["action"]
        wid_prefix = f"{row['task_id']}|B1_PROBE_{action}"
        # Deadline = checkpoint_time + H/2 (checkpoint at arrival + H/2, deadline at arrival + H).
        T = row["checkpoint_time_ns"] + (HORIZON // 2)
        fills = [
            (int(e["quantity"]), int(e["price_fixed"]))
            for e in exec_rows
            if e["world_id"].startswith(wid_prefix) and int(e["exchange_time_ns"]) <= T
        ]
        recomputed = deadline_value_ticks(
            int(row["side"]),
            fills,
            int(row["fees_fixed"]),
            int(row["z0_mid2"]),
            int(row["m_t_mid2"]),
            tick,
            mult,
        )
        assert abs(recomputed - row["c_t_ticks"]) < 1e-9
        checked += 1
    assert checked > 0, "expected at least one branch with a computed C_T"


def test_hold_branch_completes_via_cutoff_aggressive(tmp_path: Path) -> None:
    """Regression (reviewer Fix #1): a HOLD branch that never fills passively must still be
    entered at its cutoff, send the terminal aggressive, and complete before T.

    In the static one-tick book a joined passive never fills, so the only path to completion is
    the terminal aggressive at the cutoff T - G. Before the fix the fork never scheduled the
    branches' cutoff/deadline timers, so a HOLD branch leaked as LIVE and recorded completed=False
    (miss=1). It must instead enter the controller at the cutoff and complete, exactly like a
    standalone B1 world does in the same book (the reviewer's check_5b control)."""
    sid = "SYN-HOLD-CUT"
    sd = _static_session(tmp_path, sid)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()

    hold_rows = out.branch_labels.filter(out.branch_labels["action"] == "HOLD").to_dicts()
    assert hold_rows, "expected at least one HOLD branch"
    for row in hold_rows:
        # The aggressive at the cutoff takes the resting opposite side, so HOLD completes on time.
        assert row["completed"] is True
        assert row["miss"] == 0
        assert row["fill_mechanism"] is not None

    # Every HOLD branch world entered the controller at its cutoff, froze at T, and reached DONE.
    hold_worlds = [w for w in eng._worlds.values() if w.branch_action == "HOLD"]
    assert hold_worlds
    for w in hold_worlds:
        assert w.phase is WorldPhase.DONE
        kinds = [e["kind"] for e in out.trace(w.world_id, w.policy.policy_id)]
        assert "cutoff" in kinds
        assert "controller_enter" in kinds
        assert "deadline_freeze" in kinds


def test_switch_branch_not_left_live(tmp_path: Path) -> None:
    """Regression (reviewer Fix #1): a SWITCH branch must also receive a deadline timer and
    reach DONE rather than leaking as LIVE after it resolves."""
    sid = "SYN-SWITCH-DONE"
    sd = _static_session(tmp_path, sid)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()
    switch_worlds = [w for w in eng._worlds.values() if w.branch_action == "SWITCH"]
    assert switch_worlds
    for w in switch_worlds:
        assert w.phase is WorldPhase.DONE
        kinds = [e["kind"] for e in out.trace(w.world_id, w.policy.policy_id)]
        # SWITCH enters the controller at the fork (not the cutoff) and freezes at T.
        assert "controller_enter" in kinds
        assert "deadline_freeze" in kinds


def test_probe_hold_matches_standalone_b1_in_same_book(tmp_path: Path) -> None:
    """The HOLD branch label must match a standalone B1 world's outcome in the identical book
    (reviewer control check_5b): both complete on time via the cutoff aggressive."""
    sid = "SYN-HOLD-EQ"
    sd = _static_session(tmp_path, sid)
    # Standalone B1 (no probe): the control.
    b1_out = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1").run()
    b1_rows = b1_out.task_results.filter(b1_out.task_results["policy_id"] == "B1").to_dicts()
    assert b1_rows
    b1_completed_by_task = {r["task_id"]: r["status"] == "COMPLETED_ON_TIME" for r in b1_rows}
    assert any(b1_completed_by_task.values()), "control B1 must complete at cutoff in this book"

    # Probe run on the same session.
    probe_out = SessionEngine(
        sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True
    ).run()
    hold_rows = probe_out.branch_labels.filter(
        probe_out.branch_labels["action"] == "HOLD"
    ).to_dicts()
    assert hold_rows
    for row in hold_rows:
        assert row["completed"] == b1_completed_by_task.get(row["task_id"], False)
