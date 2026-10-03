"""R7 (docs/remediation.md): the terminal cutoff enters the controller unless the task is
*resolved* (FILLED, aggressive attempt already used, or TECHNICAL).

Defect: the cutoff skipped the controller for ANY terminal OMS state, including a REJECTED
passive whose single aggressive attempt was still unused. Such a task could never complete even
when liquidity returned before the cutoff.

Required fix (remediation R7): a REJECTED passive (aggressive unused) sends the one aggressive
attempt at the cutoff. Reproduction: a passive rejected during a halt, with liquidity returning
before the cutoff -> aggressive at the cutoff completes the task.

L1 timing (``c=50us, e=250us, r=250us``): a JOIN at arrival ``t0`` arrives at ``t0+300us``; the
cutoff is ``T - G`` with ``G = 1.400001ms``; an aggressive sent at the cutoff arrives at
``cutoff + 300us`` and (if TRADING with opposite liquidity) fills there, before ``T``.
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)

from qexec.core.config import ExperimentConfig
from qexec.core.types import Action, Side, TradingStatus
from qexec.policies.baselines import B1Policy
from qexec.sim.engine import SessionEngine

START = 1_000_000_000_000
WARMUP = 1_000_000_000
HORIZON = 1_000_000_000


def _cfg() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="r7", horizon_ns=HORIZON, latency_id="L1")


def _session(tmp_path: Path, sid: str, t0: int) -> Path:
    """A one-tick book that halts around the passive arrival (t0+300us) and resumes TRADING well
    before the cutoff, so the JOIN passive is REJECTED (STATUS) but liquidity exists at the
    cutoff for the terminal aggressive to complete."""
    deadline = t0 + HORIZON
    cutoff = deadline - 1_400_001
    b = H.SessionBuilder(sid, START)
    b.add_status(TradingStatus.TRADING, START, START)
    b.snapshot(
        START,
        bids=[(H.BID, 50, 1), (H.BID2, 50, 2)],
        asks=[(H.ASK, 50, 3), (H.ASK2, 50, 4)],
    )
    # Halt effective (and observed) before the passive arrival t0+300us; resume well before the
    # cutoff. Capture == event so historical and client both see it at the same wall time; the
    # overlay rejects the passive because the HISTORICAL book is HALTED at arrival.
    halt_t = t0 + 100 * H.US
    resume_t = t0 + 200 * H.MS  # long before the cutoff
    b.add_status(TradingStatus.HALTED, halt_t, halt_t)
    b.add_status(TradingStatus.TRADING, resume_t, resume_t)
    # Deep harmless feed to keep committed mids valid at arrival and the deadline. Avoid touching
    # the touch so no passive fill happens; the only completion path is the cutoff aggressive.
    deep = H.BID2 - 10 * H.TICK
    t = START + 10 * H.MS
    oid = 5000
    while t < deadline + 2 * H.S:
        # Skip the deep feed inside the halt window so no committed mid is produced there, but
        # that does not matter for arrival (before halt) / deadline (after resume).
        if not (halt_t <= t <= resume_t):
            b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
            b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
            oid += 1
        t += 50 * H.MS
    assert cutoff > resume_t
    return b.write(tmp_path)


def test_rejected_passive_sends_aggressive_at_cutoff(tmp_path: Path) -> None:
    sid = "SYN-R7"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _session(tmp_path, sid, t0)
    deadline = t0 + HORIZON
    cutoff = deadline - 1_400_001
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()

    wid = f"{sid}:00000:BUY|B1"
    reports = out.reports.filter(out.reports["world_id"] == wid)
    kinds = reports["report_kind"].to_list()
    # The passive was rejected (halt), then an aggressive was sent at the cutoff and filled.
    assert "REJECTED" in kinds
    assert "FILL" in kinds

    buy = (
        out.task_results.filter(out.task_results["task_id"] == f"{sid}:00000:BUY")
        .filter(out.task_results["policy_id"] == "B1")
        .row(0, named=True)
    )
    assert buy["status"] == "COMPLETED_ON_TIME"
    assert buy["executed_quantity"] == 1
    # The completing fill is the aggressive at the cutoff (arrives cutoff + 300us <= deadline).
    assert buy["completion_time_ns"] == cutoff + 300 * H.US
    assert buy["fill_mechanism"] == "AGGRESSIVE"

    # Exactly one execution (no over-execution; the single aggressive attempt).
    ex = out.executions.filter(out.executions["world_id"] == wid)
    assert ex.height == 1
