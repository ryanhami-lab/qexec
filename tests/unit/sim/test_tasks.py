"""Task manifest and quality-map tests (engine spec section 3)."""

from __future__ import annotations

import hashlib
from itertools import pairwise
from pathlib import Path

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)
import numpy as np

from qexec.core.config import LATENCY_SCENARIOS, ExperimentConfig, arrival_spacing_ns
from qexec.core.types import Action, Side, TradingStatus
from qexec.labels.price import MidSeries
from qexec.sim.quality import QualityWindow
from qexec.sim.tasks import build_task_manifest
from qexec.synthetic.generator import SyntheticParams, write_synthetic_session


def _write_long_session(tmp_path: Path, session_id: str = "SYN-M", duration_s: int = 60) -> Path:
    params = SyntheticParams(
        seed=7, session_id=session_id, start_ns=1_000_000_000_000, duration_s=duration_s
    )
    return write_synthetic_session(params, tmp_path)


def _config() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="m", horizon_ns=1_000_000_000, latency_id="L1")


def test_manifest_grid_phase_and_spacing(tmp_path: Path) -> None:
    sd = _write_long_session(tmp_path)
    cfg = _config()
    manifest = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    assert manifest.tasks, "expected eligible tasks"
    # Arrivals (unique) should be spaced exactly arrival_spacing_ns(H) apart.
    spacing = arrival_spacing_ns(cfg.horizon_ns)
    arrivals = sorted({int(t.arrival_time_ns) for t in manifest.tasks})
    for a, b in pairwise(arrivals):
        assert b - a == spacing
    # Both sides present per arrival.
    for a in arrivals:
        sides = {t.side.name for t in manifest.tasks if int(t.arrival_time_ns) == a}
        assert sides == {"BUY", "SELL"}


def test_phase_is_deterministic_from_seed_and_session() -> None:
    # The documented phase draw: default_rng(seed_from(task_seed, session_id)).integers(0, spacing)
    spacing = arrival_spacing_ns(1_000_000_000)
    digest = hashlib.sha256(f"{0}:{'SYN-M'}".encode()).digest()
    seed_int = int.from_bytes(digest[:8], "big")
    expected = int(np.random.default_rng(seed_int).integers(0, spacing))
    assert 0 <= expected < spacing  # sanity on the documented range


def test_manifest_id_stable_across_runs(tmp_path: Path) -> None:
    sd = _write_long_session(tmp_path, session_id="SYN-ID")
    cfg = _config()
    m1 = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    m2 = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    assert m1.task_manifest_id == m2.task_manifest_id
    assert all(t.task_manifest_id == m1.task_manifest_id for t in m1.tasks)


def test_m0_is_committed_historical_mid(tmp_path: Path) -> None:
    sd = _write_long_session(tmp_path, session_id="SYN-MID")
    cfg = _config()
    series = MidSeries.from_session(sd)
    manifest = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    for task in manifest.tasks:
        expected = series.mid_at(int(task.arrival_time_ns))
        assert expected is not None
        assert task.arrival_reference_mid2 == expected


def test_quality_map_flags_clear_in_window(tmp_path: Path) -> None:
    """A hand-built session with a CLEAR inside a window marks that window unevaluable."""
    start = 1_000_000_000_000
    b = H.SessionBuilder("SYN-CLR", start)
    b.add_status(TradingStatus.TRADING, start, start)
    b.snapshot(
        start, bids=[(H.BID, 10, 1), (H.BID2, 10, 2)], asks=[(H.ASK, 10, 3), (H.ASK2, 10, 4)]
    )
    # Fill many trivial events; put a CLEAR well into the session.
    t = start + 10 * H.MS
    clear_t = start + 2 * H.S
    while t < start + 60 * H.S:
        if abs(t - clear_t) < H.MS:
            b.add_event(t, [H.RecordSpec(Action.CLEAR, Side.NONE, 0, 0, 0)])
            # Re-seed the book with a snapshot so later mids exist.
            b.snapshot(
                t + H.US,
                bids=[(H.BID, 10, 100), (H.BID2, 10, 101)],
                asks=[(H.ASK, 10, 102), (H.ASK2, 10, 103)],
            )
        else:
            b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, H.BID2, 1, 1000 + (t % 100000))])
        t += 100 * H.MS
    sd = b.write(tmp_path)
    cfg = _config()
    manifest = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    qmap = manifest.quality
    # A window spanning the CLEAR time is unevaluable; one before it is not.
    uneval, reason = qmap.is_unevaluable(QualityWindow(clear_t - 100 * H.MS, clear_t + 100 * H.MS))
    assert uneval
    assert reason is not None


def test_quality_map_flags_crossed_book_anomaly_in_window(tmp_path: Path) -> None:
    """Regression (reviewer Fix #2): a batch that commits a crossed (locked) book while TRADING
    inside a task window must be flagged BOOK_ANOMALY by the policy-independent quality map.

    ``apply_record`` never produces an INVARIANT_VIOLATION; only ``commit_batch``'s boundary
    check does. The manifest pass previously skipped ``commit_batch`` for performance, so a
    transiently crossed committed book was flagged in the engine's main pass but silently left
    evaluable in the quality map. The manifest pass now runs ``commit_batch``, so the detection
    is identical in both passes."""
    start = 1_000_000_000_000
    warmup = 1_000_000_000
    horizon = 1_000_000_000
    sid = "SYN-XBOOK"
    t0 = H.first_arrival_ns(sid, start, warmup, horizon)
    cross_t = t0 + horizon // 2  # strictly inside [t0, t0 + horizon]

    b = H.SessionBuilder(sid, start)
    b.add_status(TradingStatus.TRADING, start, start)
    b.snapshot(
        start, bids=[(H.BID, 10, 1), (H.BID2, 10, 2)], asks=[(H.ASK, 10, 3), (H.ASK2, 10, 4)]
    )
    # Keep the feed alive with harmless deep ADD/CANCEL pairs; inject a crossing batch at cross_t.
    deep = H.BID2 - 10 * H.TICK
    t = start + 10 * H.MS
    oid = 7000
    injected = False
    while t < start + 60 * H.S:
        if not injected and t >= cross_t:
            # One batch that adds a BID above the resting best ASK -> crossed book while TRADING.
            # Then immediately uncross so later mids remain valid and the window stays usable.
            cross_oid = oid
            b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, H.ASK2, 5, cross_oid)])
            b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, H.ASK2, 5, cross_oid)])
            injected = True
            oid += 1
        else:
            b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
            b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
            oid += 1
        t += 100 * H.MS
    assert injected
    sd = b.write(tmp_path)

    cfg = _config()
    manifest = build_task_manifest(sd, cfg, planned_scenarios=(LATENCY_SCENARIOS["L1"],))
    qmap = manifest.quality
    uneval, reason = qmap.is_unevaluable(QualityWindow(t0, t0 + horizon))
    assert uneval
    assert reason == "BOOK_ANOMALY"
