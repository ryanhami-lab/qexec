"""Shared builders for WP2-ENGINE tests: hand-built micro-sessions and configs.

These helpers write a fully controlled session directory to ``tmp_path`` via
:func:`qexec.core.records_io.write_session`, so each test owns every record and timestamp.
Expected engine times are hand-derived in the tests themselves (e.g. with L1 ``c=50us, e=250us,
r=250us`` a JOIN at arrival ``t0`` arrives at ``t0 + 300us``; a fill at exchange time ``x`` is
reported at ``x + 250us``; the terminal cutoff is ``T - 1.400001ms``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from qexec.core.records_io import SessionMeta, write_session
from qexec.core.types import (
    Action,
    CanonicalRecord,
    InstrumentDefinition,
    PriceFixed,
    RecordFlag,
    Side,
    StatusEvent,
    TimeNs,
    TradingStatus,
)

TICK = 250_000_000  # 0.25 in 1e-9 units
INSTRUMENT_ID = 1

# A canonical book around 100.00 / 100.25 (one tick wide).
BID = 100_000_000_000  # 100.00
BID2 = BID - TICK  # 99.75
ASK = 100_250_000_000  # 100.25
ASK2 = ASK + TICK  # 100.50

US = 1_000
MS = 1_000_000
S = 1_000_000_000


def instrument(instrument_id: int = INSTRUMENT_ID) -> InstrumentDefinition:
    return InstrumentDefinition(
        instrument_id=instrument_id,
        symbol="SYNZ6",
        tick_size_fixed=PriceFixed(TICK),
        multiplier=50,
        effective_time_ns=TimeNs(0),
    )


def first_arrival_ns(
    session_id: str, start_ns: int, warmup_ns: int, horizon_ns: int, task_seed: int = 0
) -> int:
    """Reproduce the documented manifest arrival grid's first arrival for a session.

    ``first = start + warmup + phase``; ``phase = default_rng(seed_from(task_seed, session_id))
    .integers(0, spacing)`` with ``spacing = max(10 s, 2H)``. Tests build their controlled events
    relative to this exact time so the single manifest task lands where they expect.
    """
    spacing = max(10 * S, 2 * horizon_ns)
    digest = hashlib.sha256(f"{task_seed}:{session_id}".encode()).digest()
    seed_int = int.from_bytes(digest[:8], "big")
    phase = int(np.random.default_rng(seed_int).integers(0, spacing))
    return start_ns + warmup_ns + phase


@dataclass(slots=True)
class RecordSpec:
    """A compact record specification for a micro-session event."""

    action: Action
    side: Side
    price_fixed: int
    quantity: int
    order_id: int
    flags: RecordFlag = RecordFlag.NONE


def sell_through_bid_records() -> list[RecordSpec]:
    """Cancel the seeded 50 at BID, then execute one real contract at BID2.

    Used by timing fixtures: cancellation alone never fills; the fully linked direct
    sell group supports the hypothetical buy at its original BID limit.
    """
    return [
        RecordSpec(Action.CANCEL, Side.BID, BID, 50, 1),
        RecordSpec(Action.TRADE, Side.ASK, BID2, 1, 0),
        RecordSpec(Action.FILL, Side.BID, BID2, 1, 2),
        RecordSpec(Action.CANCEL, Side.BID, BID2, 1, 2),
    ]


@dataclass(slots=True)
class EventSpec:
    """One source event (a run of records closed by LAST) at a single event/capture time."""

    event_time_ns: int
    records: list[RecordSpec]
    capture_time_ns: int | None = None


@dataclass(slots=True)
class SessionBuilder:
    """Accumulates events and status transitions into an immutable session directory."""

    session_id: str
    start_ns: int
    events: list[EventSpec] = field(default_factory=list)
    status: list[StatusEvent] = field(default_factory=list)
    _ordinal: int = 0

    def add_event(
        self, event_time_ns: int, records: list[RecordSpec], capture_time_ns: int | None = None
    ) -> SessionBuilder:
        self.events.append(EventSpec(event_time_ns, records, capture_time_ns))
        return self

    def snapshot(
        self, event_time_ns: int, bids: list[tuple[int, int, int]], asks: list[tuple[int, int, int]]
    ) -> SessionBuilder:
        """Add an opening SNAPSHOT event. Each level is ``(price_fixed, quantity, order_id)``."""
        recs: list[RecordSpec] = []
        for price, qty, oid in bids:
            recs.append(RecordSpec(Action.ADD, Side.BID, price, qty, oid, RecordFlag.SNAPSHOT))
        for price, qty, oid in asks:
            recs.append(RecordSpec(Action.ADD, Side.ASK, price, qty, oid, RecordFlag.SNAPSHOT))
        return self.add_event(event_time_ns, recs, capture_time_ns=event_time_ns)

    def add_status(
        self, status: TradingStatus, event_time_ns: int, capture_time_ns: int
    ) -> SessionBuilder:
        self.status.append(
            StatusEvent(INSTRUMENT_ID, status, TimeNs(event_time_ns), TimeNs(capture_time_ns))
        )
        return self

    def write(self, tmp_path: Path) -> Path:
        records: list[CanonicalRecord] = []
        # Emit events in nondecreasing event-time order (stable: preserves insertion order for
        # equal times), so the assembled batches have nondecreasing exchange proxy times.
        ordered = sorted(self.events, key=lambda e: e.event_time_ns)
        for ev in ordered:
            n = len(ev.records)
            cap = ev.capture_time_ns if ev.capture_time_ns is not None else ev.event_time_ns
            for i, r in enumerate(ev.records):
                flags = r.flags
                if i == n - 1:
                    flags |= RecordFlag.LAST
                records.append(
                    CanonicalRecord(
                        dataset_id="SYNTH.MBO",
                        publisher_id=1,
                        instrument_id=INSTRUMENT_ID,
                        session_epoch=0,
                        source_ordinal=self._ordinal,
                        action=r.action,
                        side=r.side,
                        price_fixed=r.price_fixed,
                        quantity=r.quantity,
                        order_id=r.order_id,
                        event_time_ns=TimeNs(ev.event_time_ns),
                        capture_time_ns=TimeNs(cap),
                        flags=flags,
                        source_file_hash="",
                        source_record_offset=self._ordinal,
                    )
                )
                self._ordinal += 1
        end_ns = records[-1].event_time_ns if records else self.start_ns
        meta = SessionMeta(
            session_id=self.session_id,
            dataset_id="SYNTH.MBO",
            start_ns=self.start_ns,
            end_ns=int(end_ns),
            params={},
        )
        directory = tmp_path / self.session_id
        write_session(directory, meta, instrument(), records, self.status)
        return directory
