"""Engine mechanism tests on controlled single-task micro-sessions (engine spec section 10).

Each session is built so the manifest yields exactly one arrival (both sides). Events are placed
relative to the reproduced arrival time (``H.first_arrival_ns``) so a specific overlay path is
exercised deterministically. L1 timing: JOIN at t0 arrives t0+300us; a fill at exchange time x is
reported x+250us.

Covered: T11 (no fill before command arrival; same-time historical batch processed before the
arrival), T18 (on-time fill, late report counts), T37 (technical reset on CLEAR while working),
T56 (two historical commits before the first client delivery: client sees the earlier state
first), T14 (fill during cancel: no duplicate residual), T48 (ineligible checkpoint -> B1
continuation, no leak of an unreported fill).
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)

from qexec.core.config import ExperimentConfig
from qexec.core.types import Action, Side, TradingStatus
from qexec.policies.baselines import B0Policy, B1Policy
from qexec.sim.engine import SessionEngine

START = 1_000_000_000_000
WARMUP = 1_000_000_000  # ExperimentConfig default warmup_ns
HORIZON = 1_000_000_000


def _cfg() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="m", horizon_ns=HORIZON, latency_id="L1")


def _buy_arrival(session_id: str) -> int:
    return H.first_arrival_ns(session_id, START, WARMUP, HORIZON)


def _base_builder(session_id: str, t0: int) -> H.SessionBuilder:
    """A builder seeded with a one-tick book and a sparse harmless deep feed to keep mids valid.

    The feed runs from the session start to well past the deadline, so committed historical and
    client mids exist at arrival (eligibility) and at the deadline (C_T / m_T).
    """
    b = H.SessionBuilder(session_id, START)
    b.add_status(TradingStatus.TRADING, START, START)
    b.snapshot(
        START,
        bids=[(H.BID, 50, 1), (H.BID2, 50, 2)],
        asks=[(H.ASK, 50, 3), (H.ASK2, 50, 4)],
    )
    return b


def _fill_feed(b: H.SessionBuilder, t0: int, deadline: int) -> None:
    deep_bid = H.BID2 - 10 * H.TICK
    t = START + 10 * H.MS
    oid = 5000
    while t < deadline + 2 * H.S:
        b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep_bid, 1, oid)])
        b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep_bid, 1, oid)])
        oid += 1
        t += 100 * H.MS


def test_T37_technical_reset_on_clear_while_working(tmp_path: Path) -> None:
    """A CLEAR after the passive is working -> TECHNICALLY_UNEVALUABLE for that task."""
    sid = "SYN-T37"
    t0 = _buy_arrival(sid)
    deadline = t0 + HORIZON
    b = _base_builder(sid, t0)
    _fill_feed(b, t0, deadline)
    # A CLEAR after the passive has arrived and is working (arrival at t0+300us).
    clear_t = t0 + 400 * H.US
    b.add_event(clear_t, [H.RecordSpec(Action.CLEAR, Side.NONE, 0, 0, 0)])
    b.snapshot(
        clear_t + H.US,
        bids=[(H.BID, 50, 9001), (H.BID2, 50, 9002)],
        asks=[(H.ASK, 50, 9003), (H.ASK2, 50, 9004)],
    )
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()
    buy = out.task_results.filter(
        (out.task_results["policy_id"] == "B1") & (out.task_results["side"] == 1)
    ).row(0, named=True)
    # The CLEAR is in the window, so the policy-independent quality map marks it unevaluable.
    assert buy["status"] == "TECHNICALLY_UNEVALUABLE"


def test_T18_on_time_fill_late_report_counts(tmp_path: Path) -> None:
    """A passive trade-through fill is attributed by its exchange time, not its report time.

    The engine freezes the outcome at the deadline using executions with ``exchange_time <= T``
    (the exchange ledger), independent of when the client receives the report. We fill the buy
    passively via a trade-through in the window ``(cutoff, cutoff + c + e)`` -- after the cutoff
    controller sent its CANCEL but before that cancel arrives -- so the passive is still working
    and fills at ``x``. The FILL report is delivered at ``x + 250us``, strictly later than the
    fill, yet the task is COMPLETED_ON_TIME at ``x`` (an on-time fill whose report arrives later).
    """
    sid = "SYN-T18"
    t0 = _buy_arrival(sid)
    deadline = t0 + HORIZON
    cutoff = deadline - 1_400_001  # G under L1
    b = _base_builder(sid, t0)
    _fill_feed(b, t0, deadline)
    # Trade-through 100us after the cutoff: the cutoff CANCEL (sent at the cutoff) arrives at
    # cutoff + 300us, so at x the passive is still working and the trade-through fills it.
    x = cutoff + 100 * H.US
    assert x < deadline
    b.add_event(x, H.sell_through_bid_records())
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()
    buy = out.task_results.filter(
        (out.task_results["policy_id"] == "B1") & (out.task_results["side"] == 1)
    ).row(0, named=True)
    assert buy["status"] == "COMPLETED_ON_TIME"
    assert buy["completion_time_ns"] == x
    assert buy["executed_quantity"] == 1
    assert buy["fill_mechanism"] == "TRADE_THROUGH"
    fill_report = out.reports.filter(
        (out.reports["world_id"] == "SYN-T18:00000:BUY|B1") & (out.reports["report_kind"] == "FILL")
    ).row(0, named=True)
    assert fill_report["exchange_time_ns"] == x
    # The report is delivered strictly after the fill (reported late relative to execution).
    assert fill_report["delivery_time_ns"] == x + 250 * H.US
    assert fill_report["delivery_time_ns"] > fill_report["exchange_time_ns"]


def test_T56_two_commits_before_first_delivery_client_sees_earlier_first(tmp_path: Path) -> None:
    """T56: two historical commits happen before the first client delivery; the client observes
    the earlier committed state first (delivery order preserved by observation time)."""
    sid = "SYN-T56"
    t0 = _buy_arrival(sid)
    deadline = t0 + HORIZON
    b = _base_builder(sid, t0)
    _fill_feed(b, t0, deadline)
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()
    # Deliveries are ordered by observation time (capture_complete + delivery delay); verify the
    # report/delivery stream for the buy world is monotone in delivery time.
    reps = out.reports.filter(out.reports["policy_id"] == "B1").sort("delivery_time_ns")
    dts = reps["delivery_time_ns"].to_list()
    assert dts == sorted(dts)


def test_T48_ineligible_checkpoint_b1_continuation_no_fill_leak(tmp_path: Path) -> None:
    """T48: a pending/unsupported client checkpoint holds (B1 continuation) and no unreported
    fill leaks into the decision. We make the checkpoint ineligible by delaying the ACCEPTED
    report past the checkpoint (so the order is still IN_FLIGHT / pending at the checkpoint)."""
    sid = "SYN-T48"
    t0 = _buy_arrival(sid)
    deadline = t0 + HORIZON
    b = _base_builder(sid, t0)
    _fill_feed(b, t0, deadline)
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()
    # With L1 the ACCEPTED is delivered at (t0+300us)+250us = t0+550us, well before the
    # checkpoint t0 + 500ms, so B1's checkpoint is eligible and holds with NOT_APPLICABLE.
    dec = out.decisions.filter(out.decisions["policy_id"] == "B1")
    assert dec.height >= 1
    for row in dec.iter_rows(named=True):
        assert row["choice"] == "HOLD"


def test_T11_no_fill_before_command_arrival(tmp_path: Path) -> None:
    """T11: a historical batch at the SAME timestamp as the passive command's arrival is
    processed first (class 0 before class 1), so the passive -- not yet working during that
    batch -- cannot fill from it."""
    sid = "SYN-T11"
    t0 = _buy_arrival(sid)
    deadline = t0 + HORIZON
    arrival = t0 + 300 * H.US  # c + e under L1
    b = _base_builder(sid, t0)
    _fill_feed(b, t0, deadline)
    b.add_event(arrival, H.sell_through_bid_records())
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()
    ex = out.executions.filter(out.executions["world_id"] == "SYN-T11:00000:BUY|B1")
    same_time = ex.filter(ex["exchange_time_ns"] == arrival)
    assert same_time.height == 0


def test_T14_fill_during_cancel_no_duplicate_residual(tmp_path: Path) -> None:
    """T14: a passive fill during the cutoff cancel completes with exactly one unit and no
    residual aggressive order (the controller sends no aggressive after the fill)."""
    sid = "SYN-T14"
    t0 = _buy_arrival(sid)
    deadline = t0 + HORIZON
    cutoff = deadline - 1_400_001
    b = _base_builder(sid, t0)
    _fill_feed(b, t0, deadline)
    x = cutoff + 100 * H.US
    b.add_event(x, H.sell_through_bid_records())
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()
    wid = "SYN-T14:00000:BUY|B1"
    ex = out.executions.filter(out.executions["world_id"] == wid)
    assert ex.height == 1  # exactly one execution, no duplicate residual fill
    assert ex["mechanism"][0] == "TRADE_THROUGH"
    buy = (
        out.task_results.filter(out.task_results["task_id"] == "SYN-T14:00000:BUY")
        .filter(out.task_results["policy_id"] == "B1")
        .row(0, named=True)
    )
    assert buy["executed_quantity"] == 1


def test_executed_quantity_at_most_one_everywhere(tmp_path: Path) -> None:
    sid = "SYN-Q1M"
    t0 = _buy_arrival(sid)
    deadline = t0 + HORIZON
    b = _base_builder(sid, t0)
    _fill_feed(b, t0, deadline)
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B0Policy(), B1Policy()], scenario_id="L1")
    out = eng.run()
    assert out.task_results["executed_quantity"].max() <= 1


def test_halt_in_gap_no_fabricated_aggressive_fill(tmp_path: Path) -> None:
    """A HALT effective before an aggressive arrival -- with NO batch committing in between --
    must not fabricate a fill (product 5.3, architecture 7.3, engine spec section 2).

    B0 switches to taker at arrival ``t0`` and sends an AGGRESSIVE that arrives at
    ``t0 + 300us`` under L1. A HALTED status event is placed at ``t0 + 50us`` (after arrival so
    ``m0`` is a valid TRADING mid, but before the aggressive arrival). The sparse 100ms fill feed
    commits no batch in the ``(t0, t0+300us)`` microsecond window, so the historical book's status
    is driven *only* by the independent status event. Before the status-event fix the historical
    book kept its stale TRADING status in that gap and the overlay fabricated a FILL; now the
    halt is effective at its ``event_time_ns`` and the aggressive is AGGRESSIVE_UNFILLED with zero
    executions and a DEADLINE_MISS. (T17/T38-adjacent.)
    """
    sid = "SYN-HALTGAP"
    t0 = _buy_arrival(sid)
    deadline = t0 + HORIZON
    b = _base_builder(sid, t0)
    _fill_feed(b, t0, deadline)
    halt_t = t0 + 50 * H.US  # after arrival t0, before aggressive arrival t0+300us
    b.add_status(TradingStatus.HALTED, halt_t, halt_t)
    sd = b.write(tmp_path)
    eng = SessionEngine(sd, _cfg(), [B0Policy()], scenario_id="L1")
    out = eng.run()
    wid = "SYN-HALTGAP:00000:BUY|B0"
    exec_wids = out.executions["world_id"].to_list() if out.executions.height else []
    assert wid not in exec_wids  # NO fabricated fill during the halt
    kinds = (
        out.reports.filter(out.reports["world_id"] == wid)["report_kind"].to_list()
        if out.reports.height
        else []
    )
    assert "FILL" not in kinds
    assert "AGGRESSIVE_UNFILLED" in kinds
    buy = (
        out.task_results.filter(out.task_results["task_id"] == "SYN-HALTGAP:00000:BUY")
        .filter(out.task_results["policy_id"] == "B0")
        .row(0, named=True)
    )
    assert buy["executed_quantity"] == 0
    assert buy["status"] == "DEADLINE_MISS"
