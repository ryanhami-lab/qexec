"""Engine timing tests on hand-built micro-sessions (engine spec section 10).

Exact latency arithmetic is hand-derived for L1 (``c=50us, e=250us, r=250us``):

* a JOIN decided at arrival ``t0`` sends at ``t0 + 50us`` and arrives at ``t0 + 300us``;
* a fill at exchange time ``x`` is reported (delivered) at ``x + 250us``;
* the terminal cutoff is ``T - G`` with ``G = 3(c+e) + 2r + 1 = 3*300us + 500us + 1 = 1.400001ms``.

The sessions are built long enough for the manifest to produce exactly one arrival; the test
reads the actual arrival from the manifest and asserts every derived time against it.
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)

from qexec.core.config import ExperimentConfig, guard_ns
from qexec.core.types import Action, Side, TradingStatus
from qexec.policies.baselines import B1Policy
from qexec.sim.engine import SessionEngine


def _static_book_session(
    tmp_path: Path, session_id: str, duration_s: int = 60, with_gap: bool = False
) -> Path:
    """A session with a one-tick book that stays put (no fills) for the whole duration.

    A sparse stream of harmless deep-book ADD/CANCEL pairs keeps committed mids valid at every
    point without ever touching the touch queue, so a joined passive order never fills and the
    world runs all the way to the cutoff/deadline (exercising the full timer chain, T38 when
    ``with_gap`` leaves a long event-free span).
    """
    start = 1_000_000_000_000
    b = H.SessionBuilder(session_id, start)
    b.add_status(TradingStatus.TRADING, start, start)
    b.snapshot(
        start,
        bids=[(H.BID, 50, 1), (H.BID2, 50, 2)],
        asks=[(H.ASK, 50, 3), (H.ASK2, 50, 4)],
    )
    deep_bid = H.BID2 - 10 * H.TICK  # far from the touch
    t = start + 10 * H.MS
    oid = 1000
    step = 100 * H.MS if not with_gap else 5 * H.S
    while t < start + duration_s * H.S:
        # Add then cancel a deep order: keeps the feed alive, never changes the touch.
        b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep_bid, 1, oid)])
        b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep_bid, 1, oid)])
        oid += 1
        t += step
    return b.write(tmp_path)


def _cfg() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="t", horizon_ns=1_000_000_000, latency_id="L1")


def _run(sd: Path, cfg: ExperimentConfig) -> tuple[SessionEngine, object]:
    eng = SessionEngine(sd, cfg, [B1Policy()], scenario_id="L1")
    return eng, eng.run()


def test_join_command_arrival_time_t0_plus_300us(tmp_path: Path) -> None:
    """T11-adjacent: a JOIN at arrival t0 arrives at the exchange at t0 + 300us (c+e)."""
    sd = _static_book_session(tmp_path, "SYN-T11")
    cfg = _cfg()
    _eng, out = _run(sd, cfg)
    task = out.tasks.filter(out.tasks["eligible"]).row(0, named=True)
    t0 = task["arrival_time_ns"]
    tid = task["task_id"]
    trace = out.trace(tid, "B1")
    created = next(
        e for e in trace if e["kind"] == "command_created" and e["command_kind"] == "PASSIVE_LIMIT"
    )
    assert created["send_ns"] == t0 + 50 * H.US  # c = 50us
    assert created["arrival_ns"] == t0 + 300 * H.US  # c + e = 300us
    arrival = next(e for e in trace if e["kind"] == "command_arrival")
    assert arrival["time_ns"] == t0 + 300 * H.US


def test_fill_report_delivered_250us_after_exchange_time(tmp_path: Path) -> None:
    """T13/T18: a report (ACCEPTED here) is delivered r = 250us after its exchange time."""
    sd = _static_book_session(tmp_path, "SYN-T13")
    cfg = _cfg()
    _eng, out = _run(sd, cfg)
    tid = out.task_results.filter(out.task_results["policy_id"] == "B1").row(0, named=True)[
        "task_id"
    ]
    rep = out.reports.filter(
        (out.reports["task_id"] == tid) & (out.reports["report_kind"] == "ACCEPTED")
    ).row(0, named=True)
    assert rep["delivery_time_ns"] == rep["exchange_time_ns"] + 250 * H.US


def test_cutoff_is_deadline_minus_guard_1_400001ms(tmp_path: Path) -> None:
    """The terminal cutoff fires at T - G, G = 1.400001 ms under L1 (architecture 7.3)."""
    sd = _static_book_session(tmp_path, "SYN-CUT")
    cfg = _cfg()
    g = guard_ns(50 * H.US, 250 * H.US, 250 * H.US)
    assert g == 1_400_001  # 1.400001 ms
    _eng, out = _run(sd, cfg)
    task = out.tasks.filter(out.tasks["eligible"]).row(0, named=True)
    deadline = task["deadline_ns"]
    tid = task["task_id"]
    trace = out.trace(tid, "B1")
    cutoff = next(e for e in trace if e["kind"] == "cutoff")
    assert cutoff["time_ns"] == deadline - g


def test_timers_fire_with_no_market_events_T38(tmp_path: Path) -> None:
    """T38: with a long event-free gap the arrival/checkpoint/cutoff/deadline timers still fire."""
    sd = _static_book_session(tmp_path, "SYN-T38", with_gap=True)
    cfg = _cfg()
    _eng, out = _run(sd, cfg)
    task = out.tasks.filter(out.tasks["eligible"]).row(0, named=True)
    tid = task["task_id"]
    trace = out.trace(tid, "B1")
    kinds = [e["kind"] for e in trace]
    assert "arrival" in kinds
    assert "checkpoint" in kinds
    assert "cutoff" in kinds
    assert "deadline_freeze" in kinds
    # The checkpoint fires exactly at t0 + H/2 even though no market event occurs then.
    t0 = task["arrival_time_ns"]
    checkpoint = next(e for e in trace if e["kind"] == "checkpoint")
    assert checkpoint["time_ns"] == t0 + cfg.horizon_ns // 2


def test_executed_quantity_never_exceeds_one(tmp_path: Path) -> None:
    sd = _static_book_session(tmp_path, "SYN-Q1")
    cfg = _cfg()
    _eng, out = _run(sd, cfg)
    assert out.task_results["executed_quantity"].max() <= 1
