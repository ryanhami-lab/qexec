"""Task manifest construction and the policy-independent quality map (engine spec section 3).

:func:`build_task_manifest` produces the policy-independent :class:`TaskManifest` for a session:

* **Arrival grid.** ``first = session_start + warmup_ns + phase`` where ``phase`` is a
  deterministic draw ``numpy.random.default_rng(seed).integers(0, spacing)`` seeded from
  ``(config.task_seed, session_id)`` and ``spacing = arrival_spacing_ns(H)``. Arrivals continue
  while ``arrival + H + drain_margin <= session_end`` with ``drain_margin = 1 s``.
* **Both sides.** Each arrival yields a BUY and a SELL task (separate worlds), with
  ``task_id = f"{session_id}:{k:05d}:{BUY|SELL}"``.
* **Benchmark ``m0``.** The committed historical-book ``Mid2`` at arrival (last batch with proxy
  ``<= arrival``). Eligibility requires ``TRADING`` and a valid uncrossed two-sided book.
* **Client eligibility.** A valid client mid at arrival in **every** planned scenario (so one
  manifest serves all scenarios). The client book for scenario ``L`` observes each batch at
  ``capture_complete_time + L.added_delivery_ns``.
* **Manifest id.** ``task_manifest_id`` = sha256 of the canonical task tuple list.

The historical replay here also builds the policy-independent :class:`~qexec.sim.quality.QualityMap`
(returned alongside the manifest), computed before any policy runs.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from qexec.adapters.batches import iter_session_batches
from qexec.core.config import (
    LATENCY_SCENARIOS,
    ExperimentConfig,
    LatencyScenario,
    arrival_spacing_ns,
)
from qexec.core.records_io import read_instrument, read_meta, read_status
from qexec.core.tasks import Task
from qexec.core.types import Action, EventBatch, StatusEvent, TaskSide, TimeNs, TradingStatus
from qexec.reference.book import ReferenceBook
from qexec.sim.evidence import valid_resting_allocation
from qexec.sim.quality import QualityMap, QualityWindow, anomaly_count

ELIGIBILITY_RULE_VERSION = "v2"
DRAIN_MARGIN_NS = 1_000_000_000  # 1 s (engine spec section 3)


@dataclass(frozen=True, slots=True)
class IneligibleArrival:
    """A grid arrival/side excluded from the manifest, with the reason."""

    arrival_ns: int
    side: TaskSide
    reason: str


@dataclass(frozen=True, slots=True)
class TaskManifest:
    """The policy-independent task manifest plus the quality map for the session."""

    tasks: tuple[Task, ...]
    ineligible: tuple[IneligibleArrival, ...]
    quality: QualityMap
    task_manifest_id: str
    session_id: str


def _phase_ns(task_seed: int, session_id: str, spacing_ns: int) -> int:
    """Deterministic daily phase in ``[0, spacing)`` seeded from (task_seed, session_id)."""
    # Mix the session id into the seed deterministically (stable across platforms).
    digest = hashlib.sha256(f"{task_seed}:{session_id}".encode()).digest()
    seed_int = int.from_bytes(digest[:8], "big")
    rng = np.random.default_rng(seed_int)
    return int(rng.integers(0, spacing_ns))


class _ManifestBuilder:
    """Single historical pass that computes arrival m0, client mids, and the quality map."""

    def __init__(
        self,
        session_dir: Path,
        config: ExperimentConfig,
        planned_scenarios: tuple[LatencyScenario, ...],
    ) -> None:
        self._dir = session_dir
        self._config = config
        self._scenarios = planned_scenarios
        self._instrument = read_instrument(session_dir)
        self._meta = read_meta(session_dir)
        self._status = sorted(read_status(session_dir), key=lambda s: int(s.event_time_ns))

    def build(self) -> TaskManifest:
        config = self._config
        horizon = config.horizon_ns
        spacing = _arrival_spacing(horizon)
        session_start = self._meta.start_ns
        session_end = self._meta.end_ns
        phase = _phase_ns(config.task_seed, self._meta.session_id, spacing)
        first = session_start + config.warmup_ns + phase

        arrivals: list[int] = []
        k = 0
        a = first
        while a + horizon + DRAIN_MARGIN_NS <= session_end:
            arrivals.append(a)
            k += 1
            a = first + k * spacing
        if not arrivals:
            return TaskManifest((), (), QualityMap(), _manifest_id(()), self._meta.session_id)

        hist_m0, client_mids, quality = self._replay(arrivals)

        tasks: list[Task] = []
        ineligible: list[IneligibleArrival] = []
        canonical: list[tuple[str, ...]] = []

        for idx, arrival in enumerate(arrivals):
            m0 = hist_m0[idx]
            reason = None
            if m0 is None:
                reason = "NO_VALID_HISTORICAL_MID"
            else:
                for scenario in self._scenarios:
                    if client_mids[scenario.scenario_id][idx] is None:
                        reason = f"NO_CLIENT_MID[{scenario.scenario_id}]"
                        break
            for side in config.sides:
                if reason is not None:
                    ineligible.append(IneligibleArrival(arrival, side, reason))
                    continue
                assert m0 is not None
                task_id = f"{self._meta.session_id}:{idx:05d}:{side.name}"
                canonical.append(
                    (
                        task_id,
                        self._meta.session_id,
                        str(self._instrument.instrument_id),
                        side.name,
                        "1",
                        str(arrival),
                        str(arrival + horizon),
                        str(m0),
                        ELIGIBILITY_RULE_VERSION,
                    )
                )

        manifest_id = _manifest_id(tuple(canonical))
        for row in canonical:
            tasks.append(
                Task(
                    task_id=row[0],
                    session_id=row[1],
                    instrument_id=int(row[2]),
                    side=TaskSide[row[3]],
                    quantity=1,
                    arrival_time_ns=TimeNs(int(row[5])),
                    deadline_ns=TimeNs(int(row[6])),
                    arrival_reference_mid2=int(row[7]),
                    eligibility_rule_version=ELIGIBILITY_RULE_VERSION,
                    task_manifest_id=manifest_id,
                )
            )
        return TaskManifest(
            tuple(tasks), tuple(ineligible), quality, manifest_id, self._meta.session_id
        )

    def _replay(
        self, arrivals: list[int]
    ) -> tuple[list[int | None], dict[str, list[int | None]], QualityMap]:
        """Replay exchange and client state independently, including quiet-gap statuses.

        Added delivery latency shifts the entire captured market/status stream uniformly,
        so a single client replay serves all scenarios by querying ``arrival - delay``.
        Never reuse a historical midpoint as a client snapshot: delayed status and
        out-of-order captures can make the two books differ.
        """
        hist_book = ReferenceBook(self._instrument)
        hist_book.set_status(TradingStatus.TRADING)
        quality = QualityMap()
        status_idx = 0
        hist_samples: list[tuple[int, int | None]] = []
        # Match SessionEngine scheduling: statuses are enqueued before batches; equal
        # status captures retain exchange-time order, and equal batch captures retain
        # source order. The payload is never compared by sort().
        client_events: list[tuple[int, int, int, StatusEvent | EventBatch]] = [
            (int(st.capture_time_ns), 0, i, st) for i, st in enumerate(self._status)
        ]
        for batch_seq, batch in enumerate(iter_session_batches(self._dir)):
            proxy = int(batch.exchange_proxy_time)
            while (
                status_idx < len(self._status)
                and int(self._status[status_idx].event_time_ns) <= proxy
            ):
                st = self._status[status_idx]
                hist_book.set_status(st.status)
                hist_samples.append((int(st.event_time_ns), _committed_mid2(hist_book)))
                status_idx += 1
            before = anomaly_count(hist_book)
            hist_book.begin_batch(batch)
            invalid_allocation = False
            for record in batch.records:
                if record.action is Action.FILL:
                    invalid_allocation |= (
                        hist_book.status is not TradingStatus.TRADING
                        or not valid_resting_allocation(record, hist_book)
                    )
                hist_book.apply_record(record)
            hist_book.commit_batch(batch)
            quality.note_batch(
                batch, anomaly_count(hist_book) > before, invalid_allocation=invalid_allocation
            )
            # Initialization creates valid committed state even though it contributes
            # no flow features. In a quiet session it can be the latest state at arrival.
            hist_samples.append((proxy, _committed_mid2(hist_book)))
            client_events.append((int(batch.capture_complete_time), 1, batch_seq, batch))

        for st in self._status[status_idx:]:
            hist_book.set_status(st.status)
            hist_samples.append((int(st.event_time_ns), _committed_mid2(hist_book)))
        quality.finalize(self._meta.end_ns)

        client_book = ReferenceBook(self._instrument)
        client_book.set_status(TradingStatus.TRADING)
        client_samples: list[tuple[int, int | None]] = []
        for capture, _, _, payload in sorted(client_events, key=lambda event: event[:3]):
            if isinstance(payload, StatusEvent):
                client_book.set_status(payload.status)
            else:
                client_book.begin_batch(payload)
                for record in payload.records:
                    client_book.apply_record(record)
                client_book.commit_batch(payload)
            client_samples.append((capture, _committed_mid2(client_book)))

        hist_m0 = _sample_mids(hist_samples, arrivals)
        client_mids = {
            scenario.scenario_id: _sample_mids(
                client_samples, [arrival - scenario.added_delivery_ns for arrival in arrivals]
            )
            for scenario in self._scenarios
        }
        return hist_m0, client_mids, quality


def _sample_mids(samples: list[tuple[int, int | None]], times: list[int]) -> list[int | None]:
    """Last committed state at each query, preserving all same-time causal microsteps."""
    samples.sort(key=lambda sample: sample[0])
    sample_times = [time for time, _ in samples]
    values = [mid for _, mid in samples]
    indices = [bisect.bisect_right(sample_times, time) - 1 for time in times]
    return [values[index] if index >= 0 else None for index in indices]


def _arrival_spacing(horizon_ns: int) -> int:
    return arrival_spacing_ns(horizon_ns)


def _committed_mid2(book: ReferenceBook) -> int | None:
    """Committed Mid2 from best bid/ask: valid only while TRADING and two-sided/uncrossed.

    Matches :meth:`qexec.core.types.BookSnapshot.mid2` semantics without building a full
    snapshot (``best_bid_ask`` is O(1); the full commit snapshot is avoided for performance).
    """
    if book.status is not TradingStatus.TRADING:
        return None
    bid, ask = book.best_bid_ask()
    if bid is None or ask is None or bid.price_fixed >= ask.price_fixed:
        return None
    return bid.price_fixed + ask.price_fixed


def _manifest_id(canonical: tuple[tuple[str, ...], ...]) -> str:
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def build_task_manifest(
    session_dir: Path,
    config: ExperimentConfig,
    planned_scenarios: tuple[LatencyScenario, ...] | None = None,
) -> TaskManifest:
    """Build the policy-independent task manifest and quality map for ``session_dir``.

    ``planned_scenarios`` defaults to all :data:`~qexec.core.config.LATENCY_SCENARIOS` so the
    manifest's client-eligibility check serves every scenario; pass a subset to restrict it.
    """
    if planned_scenarios is None:
        planned_scenarios = (
            tuple(LATENCY_SCENARIOS[s] for s in config.planned_scenario_ids)
            if config.planned_scenario_ids is not None
            else tuple(LATENCY_SCENARIOS.values())
        )
    config.validate()
    return _ManifestBuilder(session_dir, config, planned_scenarios).build()


def quality_window(task: Task) -> QualityWindow:
    """The evaluation window ``[arrival, deadline]`` of ``task`` for the quality map."""
    return QualityWindow(int(task.arrival_time_ns), int(task.deadline_ns))
