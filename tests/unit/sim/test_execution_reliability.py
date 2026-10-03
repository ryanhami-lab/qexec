"""Adversarial source-clock, recovery, and execution-evidence regressions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import _engine_helpers as H  # noqa: N812
import _overlay_helpers as O  # noqa: N812
import pytest

from qexec.analysis.support import select_support_horizon
from qexec.core.config import LATENCY_SCENARIOS, ExperimentConfig
from qexec.core.messages import ReportKind
from qexec.core.types import Action, RecordFlag, Side, TaskSide, TradingStatus
from qexec.policies.baselines import B0Policy, B1Policy
from qexec.reference.book import ReferenceBook
from qexec.sim.engine import SessionEngine
from qexec.sim.evidence import execution_groups, valid_resting_allocation
from qexec.sim.overlay import ExchangeOverlay
from qexec.sim.quality import QualityMap, QualityWindow
from qexec.sim.tasks import build_task_manifest

START = 1_000_000_000_000


def _session(sid: str) -> tuple[H.SessionBuilder, int]:
    arrival = H.first_arrival_ns(sid, START, H.S, H.S)
    b = H.SessionBuilder(sid, START)
    b.add_status(TradingStatus.TRADING, START, START)
    b.snapshot(START, [(H.BID, 5, 1)], [(H.ASK, 5, 2)])
    b.add_event(arrival + 3 * H.S, [H.RecordSpec(Action.NONE, Side.NONE, 0, 0, 0)])
    return b, arrival


def test_delayed_resume_does_not_make_client_eligible_before_capture(tmp_path: Path) -> None:
    b, arrival = _session("delayed-resume")
    b.add_status(TradingStatus.HALTED, arrival - 400 * H.MS, arrival - 300 * H.MS)
    b.add_status(TradingStatus.TRADING, arrival - 100 * H.MS, arrival + 700 * H.MS)
    b.add_event(arrival - 50 * H.MS, [H.RecordSpec(Action.NONE, Side.NONE, 0, 0, 0)])
    manifest = build_task_manifest(
        b.write(tmp_path), ExperimentConfig("capture"), (LATENCY_SCENARIOS["L1"],)
    )
    assert not manifest.tasks
    assert {row.reason for row in manifest.ineligible} == {"NO_CLIENT_MID[L1]"}


@pytest.mark.parametrize("resume_at_batch", [False, True])
def test_client_resume_uses_its_book_even_if_last_batch_committed_during_halt(
    tmp_path: Path, resume_at_batch: bool
) -> None:
    b, arrival = _session(f"quiet-resume-{resume_at_batch}")
    b.add_status(TradingStatus.HALTED, arrival - 300 * H.MS, arrival - 300 * H.MS)
    capture = arrival - 100 * H.MS if resume_at_batch else arrival - 200 * H.MS
    b.add_event(
        arrival - 200 * H.MS,
        [H.RecordSpec(Action.NONE, Side.NONE, 0, 0, 0)],
        capture_time_ns=capture,
    )
    b.add_status(TradingStatus.TRADING, arrival - 100 * H.MS, arrival - 100 * H.MS)
    manifest = build_task_manifest(
        b.write(tmp_path), ExperimentConfig("resume"), (LATENCY_SCENARIOS["L1"],)
    )
    assert len(manifest.tasks) == 2
    assert all(t.arrival_reference_mid2 == H.BID + H.ASK for t in manifest.tasks)


def test_quiet_opening_snapshot_is_sufficient_for_eligibility(tmp_path: Path) -> None:
    b, _ = _session("quiet-snapshot")
    manifest = build_task_manifest(
        b.write(tmp_path), ExperimentConfig("snapshot"), (LATENCY_SCENARIOS["L1"],)
    )
    assert len(manifest.tasks) == 2


@pytest.mark.parametrize("scenario", ["L0", "L2", "L6"])
@pytest.mark.parametrize("offset_ns", [-1, 0, 1])
def test_client_status_delivery_boundary_and_scenario_delay(
    tmp_path: Path, scenario: str, offset_ns: int
) -> None:
    b, arrival = _session(f"capture-edge-{scenario}-{offset_ns}")
    delay = LATENCY_SCENARIOS[scenario].added_delivery_ns
    b.add_status(TradingStatus.HALTED, arrival - 300 * H.MS, arrival - 300 * H.MS)
    b.add_status(TradingStatus.TRADING, arrival - 200 * H.MS, arrival - delay + offset_ns)
    manifest = build_task_manifest(
        b.write(tmp_path), ExperimentConfig("status-edge"), (LATENCY_SCENARIOS[scenario],)
    )
    assert bool(manifest.tasks) is (offset_ns <= 0)


def test_reordered_status_captures_follow_delivery_order(tmp_path: Path) -> None:
    b, arrival = _session("status-reordering")
    b.add_status(TradingStatus.HALTED, arrival - 300 * H.MS, arrival - 100 * H.MS)
    b.add_status(TradingStatus.TRADING, arrival - 200 * H.MS, arrival - 200 * H.MS)
    manifest = build_task_manifest(
        b.write(tmp_path), ExperimentConfig("status-reordering"), (LATENCY_SCENARIOS["L1"],)
    )
    assert not manifest.tasks
    assert {row.reason for row in manifest.ineligible} == {"NO_CLIENT_MID[L1]"}


def test_quiet_snapshot_has_deadline_value_and_reference_age(tmp_path: Path) -> None:
    b, arrival = _session("quiet-snapshot-engine")
    engine = SessionEngine(b.write(tmp_path), ExperimentConfig("quiet"), [B0Policy(), B1Policy()])
    outputs = engine.run()
    assert outputs.task_results.height == 4
    assert set(outputs.task_results["status"]) == {"COMPLETED_ON_TIME"}
    assert outputs.task_results["c_t_ticks"].null_count() == 0
    assert set(outputs.task_results["m_t_age_ns"]) == {arrival + H.S - START}


def test_clean_midtask_snapshot_is_common_technical_exclusion(tmp_path: Path) -> None:
    b, arrival = _session("common-snapshot-reset")
    b.snapshot(arrival + H.S // 2, [(H.BID, 5, 11)], [(H.ASK, 5, 12)])
    outputs = SessionEngine(
        b.write(tmp_path),
        ExperimentConfig("reset"),
        [B0Policy(), B1Policy()],
        fork_label_probe=True,
    ).run()
    assert outputs.task_results.height == 4
    assert set(outputs.task_results["status"]) == {"TECHNICALLY_UNEVALUABLE"}
    assert outputs.quality["technically_unevaluable"].to_list() == [True, True]


def test_capture_reordering_replays_client_mutations_not_historical_mids(tmp_path: Path) -> None:
    b, arrival = _session("reordered-capture")
    # Exchange receives MODIFY then CANCEL. Client receives CANCEL then MODIFY, so
    # the cancelled best bid cannot be resurrected by a historical snapshot of MODIFY.
    b.add_event(
        arrival - 300 * H.MS,
        [H.RecordSpec(Action.MODIFY, Side.BID, H.BID, 4, 1)],
        capture_time_ns=arrival - 100 * H.MS,
    )
    b.add_event(
        arrival - 200 * H.MS,
        [H.RecordSpec(Action.CANCEL, Side.BID, H.BID, 5, 1)],
        capture_time_ns=arrival - 200 * H.MS,
    )
    # The new exchange bid is captured only after arrival: historical m0 exists,
    # whereas the actual client book at arrival is still one-sided.
    b.add_event(
        arrival - 150 * H.MS,
        [H.RecordSpec(Action.ADD, Side.BID, H.BID2, 5, 3)],
        capture_time_ns=arrival + H.MS,
    )
    manifest = build_task_manifest(
        b.write(tmp_path), ExperimentConfig("reordering"), (LATENCY_SCENARIOS["L1"],)
    )
    assert not manifest.tasks
    assert {row.reason for row in manifest.ineligible} == {"NO_CLIENT_MID[L1]"}


def test_clean_snapshot_invalidates_crossing_windows_but_not_new_arrivals() -> None:
    snapshot = O.make_batch(
        [O.rec(1, Action.ADD, quantity=5, order_id=1, event_time_ns=200, flags=RecordFlag.SNAPSHOT)]
    )
    quality = QualityMap()
    quality.note_batch(snapshot, anomaly_added=False)
    assert quality.is_unevaluable(QualityWindow(100, 200)) == (True, "SNAPSHOT_RESET")
    assert quality.is_unevaluable(QualityWindow(200, 300)) == (False, None)


@pytest.mark.parametrize("new_anomaly", [False, True])
def test_combined_clear_snapshot_recovers_only_when_clean(new_anomaly: bool) -> None:
    quality = QualityMap()
    corrupt = O.make_batch([O.rec(0, Action.CANCEL, quantity=1, order_id=99, event_time_ns=100)])
    quality.note_batch(corrupt, anomaly_added=True)
    recovery = O.make_batch(
        [
            O.rec(1, Action.CLEAR, side=Side.NONE, price_fixed=0, quantity=0, event_time_ns=200),
            O.rec(
                2, Action.ADD, quantity=5, order_id=1, event_time_ns=200, flags=RecordFlag.SNAPSHOT
            ),
        ]
    )
    quality.note_batch(recovery, anomaly_added=new_anomaly)
    assert quality.is_unevaluable(QualityWindow(201, 300))[0] is new_anomaly
    assert quality.is_unevaluable(QualityWindow(150, 200))[0]


def _empty_ahead() -> tuple[ExchangeOverlay, ReferenceBook]:
    book = ReferenceBook(O.instrument())
    book.set_status(TradingStatus.TRADING)
    O.seed_book(
        book,
        [
            O.rec(0, Action.ADD, quantity=5, order_id=1),
            O.rec(1, Action.ADD, side=Side.ASK, price_fixed=O.ASK, quantity=5, order_id=2),
        ],
    )
    overlay = ExchangeOverlay("T1", O.instrument(), 0)
    overlay.on_command(O.passive_cmd(side=TaskSide.BUY, limit_price_fixed=O.BID), book)
    O.drive_batch(overlay, book, [O.rec(2, Action.CANCEL, quantity=5, order_id=1)])
    O.drive_batch(overlay, book, [O.rec(3, Action.ADD, quantity=5, order_id=3)])
    return overlay, book


@pytest.mark.parametrize("quantity", [0, -1])
@pytest.mark.parametrize("action", [Action.FILL, Action.TRADE])
def test_nonpositive_execution_volume_never_grants_fill(action: Action, quantity: int) -> None:
    overlay, book = _empty_ahead()
    record = O.rec(
        4,
        action,
        side=Side.BID if action is Action.FILL else Side.ASK,
        price_fixed=O.BID if action is Action.FILL else O.BID2,
        quantity=quantity,
        order_id=3 if action is Action.FILL else 0,
    )
    assert overlay.on_record(record, book) == []
    assert overlay.executed_quantity == 0


@pytest.mark.parametrize("defect", ["orphan", "unknown_order", "wrong_instrument", "excess_volume"])
def test_unreconciled_execution_group_never_grants_fill(defect: str) -> None:
    overlay, book = _empty_ahead()
    trade = O.rec(4, Action.TRADE, side=Side.ASK, quantity=1)
    fill = O.rec(5, Action.FILL, quantity=1, order_id=3)
    if defect == "unknown_order":
        fill = replace(fill, order_id=999)
    elif defect == "wrong_instrument":
        fill = replace(fill, instrument_id=2)
    elif defect == "excess_volume":
        fill = replace(fill, quantity=2)
    cancel = replace(fill, action=Action.CANCEL, source_ordinal=6)
    if defect == "wrong_instrument":
        # A direct callback must also reject another instrument (the adapter rejects
        # cross-instrument batches separately).
        reports = overlay.on_record(fill, book)
    else:
        records = [fill, cancel] if defect == "orphan" else [trade, fill, cancel]
        reports = O.drive_batch(overlay, book, records)
    assert all(r.kind is not ReportKind.FILL for r in reports)
    assert overlay.executed_quantity == 0


def test_supported_positive_execution_group_still_fills() -> None:
    overlay, book = _empty_ahead()
    reports = O.drive_batch(
        overlay,
        book,
        [
            O.rec(4, Action.TRADE, side=Side.ASK, quantity=1),
            O.rec(5, Action.FILL, quantity=1, order_id=3),
            O.rec(6, Action.CANCEL, quantity=1, order_id=3),
        ],
    )
    assert [r.kind for r in reports] == [ReportKind.FILL]
    assert overlay.ledger.fills[0].source_group_ordinal == 4
    assert overlay.ledger.fills[0].eligible_volume == 1


@pytest.mark.parametrize("defect", ["no_cancel", "cancel_quantity", "wrong_side", "no_fills"])
def test_bad_execution_group_is_policy_independent_source_corruption(defect: str) -> None:
    trade = O.rec(4, Action.TRADE, side=Side.ASK, quantity=1)
    fill = O.rec(5, Action.FILL, quantity=1, order_id=3)
    cancel = O.rec(6, Action.CANCEL, quantity=1, order_id=3)
    if defect == "cancel_quantity":
        cancel = replace(cancel, quantity=2)
    if defect == "wrong_side":
        trade = replace(trade, side=Side.BID)
    records = [trade, fill, cancel]
    if defect == "no_cancel":
        records.pop()
    if defect == "no_fills":
        records = [trade]
    batch = O.make_batch(records)
    evidence = execution_groups(batch)
    assert evidence.invalid
    assert not evidence.allocations
    quality = QualityMap()
    quality.note_batch(batch, anomaly_added=False)
    assert quality.is_unevaluable(QualityWindow(0, 2000)) == (True, "INVALID_EXECUTION_EVIDENCE")


def test_through_allocation_that_skips_ahead_is_invalid_source() -> None:
    overlay, book = _empty_ahead()
    # Establish a fresh overlay behind the now-resting order 3; a lower-price
    # group cannot bypass those five historical contracts under direct FIFO.
    overlay = ExchangeOverlay("T2", O.instrument(), 0)
    overlay.on_command(
        O.passive_cmd(side=TaskSide.BUY, limit_price_fixed=O.BID, task_id="T2"), book
    )
    O.drive_batch(overlay, book, [O.rec(4, Action.ADD, price_fixed=O.BID2, quantity=5, order_id=4)])
    records = [
        O.rec(5, Action.TRADE, side=Side.ASK, price_fixed=O.BID2, quantity=1),
        O.rec(6, Action.FILL, price_fixed=O.BID2, quantity=1, order_id=4),
        O.rec(7, Action.CANCEL, price_fixed=O.BID2, quantity=1, order_id=4),
    ]
    # Stronger than an overlay-relative ambiguity: a surviving better historical
    # price makes this source allocation invalid independently of virtual position.
    assert not valid_resting_allocation(records[1], book)
    quality = QualityMap()
    quality.note_batch(O.make_batch(records), False, invalid_allocation=True)
    assert quality.is_unevaluable(QualityWindow(0, 2000)) == (True, "INVALID_EXECUTION_EVIDENCE")
    reports = O.drive_batch(overlay, book, records)
    assert reports == []
    assert not overlay.ledger.entries


@pytest.mark.parametrize("side", [TaskSide.BUY, TaskSide.SELL])
def test_better_historical_price_prevents_fill_even_with_no_own_queue_ahead(side: TaskSide) -> None:
    book = ReferenceBook(O.instrument())
    book.set_status(TradingStatus.TRADING)
    limit = O.BID if side is TaskSide.BUY else O.BID + 4 * O.TICK
    opposite_price = O.BID + 4 * O.TICK if side is TaskSide.BUY else O.BID
    resting_side = side.book_side
    O.seed_book(
        book,
        [
            O.rec(0, Action.ADD, side=resting_side, price_fixed=limit, quantity=5, order_id=1),
            O.rec(
                1,
                Action.ADD,
                side=resting_side.opposite(),
                price_fixed=opposite_price,
                quantity=5,
                order_id=2,
            ),
        ],
    )
    overlay = ExchangeOverlay("T1", O.instrument(), 0)
    overlay.on_command(O.passive_cmd(side=side, limit_price_fixed=limit), book)
    better = limit + int(side) * O.TICK
    through = limit - int(side) * O.TICK
    O.drive_batch(
        overlay,
        book,
        [
            O.rec(2, Action.CANCEL, side=resting_side, price_fixed=limit, quantity=5, order_id=1),
            O.rec(3, Action.ADD, side=resting_side, price_fixed=better, quantity=5, order_id=3),
            O.rec(4, Action.ADD, side=resting_side, price_fixed=through, quantity=5, order_id=4),
        ],
    )
    assert overlay.true_queue_ahead() == 0
    reports = O.drive_batch(
        overlay,
        book,
        [
            O.rec(5, Action.TRADE, side=resting_side.opposite(), price_fixed=through, quantity=1),
            O.rec(6, Action.FILL, side=resting_side, price_fixed=through, quantity=1, order_id=4),
            O.rec(7, Action.CANCEL, side=resting_side, price_fixed=through, quantity=1, order_id=4),
        ],
    )
    assert reports == []
    assert overlay.executed_quantity == 0


@pytest.mark.parametrize("side", [TaskSide.BUY, TaskSide.SELL])
def test_skipped_historical_price_is_common_technical_exclusion(
    tmp_path: Path, side: TaskSide
) -> None:
    b, arrival = _session(f"invalid-price-priority-{side.name}")
    through = H.BID2 if side is TaskSide.BUY else H.ASK2
    resting_side = side.book_side
    b.add_event(
        arrival + 600 * H.MS,
        [
            H.RecordSpec(Action.ADD, resting_side, through, 5, 3),
            H.RecordSpec(Action.TRADE, resting_side.opposite(), through, 1, 0),
            H.RecordSpec(Action.FILL, resting_side, through, 1, 3),
            H.RecordSpec(Action.CANCEL, resting_side, through, 1, 3),
        ],
    )
    outputs = SessionEngine(
        b.write(tmp_path), ExperimentConfig("invalid-source-priority"), [B0Policy(), B1Policy()]
    ).run()
    assert set(outputs.task_results["status"]) == {"TECHNICALLY_UNEVALUABLE"}
    assert set(outputs.quality["reason"]) == {"INVALID_EXECUTION_EVIDENCE"}


def test_technical_tasks_cannot_pass_support_gate(tmp_path: Path) -> None:
    b, arrival = _session("phantom-support")
    b.add_event(
        arrival + 100 * H.MS,
        [
            H.RecordSpec(Action.ADD, Side.BID, H.BID, 5, 3),
            H.RecordSpec(Action.CANCEL, Side.BID, H.BID, 5, 1),
        ],
    )
    b.add_event(
        arrival + 600 * H.MS,
        [
            H.RecordSpec(Action.TRADE, Side.ASK, H.BID, 1, 0),
            H.RecordSpec(Action.FILL, Side.BID, H.BID, 1, 3),
            H.RecordSpec(Action.CANCEL, Side.BID, H.BID, 1, 3),
        ],
    )
    # The whole task window is invalid even though a valid fill happened before
    # this corruption; policy-dependent early completion cannot erase the defect.
    b.add_event(arrival + 800 * H.MS, [H.RecordSpec(Action.CANCEL, Side.BID, H.BID2, 1, 999)])
    outputs = SessionEngine(
        b.write(tmp_path), ExperimentConfig("phantom-support"), [B1Policy()]
    ).run()
    assert set(outputs.task_results["status"]) == {"TECHNICALLY_UNEVALUABLE"}
    assert outputs.support.height == 1  # retain the pilot's explicit zero-count row
    gate = select_support_horizon(
        {H.S: outputs.support},
        [b.session_id],
        support_min_eligible=1,
        support_min_queue_depletion=1,
    )
    assert gate.verdict == "FAILED", outputs.support.to_dicts()
    counters = (
        "decision_eligible_checkpoints",
        "post_checkpoint_passive_fills",
        "post_checkpoint_queue_depletion",
        "post_checkpoint_trade_through",
        "fills_before_checkpoint",
    )
    assert all(outputs.support[name][0] == 0 for name in counters)
