"""R4/R8/R10/R11 (docs/remediation.md): output-schema and valuation fixes in the engine plane.

* R4  -- branch_labels carry ``technically_unevaluable`` + ``unevaluable_reason`` from the same
         policy-independent quality map as task_results.
* R8  -- ``checkpoint_eligible`` is persisted for every world at its checkpoint, independent of
         feature recording (so a non-B1 policy with ``record_checkpoint_features=False`` still
         reports eligible MODEL_CHOICE rows).
* R10 -- deadline valuation uses ``MidSeries.last_valid_mid_at(T)`` (not the possibly-invalid
         state at T); ``m_t_age_ns`` is persisted on task_results and branch_labels.
* R11 -- every SessionOutputs frame carries ``scenario_id`` and ``horizon_ns`` (engine half).

Reproductions use hand-built micro-sessions (L1).
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)

from qexec.core.config import ExperimentConfig
from qexec.core.tasks import CheckpointChoice, DecisionReason
from qexec.core.types import Action, Side, TradingStatus
from qexec.core.views import DecisionView
from qexec.policies.base import ArrivalAction
from qexec.policies.baselines import B1Policy
from qexec.sim.engine import SessionEngine

START = 1_000_000_000_000
WARMUP = 1_000_000_000
HORIZON = 1_000_000_000


def _cfg() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="rschema", horizon_ns=HORIZON, latency_id="L1")


class _StubModelPolicy:
    """A B3-style stub: joins at arrival like B1 and HOLDs at an eligible checkpoint. Its
    non-B1 ``policy_id`` means the engine does NOT record checkpoint features for it, so R8's
    persisted eligibility (not the feature capture) must drive ``checkpoint_eligible``."""

    policy_id: str = "B3"

    def on_arrival(self, view: DecisionView) -> ArrivalAction:
        del view
        return "JOIN_BEST"

    def on_checkpoint(self, view: DecisionView) -> tuple[CheckpointChoice, DecisionReason]:
        del view
        return (CheckpointChoice.HOLD, DecisionReason.MODEL_CHOICE)


def _static_session(tmp_path: Path, sid: str, t0: int) -> Path:
    """A static one-tick book: a joined passive never fills (so it is WORKING -> eligible at the
    checkpoint) and completes at the cutoff aggressive."""
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
        t += 50 * H.MS
    return b.write(tmp_path)


def test_R8_checkpoint_eligible_persisted_without_feature_capture(tmp_path: Path) -> None:
    sid = "SYN-R8"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _static_session(tmp_path, sid, t0)
    eng = SessionEngine(
        sd, _cfg(), [_StubModelPolicy()], scenario_id="L1", record_checkpoint_features=False
    )
    out = eng.run()
    # No checkpoint-feature rows were recorded (non-B1 policy, recording disabled).
    assert out.checkpoint_features.height == 0
    # Yet the MODEL_CHOICE worlds are marked eligible in task_results (persisted on the world).
    b3 = out.task_results.filter(out.task_results["policy_id"] == "B3").to_dicts()
    assert b3
    eligible = [r for r in b3 if r["checkpoint_eligible"]]
    assert eligible, "a MODEL_CHOICE world must be marked checkpoint_eligible without features"


def test_R11_all_frames_have_scenario_and_horizon(tmp_path: Path) -> None:
    sid = "SYN-R11"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _static_session(tmp_path, sid, t0)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()
    frames = {
        "task_results": out.task_results,
        "decisions": out.decisions,
        "executions": out.executions,
        "reports": out.reports,
        "checkpoint_features": out.checkpoint_features,
        "branch_labels": out.branch_labels,
        "support": out.support,
        "quality": out.quality,
    }
    for name, frame in frames.items():
        assert frame.height > 0, f"{name} is empty; cannot assert its scenario/horizon columns"
        assert "scenario_id" in frame.columns, f"{name} missing scenario_id"
        assert "horizon_ns" in frame.columns, f"{name} missing horizon_ns"
        assert set(frame["scenario_id"].to_list()) == {"L1"}
        assert set(frame["horizon_ns"].to_list()) == {HORIZON}


def test_R10_task_result_mt_uses_last_valid_mid_and_records_age(tmp_path: Path) -> None:
    """m_T is the last VALID committed historical mid at or before T; its age is recorded. We halt
    the historical book before the deadline so the state at T is invalid; m_T must fall back to
    the last valid mid before the halt, with a positive age (not None, not dropped)."""
    sid = "SYN-R10"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    deadline = t0 + HORIZON
    b = H.SessionBuilder(sid, START)
    b.add_status(TradingStatus.TRADING, START, START)
    b.snapshot(
        START,
        bids=[(H.BID, 50, 1), (H.BID2, 50, 2)],
        asks=[(H.ASK, 50, 3), (H.ASK2, 50, 4)],
    )
    # Halt the book 300ms before the deadline (state at T invalid) but keep feeding so the session
    # is long enough; resume after the deadline. The last valid committed mid is just before the
    # halt, so m_T is that mid and its age ~ (T - last_valid_sample_time) > 0.
    halt_t = deadline - 300 * H.MS
    resume_t = deadline + 500 * H.MS
    b.add_status(TradingStatus.HALTED, halt_t, halt_t)
    b.add_status(TradingStatus.TRADING, resume_t, resume_t)
    deep = H.BID2 - 10 * H.TICK
    t = START + 10 * H.MS
    oid = 5000
    while t < deadline + 2 * H.S:
        b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
        b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
        oid += 1
        t += 50 * H.MS
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1").run()
    rows = [
        r
        for r in eng.task_results.to_dicts()
        if r["policy_id"] == "B1" and r["status"] != "TECHNICALLY_UNEVALUABLE"
    ]
    assert rows
    for r in rows:
        # c_t is computed from a valid m_T (not dropped despite the invalid state at T).
        assert r["c_t_ticks"] is not None
        assert r["m_t_age_ns"] is not None
        # The last valid mid predates the deadline by at least the 300ms halt lead-in.
        assert r["m_t_age_ns"] >= 300 * H.MS


def _clear_in_window_session(tmp_path: Path, sid: str, t0: int) -> Path:
    """A static book with a CLEAR inside the task window (after the passive is working), so the
    policy-independent quality map marks the task TECHNICALLY_UNEVALUABLE -- and R4 must flag both
    branch rows."""
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
    clear_t = t0 + HORIZON // 2 + 50 * H.MS  # after the checkpoint, inside the window
    while t < deadline + 2 * H.S:
        if abs(t - clear_t) > 10 * H.MS:
            b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
            b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
            oid += 1
        t += 50 * H.MS
    b.add_event(clear_t, [H.RecordSpec(Action.CLEAR, Side.NONE, 0, 0, 0)])
    b.snapshot(
        clear_t + H.US,
        bids=[(H.BID, 50, 9001), (H.BID2, 50, 9002)],
        asks=[(H.ASK, 50, 9003), (H.ASK2, 50, 9004)],
    )
    return b.write(tmp_path)


def test_R4_branch_rows_carry_quality_flag_and_reason(tmp_path: Path) -> None:
    """A CLEAR in the window makes the task unevaluable; both branch rows are flagged with the
    quality map's reason (R4). The fork fires at the checkpoint before the CLEAR."""
    sid = "SYN-R4"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _clear_in_window_session(tmp_path, sid, t0)
    out = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1", fork_label_probe=True).run()
    # The quality map marks the BUY task unevaluable (CLEAR in window) -- confirm via the frame.
    q = out.quality.filter(out.quality["task_id"] == f"{sid}:00000:BUY").row(0, named=True)
    assert q["technically_unevaluable"] is True

    branch = out.branch_labels.filter(out.branch_labels["task_id"] == f"{sid}:00000:BUY").to_dicts()
    assert branch, "expected branch rows (fork fired at the checkpoint before the CLEAR)"
    for row in branch:
        assert row["technically_unevaluable"] is True
        assert row["unevaluable_reason"] is not None
        assert row["unevaluable_reason"] == q["reason"]
