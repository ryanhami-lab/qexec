"""R2 (docs/remediation.md): atomic-batch effective time and the scheduler past-event guard.

Defect: overlay record-driven fills were stamped with the triggering record's ``event_time_ns``
while the whole batch is processed atomically at the batch ``exchange_proxy_time`` (= max event
time). A fill driven by an *earlier* record in the batch therefore got an exchange time strictly
before the batch commit's logical time, so its report delivery (``fill + response``) was
scheduled into the past.

Required fix (remediation R2):

* Overlay execution effective time for record-driven fills = the batch's ``exchange_proxy_time``
  (atomic convention). The engine stamps record-driven fills at the batch proxy time.
* The scheduler asserts no event is scheduled before the current time.

Reproduction (remediation R2): a batch whose records are at ``+200us`` and ``+500us`` (proxy
``+500us``) with the passive command arriving at ``+300us``. The trade-through is the ``+200us``
record; its fill must be stamped at ``+500us`` (the batch proxy), which is ``>=`` the ``+300us``
arrival.

Timings are hand-derived for L1 (``c=50us, e=250us, r=250us``): a JOIN at arrival ``t0`` arrives
at ``t0+300us``; a report at exchange time ``x`` is delivered at ``x+250us``.
"""

from __future__ import annotations

from pathlib import Path

import _engine_helpers as H  # noqa: N812  (conventional micro-session helper alias)
import pytest

from qexec.core.config import ExperimentConfig
from qexec.core.records_io import SessionMeta, write_session
from qexec.core.types import (
    Action,
    CanonicalRecord,
    RecordFlag,
    Side,
    StatusEvent,
    TimeNs,
    TradingStatus,
)
from qexec.policies.baselines import B1Policy
from qexec.sim.engine import SessionEngine
from qexec.sim.scheduler import EventClass, Scheduler

START = 1_000_000_000_000
WARMUP = 1_000_000_000
HORIZON = 1_000_000_000


def _cfg() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="r2", horizon_ns=HORIZON, latency_id="L1")


def test_scheduler_rejects_event_in_the_past() -> None:
    """The scheduler refuses an event scheduled strictly before the current logical time."""
    sched: Scheduler[str] = Scheduler()
    sched.schedule(500, EventClass.CLIENT_DELIVERY, "a")
    popped = sched.pop()  # advances current time to 500
    assert popped.time_ns == 500
    assert sched.current_time_ns == 500
    # A zero-delay descendant at exactly the current time is allowed.
    sched.schedule(500, EventClass.CLIENT_DELIVERY, "now")
    # A strictly earlier time is a past event -> rejected.
    with pytest.raises(ValueError, match="before the current time"):
        sched.schedule(499, EventClass.CLIENT_DELIVERY, "past")


def _write_split_time_batch_session(tmp_path: Path, sid: str, t0: int) -> Path:
    """Build a session whose fill-driving batch has records at ``t0+300us+200us`` and
    ``...+500us`` inside ONE batch (one LAST), so the batch proxy time is the later record.

    A passive buy JOIN at ``t0`` arrives at the exchange at ``t0+300us``. The fill-driving batch
    opens its first (trade-through) record at ``arrival+200us`` and closes (LAST) at
    ``arrival+500us``, so the whole batch commits at proxy ``arrival+500us``. The trade-through
    is at the earlier record time; before the R2 fix the fill would be stamped at
    ``arrival+200us`` and its report delivery scheduled at ``arrival+450us`` < the batch commit
    time ``arrival+500us`` (a past event).
    """
    arrival = t0 + 300 * H.US
    r1 = arrival + 200 * H.US  # earlier record (the trade-through)
    r2 = arrival + 500 * H.US  # later record -> batch proxy time
    deadline = t0 + HORIZON

    recs: list[CanonicalRecord] = []
    ordinal = 0

    def add(
        action: Action,
        side: Side,
        price: int,
        qty: int,
        oid: int,
        event_t: int,
        flags: RecordFlag = RecordFlag.NONE,
    ) -> None:
        nonlocal ordinal
        recs.append(
            CanonicalRecord(
                dataset_id="SYNTH.MBO",
                publisher_id=1,
                instrument_id=H.INSTRUMENT_ID,
                session_epoch=0,
                source_ordinal=ordinal,
                action=action,
                side=side,
                price_fixed=price,
                quantity=qty,
                order_id=oid,
                event_time_ns=TimeNs(event_t),
                capture_time_ns=TimeNs(event_t),
                flags=flags,
                source_file_hash="",
                source_record_offset=ordinal,
            )
        )
        ordinal += 1

    # Opening snapshot (its own INITIALIZATION batch, LAST on the last record).
    add(Action.ADD, Side.BID, H.BID, 50, 1, START, RecordFlag.SNAPSHOT)
    add(Action.ADD, Side.BID, H.BID2, 50, 2, START, RecordFlag.SNAPSHOT)
    add(Action.ADD, Side.ASK, H.ASK, 50, 3, START, RecordFlag.SNAPSHOT)
    add(Action.ADD, Side.ASK, H.ASK2, 50, 4, START, RecordFlag.SNAPSHOT | RecordFlag.LAST)

    # A sparse harmless deep feed keeps committed mids valid at arrival and deadline, each its
    # own single-record batch (LAST each). Stop before the fill batch so ordering is clean.
    deep = H.BID2 - 10 * H.TICK
    t = START + 10 * H.MS
    oid = 5000
    while t < r1 - H.MS:
        add(Action.ADD, Side.BID, deep, 1, oid, t, RecordFlag.LAST)
        add(Action.CANCEL, Side.BID, deep, 1, oid, t + H.US, RecordFlag.LAST)
        oid += 1
        t += 100 * H.MS

    # The fill-driving batch: a trade-through at r1 (earlier) and a harmless deep ADD closing the
    # batch (LAST) at r2 (later). Both are in ONE batch (only the r2 record carries LAST).
    for record in H.sell_through_bid_records():
        add(record.action, record.side, record.price_fixed, record.quantity, record.order_id, r1)
    add(Action.ADD, Side.BID, deep, 1, 99000, r2, RecordFlag.LAST)  # LAST -> proxy = r2

    # Keep the feed alive to the deadline so the deadline m_T is valid.
    t = r2 + 100 * H.MS
    while t < deadline + 2 * H.S:
        add(Action.ADD, Side.BID, deep, 1, oid, t, RecordFlag.LAST)
        add(Action.CANCEL, Side.BID, deep, 1, oid, t + H.US, RecordFlag.LAST)
        oid += 1
        t += 100 * H.MS

    meta = SessionMeta(
        session_id=sid,
        dataset_id="SYNTH.MBO",
        start_ns=START,
        end_ns=int(recs[-1].event_time_ns),
        params={},
    )
    status = [StatusEvent(H.INSTRUMENT_ID, TradingStatus.TRADING, TimeNs(START), TimeNs(START))]
    directory = tmp_path / sid
    write_session(directory, meta, H.instrument(), recs, status)
    return directory


def test_record_driven_fill_stamped_at_batch_proxy_time(tmp_path: Path) -> None:
    """The record-driven trade-through fill is stamped at the batch proxy time (``arrival+500us``),
    not the earlier record time (``arrival+200us``), so its report delivery is not in the past."""
    sid = "SYN-R2"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _write_split_time_batch_session(tmp_path, sid, t0)
    arrival = t0 + 300 * H.US
    proxy = arrival + 500 * H.US

    # Before the fix this raises in the scheduler (report delivery scheduled into the past).
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()

    wid = f"{sid}:00000:BUY|B1"
    ex = out.executions.filter(out.executions["world_id"] == wid)
    assert ex.height == 1
    assert ex["mechanism"][0] == "TRADE_THROUGH"
    # The fill exchange time is the batch proxy time (atomic convention), >= command arrival.
    assert ex["exchange_time_ns"][0] == proxy
    assert ex["exchange_time_ns"][0] >= arrival

    fill_report = out.reports.filter(
        (out.reports["world_id"] == wid) & (out.reports["report_kind"] == "FILL")
    ).row(0, named=True)
    assert fill_report["exchange_time_ns"] == proxy
    assert fill_report["delivery_time_ns"] == proxy + 250 * H.US
