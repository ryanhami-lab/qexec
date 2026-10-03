"""R5 reproduction: the quality map records invalid *intervals*, not corruption *instants*.

External review R5: the quality map flagged a task window only if a corrupting batch commit fell
*inside* the window, so a task that arrived *after* an unrecovered corruption (the corrupting
instant now behind it, no recovery yet) was wrongly evaluable.

Fix: a corrupting event opens an invalid interval that runs until a *trusted recovery* -- an
INITIALIZATION snapshot batch after the corruption carrying no new anomaly and no quality flags.
An unrecovered interval extends to the session end. A task is unevaluable iff its window
``[arrival, deadline]`` overlaps any invalid interval.
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812

from qexec.core.config import LATENCY_SCENARIOS, ExperimentConfig
from qexec.core.types import Action, Side, TradingStatus
from qexec.sim.quality import QualityWindow
from qexec.sim.tasks import build_task_manifest

WARMUP = 1 * H.S
HORIZON = 1 * H.S


def _config() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="r5", horizon_ns=HORIZON, latency_id="L1")


def _base_builder(sid: str, start: int) -> H.SessionBuilder:
    b = H.SessionBuilder(sid, start)
    b.add_status(TradingStatus.TRADING, start, start)
    b.snapshot(
        start, bids=[(H.BID, 10, 1), (H.BID2, 10, 2)], asks=[(H.ASK, 10, 3), (H.ASK2, 10, 4)]
    )
    return b


def _keep_alive(b: H.SessionBuilder, t0: int, t1: int, step: int, oid_start: int) -> int:
    oid = oid_start
    t = t0
    deep = H.BID2 - 10 * H.TICK
    while t < t1:
        b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
        b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
        oid += 1
        t += step
    return oid


def test_unrecovered_corruption_before_arrival_is_unevaluable(tmp_path: Path) -> None:
    sid = "SYN-R5NOREC"
    start = 1_000_000_000_000
    arrival = H.first_arrival_ns(sid, start, WARMUP, HORIZON)
    corrupt_t = arrival - 100 * H.MS  # 100 ms before arrival; strictly outside [arrival, deadline]

    b = _base_builder(sid, start)
    oid = _keep_alive(b, start + 10 * H.MS, corrupt_t, 50 * H.MS, 7000)
    # Corrupting event: cancel of an unknown order -> CANCEL_UNKNOWN anomaly. No recovery after.
    b.add_event(corrupt_t, [H.RecordSpec(Action.CANCEL, Side.BID, H.BID, 1, 999_999)])
    # Keep trading afterwards (NO recovery snapshot), through the whole window and drain.
    _keep_alive(b, corrupt_t + 50 * H.MS, arrival + HORIZON + 2 * H.S, 50 * H.MS, oid)
    sd = b.write(tmp_path)

    cfg = _config()
    manifest = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    qmap = manifest.quality
    # The corruption is 100 ms *before* arrival (not inside the window), yet unrecovered -> the
    # invalid interval runs to session end and overlaps [arrival, deadline] -> unevaluable.
    uneval, reason = qmap.is_unevaluable(QualityWindow(arrival, arrival + HORIZON))
    assert uneval, "unrecovered corruption before arrival must leave the window unevaluable"
    assert reason is not None


def test_recovery_snapshot_before_arrival_restores_evaluability(tmp_path: Path) -> None:
    sid = "SYN-R5REC"
    start = 1_000_000_000_000
    arrival = H.first_arrival_ns(sid, start, WARMUP, HORIZON)
    corrupt_t = arrival - 300 * H.MS
    recovery_t = arrival - 100 * H.MS  # trusted recovery before arrival

    b = _base_builder(sid, start)
    oid = _keep_alive(b, start + 10 * H.MS, corrupt_t, 50 * H.MS, 7000)
    b.add_event(corrupt_t, [H.RecordSpec(Action.CANCEL, Side.BID, H.BID, 1, 999_999)])
    # Trusted recovery: an INITIALIZATION snapshot batch (all SNAPSHOT adds) with no new anomaly.
    b.snapshot(
        recovery_t,
        bids=[(H.BID, 10, 2001), (H.BID2, 10, 2002)],
        asks=[(H.ASK, 10, 2003), (H.ASK2, 10, 2004)],
    )
    _keep_alive(b, recovery_t + 50 * H.MS, arrival + HORIZON + 2 * H.S, 50 * H.MS, oid)
    sd = b.write(tmp_path)

    cfg = _config()
    manifest = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    qmap = manifest.quality
    # The invalid interval [corrupt_t, recovery_t) ends before the arrival, so the window is clean.
    uneval, _ = qmap.is_unevaluable(QualityWindow(arrival, arrival + HORIZON))
    assert not uneval, "a trusted recovery before arrival must restore evaluability"
    # The task is in the eligible manifest.
    eligible = {int(t.arrival_time_ns) for t in manifest.tasks}
    assert arrival in eligible


def test_window_overlapping_the_invalid_interval_is_unevaluable(tmp_path: Path) -> None:
    # Even with a recovery, a window that *overlaps* the invalid interval stays unevaluable.
    sid = "SYN-R5OVL"
    start = 1_000_000_000_000
    arrival = H.first_arrival_ns(sid, start, WARMUP, HORIZON)
    # Corruption inside the window; recovery after the window.
    corrupt_t = arrival + HORIZON // 2
    recovery_t = arrival + HORIZON + 500 * H.MS

    b = _base_builder(sid, start)
    oid = _keep_alive(b, start + 10 * H.MS, corrupt_t, 50 * H.MS, 7000)
    b.add_event(corrupt_t, [H.RecordSpec(Action.CANCEL, Side.BID, H.BID, 1, 999_999)])
    oid = _keep_alive(b, corrupt_t + 50 * H.MS, recovery_t, 50 * H.MS, oid)
    b.snapshot(
        recovery_t,
        bids=[(H.BID, 10, 3001), (H.BID2, 10, 3002)],
        asks=[(H.ASK, 10, 3003), (H.ASK2, 10, 3004)],
    )
    _keep_alive(b, recovery_t + 50 * H.MS, arrival + HORIZON + 3 * H.S, 50 * H.MS, oid)
    sd = b.write(tmp_path)

    cfg = _config()
    manifest = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    uneval, reason = manifest.quality.is_unevaluable(QualityWindow(arrival, arrival + HORIZON))
    assert uneval
    assert reason is not None
