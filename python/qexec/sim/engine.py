"""Event-driven single-session replay engine (engine spec sections 1-9, architecture 6-9).

:class:`SessionEngine` runs **one pass per (session, latency scenario)**. The historical stream is
read once; a single historical :class:`~qexec.reference.book.ReferenceBook` applies every batch. A
single client book (a separate ``ReferenceBook``) and one
:class:`~qexec.features.market.MarketFeatureState` observe each batch at its delivery time
``capture_complete_time + added_delivery_ns``. Many independent :class:`~qexec.sim.world.World`
objects (one per task x policy, plus label-probe branches) share these read-only books; each
world owns its overlay, OMS, controller, timers, and queue proxy. Only *active* worlds receive
callbacks.

Event ordering (contract 2.2 / architecture 6.3) is enforced by
:class:`~qexec.sim.scheduler.Scheduler` keying ``(time_ns, event_class, seq)``:

0. historical batch commit,
1. exchange command arrival,
2. client delivery (market observation / status observation / report delivery),
3. timers (arrival, checkpoint, cutoff, deadline) and policy decisions.

``seq`` is a global monotone counter assigned at schedule time, so a zero-delay descendant is
always ordered after its cause (causal microstep).

:meth:`SessionEngine.run` returns a :class:`SessionOutputs` of polars DataFrames plus a
:meth:`SessionOutputs.trace` for one world's ordered causal event trace.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import polars as pl

from qexec.adapters.batches import iter_session_batches
from qexec.analysis.metrics import (
    deadline_value_ticks,
    implementation_shortfall_ticks,
)
from qexec.core.config import LATENCY_SCENARIOS, ExperimentConfig, LatencyScenario
from qexec.core.messages import (
    CommandKind,
    ExchangeCommand,
    ExchangeReport,
)
from qexec.core.records_io import read_instrument, read_meta, read_status
from qexec.core.tasks import (
    CheckpointChoice,
    DecisionReason,
    FillMechanism,
    Task,
    TaskStatus,
)
from qexec.core.types import (
    BatchKind,
    EventBatch,
    InstrumentDefinition,
    TaskSide,
    TimeNs,
    TradingStatus,
)
from qexec.core.views import ClientOrderState, DecisionView
from qexec.features.groups import mechanics_features
from qexec.features.market import MarketFeatureState
from qexec.features.queue_proxy import QueueCohortProxy
from qexec.labels.price import MidSeries
from qexec.policies.base import Policy, checkpoint_eligibility
from qexec.policies.baselines import B1_PROBE_POLICY_ID, B1ProbePolicy
from qexec.reference.book import ReferenceBook
from qexec.sim.controller import CommandIntent, IntentKind, TerminalController
from qexec.sim.evidence import execution_groups
from qexec.sim.oms import ClientOMS
from qexec.sim.overlay import ExchangeOverlay
from qexec.sim.quality import QualityWindow
from qexec.sim.scheduler import EventClass, Scheduler
from qexec.sim.tasks import TaskManifest, build_task_manifest
from qexec.sim.world import World, WorldPhase

PriceScorer = Callable[[dict[str, float]], dict[str, float]]


@dataclass(slots=True)
class SessionOutputs:
    """All persisted DataFrames for one (session, scenario) pass, plus the trace index."""

    tasks: pl.DataFrame
    task_results: pl.DataFrame
    decisions: pl.DataFrame
    executions: pl.DataFrame
    reports: pl.DataFrame
    checkpoint_features: pl.DataFrame
    branch_labels: pl.DataFrame
    support: pl.DataFrame
    quality: pl.DataFrame
    _traces: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)

    def trace(self, task_id: str, policy_id: str) -> list[dict[str, Any]]:
        """Return the ordered causal event trace for one world (engine spec section 9)."""
        return list(self._traces.get((task_id, policy_id), []))


# ---------------------------------------------------------------------------
# Event payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BatchCommit:
    batch: EventBatch
    status_at_proxy: TradingStatus
    hist_mid2: int | None


@dataclass(frozen=True, slots=True)
class _ClientDelivery:
    batch: EventBatch
    observation_time_ns: int
    status: TradingStatus


@dataclass(frozen=True, slots=True)
class _HistStatus:
    """A historical-plane trading-status transition, effective at ``event_time_ns``.

    Scheduled as its own class-0 event so a status change (e.g. a HALT) takes effect on the
    historical book at its exact ``event_time_ns`` even when no batch commits between that
    transition and a later command arrival (engine spec section 2). Without this, a command
    arriving in a halt gap would see a stale ``TRADING`` book and fabricate a fill.
    """

    status: TradingStatus


@dataclass(frozen=True, slots=True)
class _ClientStatus:
    """A client-plane trading-status observation, effective at ``capture_time_ns + delivery``.

    Scheduled as its own class-2 event (ordered with other client deliveries) so the client
    book observes a status transition at the correct time independently of batch deliveries
    (engine spec section 2: client status observation ordered with deliveries).
    """

    status: TradingStatus


@dataclass(frozen=True, slots=True)
class _CommandArrival:
    world_id: str
    command: ExchangeCommand


@dataclass(frozen=True, slots=True)
class _ReportDelivery:
    world_id: str
    report: ExchangeReport


@dataclass(frozen=True, slots=True)
class _Timer:
    world_id: str
    kind: str  # "arrival" | "checkpoint" | "cutoff" | "deadline"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class SessionEngine:
    """Single-pass event-driven replay for one session and one latency scenario."""

    def __init__(
        self,
        session_dir: Path,
        config: ExperimentConfig,
        policies: list[Policy],
        *,
        scenario_id: str | None = None,
        fork_label_probe: bool = False,
        price_scorer: PriceScorer | None = None,
        record_checkpoint_features: bool = True,
    ) -> None:
        config.validate()
        self._dir = session_dir
        self._config = config
        self._policies = list(policies)
        self._scenario: LatencyScenario = (
            config.latency if scenario_id is None else _scenario_by_id(scenario_id)
        )
        self._fork_label_probe = fork_label_probe
        self._price_scorer = price_scorer
        self._record_checkpoint_features = record_checkpoint_features

        self._instrument: InstrumentDefinition = read_instrument(session_dir)
        self._meta = read_meta(session_dir)
        self._status = sorted(read_status(session_dir), key=lambda s: int(s.event_time_ns))

        self._sched: Scheduler[Any] = Scheduler()
        self._worlds: dict[str, World] = {}
        # Worlds whose overlay is currently working (receive on_record callbacks).
        self._live_overlays: set[str] = set()
        # Worlds whose queue proxy is still being fed (pre-checkpoint only; perf).
        self._proxy_feeding: set[str] = set()

        # Shared read-only books / feature state (one per scenario).
        self._hist_book = ReferenceBook(self._instrument)
        self._hist_book.set_status(TradingStatus.TRADING)
        self._client_book = ReferenceBook(self._instrument)
        self._client_book.set_status(TradingStatus.TRADING)
        self._market = MarketFeatureState(self._instrument)

        # Delivered-stream bookkeeping for the queue proxy / view construction.
        self._last_delivered_proxy_ns: int = -1
        self._last_delivery_time_ns: int = -1
        self._client_mid2: int | None = None

        # Outcome scaffolding.
        self._manifest: TaskManifest | None = None
        self._mid_series: MidSeries | None = None
        # Committed historical (proxy_time, mid2|None) samples collected during the pass; used to
        # build the MidSeries for m_T without a separate historical replay (perf).
        self._hist_mid_samples: list[tuple[int, int | None]] = []

        # Output row collectors.
        self._decision_rows: list[dict[str, Any]] = []
        self._execution_rows: list[dict[str, Any]] = []
        self._report_rows: list[dict[str, Any]] = []
        self._branch_rows: list[dict[str, Any]] = []
        self._checkpoint_rows: list[dict[str, Any]] = []
        self._fork_counter = 0

    # -- public API ------------------------------------------------------------------

    def run(self) -> SessionOutputs:
        """Execute the single pass and return all outputs."""
        planned_ids = self._config.planned_scenario_ids or (self._scenario.scenario_id,)
        if self._scenario.scenario_id not in planned_ids:
            raise ValueError("active scenario must belong to the declared common task population")
        self._manifest = build_task_manifest(
            self._dir,
            self._config,
            planned_scenarios=tuple(LATENCY_SCENARIOS[s] for s in planned_ids),
        )
        self._build_worlds()
        self._schedule_status()
        self._schedule_batches()
        self._schedule_arrival_timers()
        self._event_loop()
        # Build the committed historical mid series from the samples collected during the pass
        # (avoids a separate full historical replay just for m_T).
        self._mid_series = MidSeries(
            [t for t, _ in self._hist_mid_samples],
            [m for _, m in self._hist_mid_samples],
        )
        return self._assemble_outputs()

    # -- world construction ----------------------------------------------------------

    def _build_worlds(self) -> None:
        assert self._manifest is not None
        config = self._config
        guard = self._scenario.guard_ns
        for task in self._manifest.tasks:
            arrival = int(task.arrival_time_ns)
            deadline = int(task.deadline_ns)
            checkpoint = arrival + config.checkpoint_offset_ns
            cutoff = deadline - guard
            for policy in self._policies:
                self._make_world(task, policy, checkpoint, cutoff, deadline)
            if self._fork_label_probe:
                probe = B1ProbePolicy()
                self._make_world(task, probe, checkpoint, cutoff, deadline, is_probe=True)

    def _make_world(
        self,
        task: Task,
        policy: Policy,
        checkpoint: int,
        cutoff: int,
        deadline: int,
        *,
        is_probe: bool = False,
    ) -> World:
        world_id = f"{task.task_id}|{policy.policy_id}"
        overlay = ExchangeOverlay(
            task.task_id, self._instrument, self._config.fee_per_contract_fixed
        )
        world = World(
            world_id=world_id,
            task=task,
            side=task.side,
            policy=policy,
            overlay=overlay,
            oms=ClientOMS(task.task_id),
            controller=TerminalController(task.task_id),
            checkpoint_ns=checkpoint,
            cutoff_ns=cutoff,
            deadline_ns=deadline,
            is_probe=is_probe,
        )
        self._worlds[world_id] = world
        return world

    def _schedule_arrival_timers(self) -> None:
        for world in self._worlds.values():
            self._sched.schedule(
                int(world.task.arrival_time_ns), EventClass.TIMER, _Timer(world.world_id, "arrival")
            )

    # -- batch / delivery scheduling -------------------------------------------------

    def _schedule_status(self) -> None:
        """Schedule each trading-status transition as its own effective-time event.

        Each :class:`~qexec.core.types.StatusEvent` is scheduled twice:

        * a historical-plane event (class 0) at ``event_time_ns`` that sets the historical
          book's status, and
        * a client-plane event (class 2) at ``capture_time_ns + added_delivery_ns`` that sets
          the client book's status (observation ordered with other client deliveries).

        This makes a status change effective at its exact time independently of batch commits
        (engine spec section 2). Scheduling runs *before* :meth:`_schedule_batches`, so at an
        identical ``(time_ns, class 0)`` a status transition is assigned a lower ``seq`` than any
        batch commit at the same proxy time and therefore applies to the historical book *before*
        that batch (spec: "a status event with event_time_ns <= t applies to the historical book
        before batch t"). Any command arrival (class 1) or decision/timer (class 3) at or after
        a status time observes the updated status, so the overlay never fabricates a fill during
        a halt that opened in a gap with no intervening batch.
        """
        for status_event in self._status:
            self._sched.schedule(
                int(status_event.event_time_ns),
                EventClass.HISTORICAL_BATCH,
                _HistStatus(status_event.status),
            )
            client_obs = int(status_event.capture_time_ns) + self._scenario.added_delivery_ns
            self._sched.schedule(
                client_obs,
                EventClass.CLIENT_DELIVERY,
                _ClientStatus(status_event.status),
            )

    def _schedule_batches(self) -> None:
        """Stream all historical batches and client deliveries into the scheduler up front.

        Historical batch commits are class 0 at ``exchange_proxy_time``; client deliveries are
        class 2 at ``capture_complete_time + added_delivery_ns``. The status at a batch's proxy
        time is precomputed so the commit handler can set both books' status deterministically.
        """
        status_idx = 0
        n_status = len(self._status)
        cur_status = TradingStatus.TRADING
        for batch in iter_session_batches(self._dir):
            proxy = int(batch.exchange_proxy_time)
            while status_idx < n_status and int(self._status[status_idx].event_time_ns) <= proxy:
                cur_status = self._status[status_idx].status
                status_idx += 1
            # Historical mid at this committed batch is computed lazily in the handler.
            self._sched.schedule(
                proxy,
                EventClass.HISTORICAL_BATCH,
                _BatchCommit(batch, cur_status, None),
            )
            obs = int(batch.capture_complete_time) + self._scenario.added_delivery_ns
            self._sched.schedule(
                obs, EventClass.CLIENT_DELIVERY, _ClientDelivery(batch, obs, cur_status)
            )

    # -- event loop ------------------------------------------------------------------

    def _event_loop(self) -> None:
        """Pop events in ``(time_ns, class, seq)`` order and dispatch to handlers."""
        while self._sched:
            event = self._sched.pop()
            payload = event.payload
            now = event.time_ns
            if isinstance(payload, _BatchCommit):
                self._on_batch_commit(payload, now)
            elif isinstance(payload, _HistStatus):
                self._hist_book.set_status(payload.status)
                self._record_historical_mid(now)
            elif isinstance(payload, _ClientStatus):
                self._client_book.set_status(payload.status)
            elif isinstance(payload, _CommandArrival):
                self._on_command_arrival(payload, now)
            elif isinstance(payload, _ClientDelivery):
                self._on_client_delivery(payload, now)
            elif isinstance(payload, _ReportDelivery):
                self._on_report_delivery(payload, now)
            elif isinstance(payload, _Timer):
                self._on_timer(payload, now)
            else:  # pragma: no cover - exhaustive
                raise TypeError(f"unknown event payload {payload!r}")

    # -- historical batch commit (class 0) -------------------------------------------

    def _on_batch_commit(self, payload: _BatchCommit, now: int) -> None:
        batch = payload.batch
        self._hist_book.set_status(payload.status_at_proxy)
        # For every live-overlay world: observe each record BEFORE the book applies it, then
        # apply the record. Finally commit and run on_batch_committed per live world.
        live_ids = [wid for wid in self._live_overlays if self._worlds[wid].active]
        proxy_time = int(batch.exchange_proxy_time)
        evidence = execution_groups(batch)
        self._hist_book.begin_batch(batch)
        for record in batch.records:
            for wid in list(live_ids):
                world = self._worlds[wid]
                if not world.overlay.is_working:
                    continue
                for report in world.overlay.on_record(record, self._hist_book, evidence):
                    # R2 (atomic batch convention): the batch is processed as a single atomic
                    # event at the batch's exchange_proxy_time (= max record event time). A fill
                    # driven by an earlier record in the batch is stamped by the overlay with
                    # that record's event_time_ns, which can be strictly before proxy_time; its
                    # report delivery (exchange_time + response) would then be scheduled into the
                    # past (before the current logical time, the batch commit). We cannot edit
                    # the overlay (another package), so the engine re-stamps every record-driven
                    # report and its execution to the batch proxy time here: the single effective
                    # time of the atomic batch. This keeps the fill at or after any command that
                    # arrived at the same proxy time (class 1 before class 2 at equal time) and
                    # never schedules a delivery before now. See docs/remediation.md R2.
                    self._emit_report(world, _restamp_report(report, proxy_time))
            self._hist_book.apply_record(record)
        self._hist_book.commit_batch(batch)
        # Snapshot quotes establish reference state without contributing flow features.
        self._record_historical_mid(proxy_time)
        # on_batch_committed emits reports only for INITIALIZATION batches (a technical reset of
        # a working order, T37); for LIVE batches it only refreshes the overlay's audit
        # queue-ahead, which the engine never reads (outcomes use the execution ledger). Calling
        # it per LIVE batch rescans each working order's full ahead set and dominates runtime, so
        # we restrict it to INITIALIZATION batches. This preserves every engine-observable report
        # and fill; only the audit-only true_queue_ahead value may be stale between fills (it is
        # recomputed on the fill path anyway). See the final-message note on this interaction.
        if batch.kind is BatchKind.INITIALIZATION:
            for wid in list(live_ids):
                world = self._worlds[wid]
                if world.overlay.is_working:
                    for report in world.overlay.on_batch_committed(batch, self._hist_book):
                        self._emit_report(world, report)
                if not world.overlay.is_working:
                    self._live_overlays.discard(wid)

    def _record_historical_mid(self, now: int) -> None:
        snap = self._hist_book.snapshot(depth=1)
        mid = snap.mid2() if self._hist_book.status is TradingStatus.TRADING else None
        self._hist_mid_samples.append((now, mid))

    # -- exchange command arrival (class 1) ------------------------------------------

    def _on_command_arrival(self, payload: _CommandArrival, now: int) -> None:
        world = self._worlds[payload.world_id]
        if not world.active:
            return
        reports = world.overlay.on_command(payload.command, self._hist_book)
        if world.overlay.is_working:
            self._live_overlays.add(world.world_id)
        world.log_trace(
            {
                "kind": "command_arrival",
                "time_ns": now,
                "command_id": payload.command.command_id,
                "command_kind": payload.command.kind.value,
                "limit_price_fixed": payload.command.limit_price_fixed,
            }
        )
        for report in reports:
            self._emit_report(world, report)

    # -- client delivery (class 2): market observation -------------------------------

    def _on_client_delivery(self, payload: _ClientDelivery, now: int) -> None:
        batch = payload.batch
        # R3: a market delivery MUST NOT set the client book's trading status. The exchange-plane
        # status attached to the batch (payload.status, the historical status at the batch's
        # proxy time) is not yet observed by the client -- a halt effective on the exchange is
        # only learned at the delayed status-observation event (_ClientStatus) scheduled at
        # capture_time + added_delivery_ns. Setting it here leaked undelivered halts into the
        # client book as soon as any batch was delivered (docs/remediation.md R3). The client
        # book's status now changes only in the _ClientStatus handler.
        # Apply the delivered records directly rather than apply_batch/commit_batch: committing
        # only validates invariants and builds a full snapshot the engine never consumes (it
        # reads best_bid_ask / depth-limited snapshots at decision times). The post-apply book
        # state equals the committed state (commit does not mutate), so features, the queue
        # proxy, and the view are unaffected. This avoids the ReferenceBook's per-batch full
        # snapshot + touched-level validation, the dominant replay cost (perf; final-message note).
        self._client_book.begin_batch(batch)
        for record in batch.records:
            self._client_book.apply_record(record)
        self._market.on_delivered(batch, self._client_book, payload.observation_time_ns)
        if batch.kind is not BatchKind.INITIALIZATION:
            self._last_delivered_proxy_ns = int(batch.exchange_proxy_time)
        self._last_delivery_time_ns = payload.observation_time_ns
        # The client mid is only read at decision times (in _build_view); do not snapshot per
        # delivery (per-batch snapshots dominate runtime, engine spec section 1 perf hint).
        # Feed the queue proxies that are still pre-checkpoint (queue features are read only at
        # the checkpoint). A dedicated set avoids scanning every world on every delivery
        # (engine spec section 1 "only active worlds"; features only at decision times).
        for wid in list(self._proxy_feeding):
            world = self._worlds[wid]
            if world.active and world.proxy is not None and now <= world.checkpoint_ns:
                world.proxy.on_delivered(batch, self._client_book)
            else:
                self._proxy_feeding.discard(wid)

    # -- report delivery (class 2) ---------------------------------------------------

    def _on_report_delivery(self, payload: _ReportDelivery, now: int) -> None:
        world = self._worlds[payload.world_id]
        if world.phase is WorldPhase.DONE:
            return
        changed = world.oms.apply(payload.report)
        world.log_trace(
            {
                "kind": "report_delivery",
                "time_ns": now,
                "report_id": payload.report.report_id,
                "report_kind": payload.report.kind.value,
                "exchange_time_ns": payload.report.exchange_time_ns,
                "cumulative_executed": payload.report.cumulative_executed,
                "changed": changed,
            }
        )
        # Drive the controller if active.
        if world.controller.active and not world.controller.done:
            intents = world.controller.on_report(
                payload.report, world.oms.state, world.oms.reported_executed
            )
            self._dispatch_intents(world, intents, now)
        self._maybe_finish(world, now)

    # -- timers (class 3) ------------------------------------------------------------

    def _on_timer(self, payload: _Timer, now: int) -> None:
        world = self._worlds[payload.world_id]
        kind = payload.kind
        if kind == "arrival":
            self._on_arrival(world, now)
        elif kind == "checkpoint":
            self._on_checkpoint(world, now)
        elif kind == "cutoff":
            self._on_cutoff(world, now)
        elif kind == "deadline":
            self._on_deadline(world, now)

    def _on_arrival(self, world: World, now: int) -> None:
        world.phase = WorldPhase.LIVE
        world.log_trace({"kind": "arrival", "time_ns": now})
        # Schedule the remaining per-world timers now that the world is live.
        self._sched.schedule(
            world.checkpoint_ns, EventClass.TIMER, _Timer(world.world_id, "checkpoint")
        )
        self._sched.schedule(world.cutoff_ns, EventClass.TIMER, _Timer(world.world_id, "cutoff"))
        self._sched.schedule(
            world.deadline_ns, EventClass.TIMER, _Timer(world.world_id, "deadline")
        )
        view = self._build_view(world, now)
        world.z0_mid2 = view.client_mid2
        action = world.policy.on_arrival(view)
        world.log_trace({"kind": "arrival_decision", "time_ns": now, "action": action})
        if action == "SWITCH_TO_TAKER":
            self._enter_controller(world, now)
        elif action == "JOIN_BEST":
            self._join_best(world, now, view)
        # WAIT: nothing.

    def _join_best(self, world: World, now: int, view: DecisionView) -> None:
        """Submit a one-contract same-side limit at the client best (JOIN_BEST)."""
        best = view.best_bid if world.side is TaskSide.BUY else view.best_ask
        if best is None:
            # No client best to join; treat as a rejected-equivalent (cannot submit).
            world.log_trace({"kind": "join_skipped_no_best", "time_ns": now})
            return
        limit = best.price_fixed
        world.own_limit_price_fixed = limit
        # Create the queue cohort proxy at this decision from the client book.
        expected_arrival = now + self._scenario.computation_ns + self._scenario.entry_ns
        world.proxy = QueueCohortProxy(
            side=world.side,
            limit_price_fixed=limit,
            client_book_at_decision=self._client_book,
            decision_time_ns=now,
            expected_arrival_ns=expected_arrival,
            last_delivered_proxy_ns=self._last_delivered_proxy_ns,
        )
        self._proxy_feeding.add(world.world_id)
        cmd_id = world.next_command_id("PASSIVE")
        world.passive_command_id = cmd_id
        world.order_created_ns = now
        world.oms.reserve(cmd_id, "PASSIVE_LIMIT", is_passive=True)
        command = ExchangeCommand(
            command_id=cmd_id,
            task_id=world.task.task_id,
            kind=CommandKind.PASSIVE_LIMIT,
            side=world.side,
            quantity=1,
            arrival_time_ns=expected_arrival,
            limit_price_fixed=limit,
        )
        self._schedule_command(world, command, now)

    # -- checkpoint ------------------------------------------------------------------

    def _on_checkpoint(self, world: World, now: int) -> None:
        if world.phase is WorldPhase.DONE or world.checkpoint_done:
            return
        world.checkpoint_done = True
        self._proxy_feeding.discard(world.world_id)
        view = self._build_view(world, now)
        eligibility = checkpoint_eligibility(view)
        # R8: persist checkpoint eligibility on the world for EVERY world at its checkpoint,
        # independent of feature recording. task_results reads this field (not the optional B1
        # feature capture), so a MODEL_CHOICE world is marked eligible even when
        # record_checkpoint_features is False or the policy is not a B1-prefix policy.
        world.checkpoint_eligible = eligibility is DecisionReason.MODEL_CHOICE
        # Record checkpoint features for B1-prefix worlds (B1 / B1_PROBE).
        is_b1_prefix = world.policy.policy_id in ("B1", B1_PROBE_POLICY_ID)
        if self._record_checkpoint_features and is_b1_prefix:
            self._record_checkpoint_feature_row(world, view, eligibility, now)

        if eligibility is not DecisionReason.MODEL_CHOICE:
            # Hold with the ineligibility reason; no policy call.
            self._log_decision(world, now, eligibility, CheckpointChoice.HOLD, eligible=False)
            world.log_trace(
                {
                    "kind": "checkpoint",
                    "time_ns": now,
                    "eligibility": eligibility.value,
                    "choice": CheckpointChoice.HOLD.value,
                    "client_status": view.client_status.value,
                }
            )
            return

        # Eligible checkpoint.
        if world.is_probe:
            self._fork_probe(world, now, view)
            return
        choice, reason = world.policy.on_checkpoint(view)
        self._log_decision(world, now, reason, choice, eligible=True)
        world.log_trace(
            {
                "kind": "checkpoint",
                "time_ns": now,
                "eligibility": eligibility.value,
                "choice": choice.value,
                "reason": reason.value,
                "client_status": view.client_status.value,
            }
        )
        if choice is CheckpointChoice.SWITCH:
            self._enter_controller(world, now)

    # -- cutoff ----------------------------------------------------------------------

    def _on_cutoff(self, world: World, now: int) -> None:
        if world.phase is WorldPhase.DONE or world.cutoff_entered:
            return
        world.cutoff_entered = True
        # R7: enter the controller for every *unresolved* world. "Resolved" is NOT the same as a
        # terminal OMS state: a REJECTED passive (its single aggressive attempt still unused) is
        # terminal on the client OMS yet must still send the one aggressive attempt at the cutoff
        # (the controller's REJECTED/NONE path does exactly that). A task is resolved only if it
        # is FILLED (reported executed), its aggressive attempt is already used (AGGRESSIVE_UNFILLED
        # or an earlier aggressive), or it is a TECHNICAL reset. Previously the cutoff skipped the
        # controller for any terminal state, so a rejected passive could never complete even when
        # liquidity returned before the cutoff (docs/remediation.md R7).
        if self._is_resolved(world):
            return
        world.log_trace({"kind": "cutoff", "time_ns": now})
        self._enter_controller(world, now)

    def _is_resolved(self, world: World) -> bool:
        """True iff the world needs no terminal aggressive attempt at the cutoff (R7).

        Resolved = already executed (FILLED), the single aggressive attempt is already used
        (controller ``aggressive_used``, which also covers AGGRESSIVE_UNFILLED), or the task is a
        TECHNICAL reset. A REJECTED/CANCELLED/NONE/WORKING/IN_FLIGHT state with an unused
        aggressive is NOT resolved and must enter the controller.
        """
        if world.oms.reported_executed >= 1:
            return True
        if world.oms.state in (ClientOrderState.FILLED, ClientOrderState.TECHNICAL):
            return True
        return bool(world.controller.aggressive_used)

    # -- deadline --------------------------------------------------------------------

    def _on_deadline(self, world: World, now: int) -> None:
        if world.phase is WorldPhase.DONE:
            return
        # Freeze the economic outcome at T; continue draining for reconciliation only.
        world.phase = WorldPhase.DRAINING
        world.log_trace({"kind": "deadline_freeze", "time_ns": now})
        self._maybe_finish(world, now)

    # -- controller & command dispatch -----------------------------------------------

    def _enter_controller(self, world: World, now: int) -> None:
        """Enter the terminal controller (idempotent) and dispatch its initial intents."""
        intents = world.controller.enter(world.oms.state, world.oms.reported_executed)
        world.log_trace(
            {"kind": "controller_enter", "time_ns": now, "state": world.oms.state.value}
        )
        self._dispatch_intents(world, intents, now)
        self._maybe_finish(world, now)

    def _dispatch_intents(self, world: World, intents: list[CommandIntent], now: int) -> None:
        for intent in intents:
            if intent.kind is IntentKind.CANCEL:
                cmd_id = world.next_command_id("CANCEL")
                world.oms.reserve(cmd_id, "CANCEL", is_passive=False)
                command = ExchangeCommand(
                    command_id=cmd_id,
                    task_id=world.task.task_id,
                    kind=CommandKind.CANCEL,
                    side=world.side,
                    quantity=intent.quantity,
                    arrival_time_ns=now + self._scenario.computation_ns + self._scenario.entry_ns,
                    limit_price_fixed=None,
                    target_command_id=world.passive_command_id,
                )
                self._schedule_command(world, command, now)
            elif intent.kind is IntentKind.AGGRESSIVE:
                cmd_id = world.next_command_id("AGGRESSIVE")
                world.oms.reserve(cmd_id, "AGGRESSIVE", is_passive=False)
                command = ExchangeCommand(
                    command_id=cmd_id,
                    task_id=world.task.task_id,
                    kind=CommandKind.AGGRESSIVE,
                    side=world.side,
                    quantity=intent.quantity,
                    arrival_time_ns=now + self._scenario.computation_ns + self._scenario.entry_ns,
                    limit_price_fixed=None,
                )
                self._schedule_command(world, command, now)

    def _schedule_command(self, world: World, command: ExchangeCommand, decision_ns: int) -> None:
        """Schedule a command arrival (``decision + computation + entry``) as a class-1 event."""
        world.log_trace(
            {
                "kind": "command_created",
                "time_ns": decision_ns,
                "command_id": command.command_id,
                "command_kind": command.kind.value,
                "send_ns": decision_ns + self._scenario.computation_ns,
                "arrival_ns": command.arrival_time_ns,
            }
        )
        self._sched.schedule(
            int(command.arrival_time_ns),
            EventClass.EXCHANGE_COMMAND,
            _CommandArrival(world.world_id, command),
        )

    def _emit_report(self, world: World, report: ExchangeReport) -> None:
        """Record an exchange report and schedule its delivery (``exchange_time + response``)."""
        self._report_rows.append(
            {
                "task_id": world.task.task_id,
                "policy_id": world.policy.policy_id,
                "scenario_id": self._scenario.scenario_id,
                "horizon_ns": self._config.horizon_ns,
                "world_id": world.world_id,
                "report_id": report.report_id,
                "command_id": report.command_id,
                "report_kind": report.kind.value,
                "exchange_time_ns": int(report.exchange_time_ns),
                "delivery_time_ns": int(report.exchange_time_ns) + self._scenario.response_ns,
                "cumulative_executed": report.cumulative_executed,
                "reason": report.reason,
            }
        )
        if report.execution is not None:
            ex = report.execution
            self._execution_rows.append(
                {
                    "task_id": world.task.task_id,
                    "policy_id": world.policy.policy_id,
                    "scenario_id": self._scenario.scenario_id,
                    "horizon_ns": self._config.horizon_ns,
                    "world_id": world.world_id,
                    "execution_id": ex.execution_id,
                    "quantity": ex.quantity,
                    "price_fixed": ex.price_fixed,
                    "fee_fixed": ex.fee_fixed,
                    "exchange_time_ns": int(ex.exchange_time_ns),
                    "liquidity": ex.liquidity.value,
                    "mechanism": ex.mechanism.value,
                }
            )
        delivery = int(report.exchange_time_ns) + self._scenario.response_ns
        self._sched.schedule(
            delivery, EventClass.CLIENT_DELIVERY, _ReportDelivery(world.world_id, report)
        )

    # -- decision view ----------------------------------------------------------------

    def _build_view(self, world: World, now: int) -> DecisionView:
        """Build the client-plane :class:`DecisionView` (never contains m0 or overlay truth)."""
        snap = self._client_book.snapshot(depth=3)
        best_bid = snap.best_bid
        best_ask = snap.best_ask
        # Client mid is derived here (at the decision), not per delivery (perf). It is valid only
        # while TRADING and the book is two-sided and uncrossed (matches BookSnapshot.mid2()).
        client_mid2 = snap.mid2() if self._client_book.status is TradingStatus.TRADING else None
        self._client_mid2 = client_mid2
        quote_age = (
            None if self._last_delivery_time_ns < 0 else max(now - self._last_delivery_time_ns, 0)
        )
        market_features = self._market.features(now, self._client_book)
        queue_features = world.proxy.features(self._client_book) if world.proxy is not None else {}
        price_signal = self._price_scorer(market_features) if self._price_scorer is not None else {}
        order_age = None if world.order_created_ns is None else max(now - world.order_created_ns, 0)
        view = DecisionView(
            now_ns=now,
            task_id=world.task.task_id,
            session_id=world.task.session_id,
            side=world.side,
            horizon_ns=self._config.horizon_ns,
            deadline_ns=int(world.task.deadline_ns),
            time_remaining_ns=int(world.task.deadline_ns) - now,
            client_status=self._client_book.status,
            best_bid=best_bid,
            best_ask=best_ask,
            client_mid2=client_mid2,
            client_quote_age_ns=quote_age,
            order_state=world.oms.state,
            pending_command=world.oms.has_pending_command,
            reported_executed=world.oms.reported_executed,
            own_limit_price_fixed=world.own_limit_price_fixed,
            order_age_ns=order_age,
            in_controller=world.controller.active,
            market_features=market_features,
            queue_features=queue_features,
            price_signal=price_signal,
        )
        return view

    def _checkpoint_feature_dict(self, world: World, view: DecisionView) -> dict[str, float]:
        """All MARKET, MECHANICS, and QUEUE features at the checkpoint (engine spec section 8)."""
        feats: dict[str, float] = {}
        feats.update(view.market_features)
        feats.update(mechanics_features(view, int(self._instrument.tick_size_fixed)))
        feats.update(view.queue_features)
        return feats

    def _record_checkpoint_feature_row(
        self, world: World, view: DecisionView, eligibility: DecisionReason, now: int
    ) -> None:
        feats = self._checkpoint_feature_dict(world, view)
        row: dict[str, Any] = {
            "task_id": world.task.task_id,
            "session_id": world.task.session_id,
            "policy_id": world.policy.policy_id,
            "scenario_id": self._scenario.scenario_id,
            "horizon_ns": self._config.horizon_ns,
            "side": int(world.side),
            "time_ns": now,
            "eligibility": eligibility.value,
            "checkpoint_eligible": eligibility is DecisionReason.MODEL_CHOICE,
        }
        row.update({f"feat_{k}": v for k, v in feats.items()})
        world.checkpoint_features = row
        self._checkpoint_rows.append(row)

    # -- label probe forking ----------------------------------------------------------

    def _fork_probe(self, world: World, now: int, view: DecisionView) -> None:
        """Clone the probe world into HOLD and SWITCH branches (engine spec section 8)."""
        feats = self._checkpoint_feature_dict(world, view)
        self._fork_counter += 1
        hold = world.clone(f"{world.task.task_id}|B1_PROBE_HOLD#{self._fork_counter}")
        switch = world.clone(f"{world.task.task_id}|B1_PROBE_SWITCH#{self._fork_counter}")
        for branch, action in ((hold, "HOLD"), (switch, "SWITCH")):
            branch.is_probe = False
            branch.branch_action = action
            branch.probe_parent_id = world.world_id
            branch.checkpoint_features = dict(world.checkpoint_features or {})
            branch.fork_feats = feats
            branch.fork_time_ns = now
            self._worlds[branch.world_id] = branch
            if branch.overlay.is_working:
                self._live_overlays.add(branch.world_id)
            # The parent's remaining per-world timers (cutoff, deadline) were scheduled keyed to
            # the parent world_id; once the parent is marked DONE below they would no-op on
            # lookup. World.clone cannot carry pending scheduler events (they live on the engine,
            # not the world), so the engine re-schedules the branch's cutoff and deadline timers
            # here keyed to the branch world_id. This makes each branch enter the terminal
            # controller at its cutoff and freeze at T exactly like a standalone world
            # (engine spec section 6: controller entered for every unresolved world at the
            # cutoff; section 7: outcome frozen at T; section 8: branches continue in the same
            # pass). The checkpoint already fired (checkpoint_done is cloned True), so no
            # checkpoint timer is re-scheduled.
            self._sched.schedule(
                branch.cutoff_ns, EventClass.TIMER, _Timer(branch.world_id, "cutoff")
            )
            self._sched.schedule(
                branch.deadline_ns, EventClass.TIMER, _Timer(branch.world_id, "deadline")
            )
            # R1: the branch inherits the parent's execution-ledger and report rows (emitted
            # BEFORE the fork, keyed to the parent world_id) and every pending scheduled event for
            # the parent (report deliveries and command arrivals still in flight at the fork),
            # duplicated under the branch world_id. Without this a pre-fork passive fill -- whose
            # execution row sits under the parent id and whose FILL report is still in flight at
            # the fork -- is lost to both branches: the branch's outcome math filters the ledger
            # by its own world_id and its OMS never receives the in-flight report.
            self._inherit_parent_state(world, branch, now)
        # Parent stops: it will not act further; mark done so it contributes no result.
        world.phase = WorldPhase.DONE
        self._live_overlays.discard(world.world_id)
        world.log_trace({"kind": "probe_fork", "time_ns": now})
        # SWITCH branch enters the controller now; HOLD continues passively until its cutoff.
        self._enter_controller(switch, now)

    def _inherit_parent_state(self, parent: World, branch: World, now: int) -> None:
        """Duplicate the parent's pre-fork ledger rows and pending scheduled events onto a branch.

        Executions and reports emitted before the fork are keyed to the parent ``world_id``; the
        branch's outcome math and support accounting filter the ledger by its own ``world_id``, so
        the engine copies every parent execution/report row under the branch ``world_id`` (ids are
        reused: idempotency is per-OMS and each branch has its own OMS, and the output frames key
        on ``world_id``). Then it duplicates every pending (future) ``_ReportDelivery`` and
        ``_CommandArrival`` for the parent -- a passive FILL report in flight, a command arrival --
        onto the branch so the branch's OMS evolves exactly as a standalone world would from the
        fork onward (engine spec section 8; remediation R1).
        """
        pid = parent.world_id
        bid = branch.world_id
        for row in list(self._execution_rows):
            if row["world_id"] == pid:
                new = dict(row)
                new["world_id"] = bid
                self._execution_rows.append(new)
        for row in list(self._report_rows):
            if row["world_id"] == pid:
                new = dict(row)
                new["world_id"] = bid
                self._report_rows.append(new)
        # Duplicate pending report deliveries and command arrivals (future events only). The
        # scheduler guards against past scheduling; every pending event is at time >= now by
        # construction, so re-scheduling at the same time (a zero-delay copy ordered after the
        # original by seq) is always valid.
        for event in self._sched.pending_events():
            payload = event.payload
            if isinstance(payload, _ReportDelivery) and payload.world_id == pid:
                self._sched.schedule(
                    event.time_ns,
                    EventClass.CLIENT_DELIVERY,
                    _ReportDelivery(bid, payload.report),
                )
            elif isinstance(payload, _CommandArrival) and payload.world_id == pid:
                self._sched.schedule(
                    event.time_ns,
                    EventClass.EXCHANGE_COMMAND,
                    _CommandArrival(bid, payload.command),
                )

    def _log_decision(
        self,
        world: World,
        now: int,
        reason: DecisionReason,
        choice: CheckpointChoice,
        *,
        eligible: bool,
    ) -> None:
        self._decision_rows.append(
            {
                "task_id": world.task.task_id,
                "policy_id": world.policy.policy_id,
                "scenario_id": self._scenario.scenario_id,
                "horizon_ns": self._config.horizon_ns,
                "time_ns": now,
                "eligibility": "MODEL_CHOICE" if eligible else reason.value,
                "choice": choice.value,
                "reason": reason.value,
            }
        )

    # -- world finishing ---------------------------------------------------------------

    def _maybe_finish(self, world: World, now: int) -> None:
        """Mark a world DONE once it is draining and has no pending commands or live overlay."""
        if world.phase is not WorldPhase.DRAINING:
            return
        if world.oms.has_pending_command:
            return
        if world.overlay.is_working and not world.oms.is_terminal:
            # Overlay still live and could still fill passively during drain; wait.
            return
        world.phase = WorldPhase.DONE
        self._live_overlays.discard(world.world_id)

    # -- output assembly ---------------------------------------------------------------

    def _assemble_outputs(self) -> SessionOutputs:
        assert self._manifest is not None and self._mid_series is not None
        tick = int(self._instrument.tick_size_fixed)
        mult = self._instrument.multiplier
        fee = self._config.fee_per_contract_fixed

        task_results: list[dict[str, Any]] = []
        traces: dict[tuple[str, str], list[dict[str, Any]]] = {}
        support_counts: dict[str, dict[str, int]] = {}

        quality_rows = self._quality_rows()
        unevaluable: dict[str, str] = {}
        for row in quality_rows:
            if row["technically_unevaluable"]:
                unevaluable[row["task_id"]] = row["reason"] or "UNKNOWN"

        for world in self._worlds.values():
            if world.phase is WorldPhase.PENDING:
                # Never arrived (should not happen for manifest tasks); skip.
                continue
            if world.branch_action is not None:
                # Branch worlds contribute branch_labels, not task_results.
                self._finalize_branch(world, tick, mult, fee, unevaluable)
                traces[(world.world_id, world.policy.policy_id)] = world.trace_events
                continue
            if world.is_probe:
                # The probe parent that forked produced branches; it has no own result.
                continue
            result = self._compute_task_result(world, tick, mult, fee, unevaluable)
            task_results.append(result)
            traces[(world.task.task_id, world.policy.policy_id)] = world.trace_events
            if world.policy.policy_id == "B1":
                self._accumulate_support(
                    world,
                    support_counts,
                    technically_unevaluable=result["status"]
                    == TaskStatus.TECHNICALLY_UNEVALUABLE.value,
                )

        return SessionOutputs(
            tasks=self._tasks_frame(),
            task_results=_frame(task_results, _TASK_RESULT_COLUMNS),
            decisions=_frame(self._decision_rows, _DECISION_COLUMNS),
            executions=_frame(self._execution_rows, _EXECUTION_COLUMNS),
            reports=_frame(self._report_rows, _REPORT_COLUMNS),
            checkpoint_features=_frame(self._checkpoint_rows, None),
            branch_labels=_frame(self._branch_rows, _BRANCH_COLUMNS),
            support=self._support_frame(support_counts),
            quality=_frame(quality_rows, _QUALITY_COLUMNS),
            _traces=traces,
        )

    def _compute_task_result(
        self,
        world: World,
        tick: int,
        mult: int,
        fee: int,
        unevaluable: dict[str, str],
    ) -> dict[str, Any]:
        assert self._mid_series is not None
        task = world.task
        deadline = int(task.deadline_ns)
        m0 = int(task.arrival_reference_mid2)

        # Executions that count: exchange_time <= T (on-time, even if reported late, T18).
        # Use the exchange ledger (recorded execution rows) so an on-time fill whose report is
        # delivered after the drain still counts.
        on_time = self._on_time_executions(world, deadline)
        executed_qty = sum(e.quantity for e in on_time)
        executed_qty = min(executed_qty, 1)
        residual = 1 - executed_qty

        reason = unevaluable.get(task.task_id)
        technically_invalid = world.overlay.technically_invalid
        if reason is not None or technically_invalid:
            status = TaskStatus.TECHNICALLY_UNEVALUABLE
        elif executed_qty >= 1:
            status = TaskStatus.COMPLETED_ON_TIME
        else:
            status = TaskStatus.DEADLINE_MISS

        completion_time = None
        fill_mechanism = None
        passive_filled = False
        avg_price = None
        fees_total = 0
        if on_time:
            fill = on_time[0]
            completion_time = int(fill.exchange_time_ns)
            fill_mechanism = fill.mechanism
            passive_filled = fill.liquidity == "PASSIVE"
            avg_price = float(fill.price_fixed)
            fees_total = sum(e.fee_fixed for e in on_time)

        is_gross = is_net = None
        if status is TaskStatus.COMPLETED_ON_TIME:
            fills = [(e.quantity, e.price_fixed) for e in on_time]
            is_gross, is_net = implementation_shortfall_ticks(
                int(world.side), fills, m0, tick, mult, fees_total, quantity=1
            )

        c_t = None
        m_t_age_ns: int | None = None
        if status is not TaskStatus.TECHNICALLY_UNEVALUABLE:
            z0 = self._client_mid2_at_arrival(world)
            # R10: m_T is the last *valid* committed historical mid at or before T (skipping
            # invalid/halted samples), not the state at T (which may be invalid). Record its age
            # so a stale reference is auditable. deadline_value_ticks is then always computed from
            # a valid m_T when z0 exists; a null cost (no valid mid anywhere at/before T) does not
            # drop the row -- it is persisted with c_t None and counted downstream.
            m_t_lookup = self._mid_series.last_valid_mid_at(deadline)
            if m_t_lookup is not None:
                m_t, m_t_sample_ns = m_t_lookup
                m_t_age_ns = deadline - m_t_sample_ns
                if z0 is not None:
                    fills = [(e.quantity, e.price_fixed) for e in on_time]
                    c_t = deadline_value_ticks(
                        int(world.side), fills, fees_total, z0, m_t, tick, mult
                    )

        # Diagnostic late completion: a fill after T (drain reconciliation only).
        late = [e for e in world.oms.executions if int(e.exchange_time_ns) > deadline]
        diag_late = (
            int(late[0].exchange_time_ns)
            if late and status != TaskStatus.COMPLETED_ON_TIME
            else None
        )

        checkpoint_eligible = world.checkpoint_eligible
        checkpoint_choice = None
        decision_reason = DecisionReason.NOT_APPLICABLE.value
        for d in reversed(self._decision_rows):
            if d["task_id"] == task.task_id and d["policy_id"] == world.policy.policy_id:
                checkpoint_choice = d["choice"]
                decision_reason = d["reason"]
                break

        world.log_trace(
            {
                "kind": "outcome",
                "time_ns": deadline,
                "status": status.value,
                "executed_quantity": executed_qty,
            }
        )

        return {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "policy_id": world.policy.policy_id,
            "scenario_id": self._scenario.scenario_id,
            "horizon_ns": self._config.horizon_ns,
            "side": int(world.side),
            "status": status.value,
            "executed_quantity": executed_qty,
            "residual_quantity": residual,
            "completion_time_ns": completion_time,
            "avg_price_fixed": avg_price,
            "fees_fixed": fees_total,
            "is_ticks_gross": is_gross,
            "is_ticks_net": is_net,
            "c_t_ticks": c_t,
            "m_t_age_ns": m_t_age_ns,
            "passive_filled": passive_filled,
            "fill_mechanism": fill_mechanism,
            "checkpoint_eligible": checkpoint_eligible,
            "checkpoint_choice": checkpoint_choice,
            "decision_reason": decision_reason,
            "action_count": world.command_counter,
            "diagnostic_late_completion_ns": diag_late,
        }

    def _on_time_executions(self, world: World, deadline: int) -> list[Any]:
        """On-time executions (exchange_time <= T) from the exchange report ledger for the world.

        Uses the recorded execution rows (exchange ledger) rather than only client-delivered
        reports, so an on-time fill whose report arrives after the drain still counts (T18).
        """
        out = []
        for row in self._execution_rows:
            if row["world_id"] == world.world_id and row["exchange_time_ns"] <= deadline:
                out.append(_ExecRow(row))
        out.sort(key=lambda e: e.exchange_time_ns)
        return out

    def _client_mid2_at_arrival(self, world: World) -> int | None:
        """``z0`` = client Mid2 at arrival from the scenario's client book (manifest guarantees
        a valid client mid at arrival in this scenario). Recovered from the manifest's eligibility
        pass is not stored per task, so recompute from the delivered stream snapshot cached at the
        arrival timer. We stored it on the world via the arrival view's client_mid2."""
        return world.z0_mid2

    # -- branch labels (engine spec section 8) ----------------------------------------

    def _finalize_branch(
        self, world: World, tick: int, mult: int, fee: int, unevaluable: dict[str, str]
    ) -> None:
        assert self._mid_series is not None
        task = world.task
        deadline = int(task.deadline_ns)
        on_time = self._on_time_executions(world, deadline)
        executed_qty = min(sum(e.quantity for e in on_time), 1)
        completed = executed_qty >= 1
        miss = 0 if completed else 1
        fill_mechanism = on_time[0].mechanism if on_time else None
        z0 = world.z0_mid2
        # R10: m_T is the last *valid* committed historical mid at or before T; record its age.
        m_t_lookup = self._mid_series.last_valid_mid_at(deadline)
        m_t: int | None = None
        m_t_age_ns: int | None = None
        if m_t_lookup is not None:
            m_t, m_t_sample_ns = m_t_lookup
            m_t_age_ns = deadline - m_t_sample_ns
        c_t = None
        if z0 is not None and m_t is not None:
            fills = [(e.quantity, e.price_fixed) for e in on_time]
            fees_total = sum(e.fee_fixed for e in on_time)
            c_t = deadline_value_ticks(int(world.side), fills, fees_total, z0, m_t, tick, mult)
        # R4: branch rows carry the policy-independent quality verdict from the same quality map
        # as task_results, so the C plane excludes technically-unevaluable branches from training
        # (counted) rather than silently training on them. An overlay TECHNICAL_RESET on the
        # branch is also unevaluable (it is a strict subset of the quality map's CLEAR/snapshot
        # conditions, but a reset reached on a cloned overlay is flagged defensively here too).
        reason = unevaluable.get(task.task_id)
        technically_unevaluable = reason is not None or world.overlay.technically_invalid
        unevaluable_reason = reason or (
            "OVERLAY_TECHNICAL_RESET" if world.overlay.technically_invalid else None
        )
        row: dict[str, Any] = {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "scenario_id": self._scenario.scenario_id,
            "horizon_ns": self._config.horizon_ns,
            "side": int(world.side),
            "action": world.branch_action,
            "miss": miss,
            "c_t_ticks": c_t,
            "completed": completed,
            "fill_mechanism": fill_mechanism,
            "checkpoint_time_ns": world.fork_time_ns,
            "z0_mid2": z0,
            "m_t_mid2": m_t,
            "m_t_age_ns": m_t_age_ns,
            "technically_unevaluable": technically_unevaluable,
            "unevaluable_reason": unevaluable_reason,
            "fees_fixed": sum(e.fee_fixed for e in on_time),
        }
        if world.fork_feats is not None:
            row.update({f"feat_{k}": v for k, v in world.fork_feats.items()})
        self._branch_rows.append(row)

    # -- support diagnostic (engine spec section 9) ----------------------------------

    def _accumulate_support(
        self,
        world: World,
        counts: dict[str, dict[str, int]],
        *,
        technically_unevaluable: bool,
    ) -> None:
        sess = world.task.session_id
        c = counts.setdefault(
            sess,
            {
                "decision_eligible_checkpoints": 0,
                "post_checkpoint_passive_fills": 0,
                "post_checkpoint_queue_depletion": 0,
                "post_checkpoint_trade_through": 0,
                "fills_before_checkpoint": 0,
            },
        )
        # Support must describe usable research observations. Keep the pilot's row
        # when every task is excluded, but never let invalid task windows pass G2-S.
        if technically_unevaluable:
            return
        if world.checkpoint_eligible:
            c["decision_eligible_checkpoints"] += 1
        checkpoint_ns = world.checkpoint_ns
        cutoff_ns = world.cutoff_ns
        for row in self._execution_rows:
            if row["world_id"] != world.world_id:
                continue
            t = row["exchange_time_ns"]
            if row["liquidity"] == "PASSIVE" and checkpoint_ns < t < cutoff_ns:
                c["post_checkpoint_passive_fills"] += 1
                if row["mechanism"] == FillMechanism.QUEUE_DEPLETION.value:
                    c["post_checkpoint_queue_depletion"] += 1
                elif row["mechanism"] == FillMechanism.TRADE_THROUGH.value:
                    c["post_checkpoint_trade_through"] += 1
            elif t <= checkpoint_ns:
                c["fills_before_checkpoint"] += 1

    def _support_frame(self, counts: dict[str, dict[str, int]]) -> pl.DataFrame:
        rows = [
            {
                "session_id": s,
                "scenario_id": self._scenario.scenario_id,
                "horizon_ns": self._config.horizon_ns,
                **v,
            }
            for s, v in sorted(counts.items())
        ]
        return _frame(rows, _SUPPORT_COLUMNS)

    # -- tasks / quality frames -------------------------------------------------------

    def _tasks_frame(self) -> pl.DataFrame:
        assert self._manifest is not None
        rows: list[dict[str, Any]] = []
        for t in self._manifest.tasks:
            rows.append(
                {
                    "task_id": t.task_id,
                    "session_id": t.session_id,
                    "instrument_id": t.instrument_id,
                    "side": int(t.side),
                    "quantity": t.quantity,
                    "arrival_time_ns": int(t.arrival_time_ns),
                    "deadline_ns": int(t.deadline_ns),
                    "arrival_reference_mid2": t.arrival_reference_mid2,
                    "eligibility_rule_version": t.eligibility_rule_version,
                    "task_manifest_id": t.task_manifest_id,
                    "eligible": True,
                    "ineligible_reason": None,
                }
            )
        for ineligible in self._manifest.ineligible:
            rows.append(
                {
                    "task_id": None,
                    "session_id": self._manifest.session_id,
                    "instrument_id": self._instrument.instrument_id,
                    "side": int(ineligible.side),
                    "quantity": 1,
                    "arrival_time_ns": ineligible.arrival_ns,
                    "deadline_ns": ineligible.arrival_ns + self._config.horizon_ns,
                    "arrival_reference_mid2": None,
                    "eligibility_rule_version": "v1",
                    "task_manifest_id": self._manifest.task_manifest_id,
                    "eligible": False,
                    "ineligible_reason": ineligible.reason,
                }
            )
        return _frame(rows, _TASKS_COLUMNS)

    def _quality_rows(self) -> list[dict[str, Any]]:
        assert self._manifest is not None
        qmap = self._manifest.quality
        rows: list[dict[str, Any]] = []
        for t in self._manifest.tasks:
            uneval, reason = qmap.is_unevaluable(
                QualityWindow(int(t.arrival_time_ns), int(t.deadline_ns))
            )
            rows.append(
                {
                    "task_id": t.task_id,
                    "session_id": t.session_id,
                    "scenario_id": self._scenario.scenario_id,
                    "horizon_ns": self._config.horizon_ns,
                    "arrival_ns": int(t.arrival_time_ns),
                    "deadline_ns": int(t.deadline_ns),
                    "technically_unevaluable": uneval,
                    "reason": reason,
                }
            )
        return rows


@dataclass(slots=True)
class _ExecRow:
    """Lightweight execution view over a recorded execution row (for outcome math)."""

    _row: dict[str, Any]

    @property
    def quantity(self) -> int:
        return int(self._row["quantity"])

    @property
    def price_fixed(self) -> int:
        return int(self._row["price_fixed"])

    @property
    def fee_fixed(self) -> int:
        return int(self._row["fee_fixed"])

    @property
    def exchange_time_ns(self) -> int:
        return int(self._row["exchange_time_ns"])

    @property
    def mechanism(self) -> str:
        return str(self._row["mechanism"])

    @property
    def liquidity(self) -> str:
        return str(self._row["liquidity"])


def _restamp_report(report: ExchangeReport, exchange_time_ns: int) -> ExchangeReport:
    """Return ``report`` re-stamped to ``exchange_time_ns`` (atomic-batch convention, R2).

    Record-driven fills are emitted by the overlay with the triggering record's event time, but
    the batch is processed atomically at its ``exchange_proxy_time``. The engine stamps every
    record-driven report -- and its nested :class:`~qexec.core.tasks.Execution` -- with the batch
    proxy time so the fill's effective time is the single atomic batch time (never before the
    batch commit's logical ``now``). A no-op when the times already match.
    """
    if int(report.exchange_time_ns) == exchange_time_ns and (
        report.execution is None or int(report.execution.exchange_time_ns) == exchange_time_ns
    ):
        return report
    execution = report.execution
    if execution is not None:
        execution = replace(execution, exchange_time_ns=TimeNs(exchange_time_ns))
    return replace(report, exchange_time_ns=exchange_time_ns, execution=execution)


def _scenario_by_id(scenario_id: str) -> LatencyScenario:
    if scenario_id not in LATENCY_SCENARIOS:
        raise ValueError(f"unknown latency scenario {scenario_id!r}")
    return LATENCY_SCENARIOS[scenario_id]


def _frame(rows: list[dict[str, Any]], columns: tuple[str, ...] | None) -> pl.DataFrame:
    """Build a polars DataFrame, enforcing a stable schema even when ``rows`` is empty."""
    if rows:
        # Scan every row for schema inference: optional columns (e.g. ineligible_reason) may be
        # None on the leading rows and populated later; polars' default 100-row inference would
        # then reject the later values.
        df = pl.DataFrame(rows, infer_schema_length=None)
        if columns is not None:
            ordered = [c for c in columns if c in df.columns]
            extra = [c for c in df.columns if c not in columns]
            return df.select(ordered + extra)
        return df
    if columns is None:
        return pl.DataFrame()
    return pl.DataFrame({c: [] for c in columns})


_TASKS_COLUMNS = (
    "task_id",
    "session_id",
    "instrument_id",
    "side",
    "quantity",
    "arrival_time_ns",
    "deadline_ns",
    "arrival_reference_mid2",
    "eligibility_rule_version",
    "task_manifest_id",
    "eligible",
    "ineligible_reason",
)
_TASK_RESULT_COLUMNS = (
    "task_id",
    "session_id",
    "policy_id",
    "scenario_id",
    "horizon_ns",
    "side",
    "status",
    "executed_quantity",
    "residual_quantity",
    "completion_time_ns",
    "avg_price_fixed",
    "fees_fixed",
    "is_ticks_gross",
    "is_ticks_net",
    "c_t_ticks",
    "m_t_age_ns",
    "passive_filled",
    "fill_mechanism",
    "checkpoint_eligible",
    "checkpoint_choice",
    "decision_reason",
    "action_count",
    "diagnostic_late_completion_ns",
)
_DECISION_COLUMNS = (
    "task_id",
    "policy_id",
    "scenario_id",
    "horizon_ns",
    "time_ns",
    "eligibility",
    "choice",
    "reason",
)
_EXECUTION_COLUMNS = (
    "task_id",
    "policy_id",
    "scenario_id",
    "horizon_ns",
    "world_id",
    "execution_id",
    "quantity",
    "price_fixed",
    "fee_fixed",
    "exchange_time_ns",
    "liquidity",
    "mechanism",
)
_REPORT_COLUMNS = (
    "task_id",
    "policy_id",
    "scenario_id",
    "horizon_ns",
    "world_id",
    "report_id",
    "command_id",
    "report_kind",
    "exchange_time_ns",
    "delivery_time_ns",
    "cumulative_executed",
    "reason",
)
_BRANCH_COLUMNS = (
    "task_id",
    "session_id",
    "scenario_id",
    "horizon_ns",
    "side",
    "action",
    "miss",
    "c_t_ticks",
    "completed",
    "fill_mechanism",
    "checkpoint_time_ns",
    "z0_mid2",
    "m_t_mid2",
    "m_t_age_ns",
    "technically_unevaluable",
    "unevaluable_reason",
    "fees_fixed",
)
_SUPPORT_COLUMNS = (
    "session_id",
    "scenario_id",
    "horizon_ns",
    "decision_eligible_checkpoints",
    "post_checkpoint_passive_fills",
    "post_checkpoint_queue_depletion",
    "post_checkpoint_trade_through",
    "fills_before_checkpoint",
)
_QUALITY_COLUMNS = (
    "task_id",
    "session_id",
    "scenario_id",
    "horizon_ns",
    "arrival_ns",
    "deadline_ns",
    "technically_unevaluable",
    "reason",
)
