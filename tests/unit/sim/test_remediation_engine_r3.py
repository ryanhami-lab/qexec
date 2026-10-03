"""R3 (docs/remediation.md): client book status changes ONLY via delayed status observations.

Defect: the client book's trading status was set from the exchange-plane status attached to each
market delivery, so a halt that was effective on the exchange leaked into the client book as soon
as *any* book batch was delivered -- before the client's delayed status *observation* arrived.

Required fix (remediation R3): market deliveries never set client status; the client book's
status changes only at the dedicated delayed status-observation events
(``capture_time_ns + added_delivery_ns``). The historical book's status timing is unchanged
(effective at ``event_time_ns``).

Reproduction (remediation R3): a halt effective at ``+100ms`` but observed only at ``+800ms``,
with a book batch delivered at ``+200ms``; the client view at ``+500ms`` must still read
``TRADING``. All times are relative to the single manifest arrival ``t0`` (L1, so the client
observation delay ``added_delivery_ns = 0`` and the observation time equals the capture time).
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
    return ExperimentConfig(experiment_id="r3", horizon_ns=HORIZON, latency_id="L1")


def _session(tmp_path: Path, sid: str, t0: int) -> Path:
    """A one-tick book with a halt effective at t0+100ms but captured (observed) only at
    t0+800ms. A book batch is delivered at t0+200ms (its capture is at t0+200ms). The sparse
    deep feed keeps committed mids valid at arrival and the deadline."""
    deadline = t0 + HORIZON
    b = H.SessionBuilder(sid, START)
    b.add_status(TradingStatus.TRADING, START, START)
    b.snapshot(
        START,
        bids=[(H.BID, 50, 1), (H.BID2, 50, 2)],
        asks=[(H.ASK, 50, 3), (H.ASK2, 50, 4)],
    )
    # Halt effective (historical) at t0+100ms, but its observation is captured only at t0+800ms
    # (capture >= event is allowed): the client should not observe it until t0+800ms.
    b.add_status(TradingStatus.HALTED, t0 + 100 * H.MS, t0 + 800 * H.MS)
    # A book batch whose capture completes at t0+200ms (delivered at t0+200ms under L1): it must
    # NOT carry the halt into the client book.
    deep = H.BID2 - 10 * H.TICK
    b.add_event(
        t0 + 200 * H.MS,
        [H.RecordSpec(Action.ADD, Side.BID, deep, 1, 7001)],
        capture_time_ns=t0 + 200 * H.MS,
    )
    b.add_event(
        t0 + 200 * H.MS + H.US,
        [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, 7001)],
        capture_time_ns=t0 + 200 * H.MS + H.US,
    )
    # Keep mids valid before arrival and after the deadline.
    t = START + 10 * H.MS
    oid = 5000
    while t < deadline + 2 * H.S:
        if abs(t - (t0 + 200 * H.MS)) > 10 * H.MS:
            b.add_event(t, [H.RecordSpec(Action.ADD, Side.BID, deep, 1, oid)])
            b.add_event(t + H.US, [H.RecordSpec(Action.CANCEL, Side.BID, deep, 1, oid)])
            oid += 1
        t += 100 * H.MS
    return b.write(tmp_path)


def test_client_view_at_500ms_shows_trading_despite_effective_halt(tmp_path: Path) -> None:
    """The checkpoint view at t0+500ms reads TRADING: the halt (effective t0+100ms) is observed
    only at t0+800ms, so no delivered market batch may have leaked it into the client book."""
    sid = "SYN-R3"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _session(tmp_path, sid, t0)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    out = eng.run()

    # The checkpoint fires at t0 + H/2 = t0 + 500ms; its trace records the client status seen.
    tid = f"{sid}:00000:BUY"
    trace = out.trace(tid, "B1")
    checkpoint = next(e for e in trace if e["kind"] == "checkpoint")
    assert checkpoint["time_ns"] == t0 + 500 * H.MS
    assert checkpoint["client_status"] == "TRADING"


def test_client_observes_halt_only_after_delayed_observation(tmp_path: Path) -> None:
    """After the delayed observation at t0+800ms the client book is HALTED; the historical book's
    status timing is unchanged (effective at t0+100ms, visible in the committed mid series)."""
    sid = "SYN-R3B"
    t0 = H.first_arrival_ns(sid, START, WARMUP, HORIZON)
    sd = _session(tmp_path, sid, t0)
    eng = SessionEngine(sd, _cfg(), [B1Policy()], scenario_id="L1")
    eng.run()

    # Client plane: still TRADING at t0+500ms (checked above); HALTED after t0+800ms.
    # We assert the final client-book status is HALTED (observation at t0+800ms applied).
    assert eng._client_book.status is TradingStatus.HALTED

    # Historical plane unchanged: the committed historical mid is None (halted) for samples with
    # proxy time at/after the effective halt t0+100ms, and valid before it.
    assert eng._mid_series is not None
    before = eng._mid_series.mid_at(t0 + 50 * H.MS)
    after = eng._mid_series.mid_at(t0 + 150 * H.MS)
    assert before is not None  # TRADING before the effective halt
    assert after is None  # halted on the historical book at its effective time (unchanged)
