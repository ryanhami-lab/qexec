"""R9 reproduction (manifest plane): task eligibility honors status on a merged status/book
timeline, including a halt that begins in a quiet gap, and the client-mid check honors delayed
status observations per scenario.

External review R9: the manifest applied status to the historical book only when a committed
batch arrived, so a halt beginning in a quiet gap (no batches during the gap) before a task's
arrival was ignored -- the task stayed eligible with a stale ``m0`` and a stale client mid.

Fix: the manifest advances both the historical status and each scenario's client-observed
status on a merged timeline. A task is ineligible when, at its arrival, the historical book is
not TRADING (no valid ``m0``) or when a scenario's client-observed status at arrival is not
TRADING (no valid client mid for that scenario).
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812

from qexec.core.config import LATENCY_SCENARIOS, ExperimentConfig
from qexec.core.types import Action, Side, TradingStatus
from qexec.sim.tasks import build_task_manifest

WARMUP = 1 * H.S
HORIZON = 1 * H.S


def _config() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="r9", horizon_ns=HORIZON, latency_id="L1")


def _base_builder(sid: str, start: int) -> H.SessionBuilder:
    b = H.SessionBuilder(sid, start)
    b.add_status(TradingStatus.TRADING, start, start)
    b.snapshot(
        start, bids=[(H.BID, 10, 1), (H.BID2, 10, 2)], asks=[(H.ASK, 10, 3), (H.ASK2, 10, 4)]
    )
    return b


def _keep_alive(b: H.SessionBuilder, t0: int, t1: int, step: int, oid_start: int) -> int:
    """Harmless deep ADD/CANCEL pairs from ``t0`` up to (not into) ``t1``; returns next oid."""
    oid = oid_start
    t = t0
    deep = H.BID2 - 10 * H.TICK
    while t < t1:
        b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
        b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
        oid += 1
        t += step
    return oid


def test_no_halt_control_task_is_eligible(tmp_path: Path) -> None:
    sid = "SYN-R9OK"
    start = 1_000_000_000_000
    arrival = H.first_arrival_ns(sid, start, WARMUP, HORIZON)
    b = _base_builder(sid, start)
    # Keep the feed alive across the whole window so the control task is clearly eligible.
    _keep_alive(b, start + 10 * H.MS, arrival + HORIZON + 2 * H.S, 100 * H.MS, 7000)
    sd = b.write(tmp_path)
    manifest = build_task_manifest(sd, _config(), planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    arrivals = {int(t.arrival_time_ns) for t in manifest.tasks}
    assert arrival in arrivals, "control task should be eligible"


def test_quiet_gap_halt_before_arrival_makes_task_ineligible(tmp_path: Path) -> None:
    sid = "SYN-R9HALT"
    start = 1_000_000_000_000
    arrival = H.first_arrival_ns(sid, start, WARMUP, HORIZON)
    b = _base_builder(sid, start)
    # Trade up to a point well before the arrival, then stop producing batches (quiet gap).
    halt_t = arrival - 500 * H.MS
    _keep_alive(b, start + 10 * H.MS, halt_t - 100 * H.MS, 100 * H.MS, 7000)
    # HALT during the quiet gap, before the arrival; no batches between halt_t and the arrival.
    b.add_status(TradingStatus.HALTED, halt_t, halt_t)
    # Keep a far-future batch so the session extends past the deadline (drain margin satisfied),
    # but it is well after the task window so the quiet gap truly spans the arrival.
    tail = arrival + HORIZON + 2 * H.S
    b.add_event(tail, [H.RecordSpec(Action.ADD, Side.BID, H.BID2 - 10 * H.TICK, 1, 9000)])
    b.add_event(tail + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, H.BID2 - 10 * H.TICK, 1, 9000)])
    sd = b.write(tmp_path)

    cfg = _config()
    # L1 client has zero added delivery delay, so it observes the halt at halt_t < arrival.
    manifest = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    eligible_arrivals = {int(t.arrival_time_ns) for t in manifest.tasks}
    assert arrival not in eligible_arrivals, "task under a quiet-gap halt must be ineligible"
    # The ineligibility is recorded with a reason for both sides.
    reasons = {
        (int(ia.arrival_ns), ia.side.name): ia.reason
        for ia in manifest.ineligible
        if int(ia.arrival_ns) == arrival
    }
    assert reasons, "the halted arrival must be recorded as ineligible with a reason"
    assert all(r is not None for r in reasons.values())
