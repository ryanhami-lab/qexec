# WP2-ENGINE specification (binding)

Owned paths: `python/qexec/sim/{scheduler,oms,controller,world,engine,tasks,quality}.py`,
`python/qexec/policies/{__init__,base,baselines}.py`, tests in `tests/unit/sim/test_engine_*.py`,
`tests/unit/sim/test_oms*.py`, `tests/unit/sim/test_controller*.py`, `tests/unit/sim/test_tasks*.py`,
`tests/unit/policies/`, `tests/integration/test_engine_*.py`.
Execution attribution in `sim/overlay.py` and `sim/evidence.py` follows the same source contract.
Changes to these interfaces require coordinated engine and regression updates.

Reuse: `ReferenceBook` (historical book and each scenario's client book), `iter_session_batches`,
`ExchangeOverlay`, `MarketFeatureState`, `QueueCohortProxy`, `features.groups.mechanics_features`,
`labels.price.MidSeries`, `analysis.metrics` (IS, C_T), `core.messages`, `core.views`, `core.tasks`.

## 1. One pass per (session, latency scenario)

`SessionEngine(session_dir, config, policies, *, scenario_id=None, fork_label_probe=False,
price_scorer=None, record_checkpoint_features=True)`. `run() -> SessionOutputs`.

The historical stream is read once. A single **historical book** applies every batch. For the
scenario there is a single **client book** (a separate `ReferenceBook`) that applies each batch at
its observation time `capture_complete_time + added_delivery_ns`, together with one
`MarketFeatureState`. Many independent **worlds** (one per task x policy, plus label branches)
share these read-only books; each world owns its overlay, OMS, controller, timers, queue proxy.
A world is *active* from its arrival until all its commands and reports are resolved after the
deadline; inactive worlds receive no callbacks (performance).

## 2. Event ordering (contract 2.2, architecture 6.3)

All events are keyed `(time_ns, class, seq)` where class: 0 historical batch commit, 1 exchange
command arrival, 2 client delivery (market observation or report), 3 timers (arrival,
checkpoint, cutoff, deadline) and policy decisions. `seq` is a global monotone counter assigned
when the event is scheduled (causal microsteps: a zero-delay descendant gets a later seq).

Historical batch at proxy time t: validate its complete execution groups once with
`execution_groups(batch)`. For each record, call `overlay.on_record(rec, hist_book, evidence)` for
every active world with a live order, then `hist_book.apply_record(rec)`; then
`hist_book.commit_batch(batch)`; then `overlay.on_batch_committed(batch, hist_book)` per active
world. Apply status events (`records_io.read_status`) to both books by time: a status event with
`event_time_ns <= t` applies to the historical book before batch t; to the client book at
`capture_time_ns + added_delivery_ns` (status observation), ordered with deliveries. Status
observations are enqueued before market deliveries, so a capture-time tie processes statuses
first (exchange-time order among statuses), then batches (source order among batches).
The synthetic adapter's initial status is TRADING until its first explicit status event;
this default is not a rule for admitting a real feed with missing status metadata.

The synthetic execution contract is `TRADE, (FILL, CANCEL)+` at each executed price. All quantities
are positive, linked side/price/identity fields match, each reduction exactly matches its fill,
and allocated quantity equals print volume. An overlay fill requires a matching existing resting
allocation with sufficient quantity that respects the best historical same-side price; skipping better resting
liquidity is invalid source evidence shared by every policy. Same-price queue ambiguity remains
conservative. Both fill mechanisms consume one unit from the reconciled budget;
a bare TRADE or orphan FILL cannot fill. Through execution is
confirmed by its linked FILL, priced at the virtual limit; surviving ahead priority yields
ambiguous evidence and no fill. Evidence retains source group/allocation ordinals and quantity.
Record-driven reports/executions are effective at the atomic batch proxy time.

Exchange reports are delivered to the world's client OMS at `exchange_time_ns + response_ns`.
Commands: created at decision time d (exposure reserved immediately), `send = d + computation`,
`arrival = send + entry`; delivered to `overlay.on_command(cmd, hist_book)` at class 1.

## 3. Tasks and eligibility (`sim/tasks.py`)

`build_task_manifest(session_dir, config, planned_scenarios=tuple(LATENCY_SCENARIOS)) ->
TaskManifest` (tasks: list[Task], ineligible: list[(arrival_ns, side, reason)]). Arrival grid:
`first = session_start + warmup_ns + phase`, phase = deterministic from
`(config.task_seed, session_id)` via `numpy.random.default_rng(seed).integers(0, spacing)`,
spacing = `arrival_spacing_ns(H)`; arrivals while `arrival + H + drain_margin <= session_end`
(drain_margin = 1 s). Both sides per arrival (separate worlds). `task_id =
f"{session_id}:{k:05d}:{BUY|SELL}"`. `m0` = Mid2 of the committed historical book at arrival
(last state at or before arrival on the merged status/book timeline), requires TRADING and a
valid uncrossed two-sided book. Status is applied on a merged timeline: a status transition
creates its own sample even in a quiet gap (no committed batch during the gap), so a halt that
begins before an arrival with no batch in between still makes `m0` invalid at that arrival.
Eligibility also requires a valid client mid at arrival in **every planned scenario** (so the
same manifest serves all scenarios); the client-mid check honors each scenario's delayed status
observations (status observed at `capture_time + added_delivery_ns`). Client eligibility
replays a separate book in delivery order and applies delivered status independently; it never
substitutes a shifted historical midpoint. This handles capture reordering, quiet-gap resumes,
and batches captured while the exchange was halted. Opening snapshots establish quote state
without contributing flow features. The common captured stream can be replayed once and queried
at `arrival - added_delivery_ns` for each constant-delay scenario. Eligibility rule version is
`v2`; `task_manifest_id` = sha256 of the canonical task list.

Policy-independent quality map (`sim/quality.py`): the map records invalid **intervals**, not
corruption instants (architecture section 5). A corrupting event — a committed batch carrying
any `quality_flags`, a CLEAR record, a historical-book anomaly, or malformed/unsupported
execution evidence — opens an invalid interval
that runs until a *trusted recovery*: an INITIALIZATION snapshot batch after the corruption that
itself carries no `quality_flags`, invalid execution evidence or new anomaly. A combined
CLEAR + SNAPSHOT reconstruction closes the old interval atomically; its CLEAR does not reopen it.
An unrecovered interval extends to session end. Every INITIALIZATION also marks a queue-continuity
boundary, including a clean snapshot while no interval is open. A task is TECHNICALLY_UNEVALUABLE
for every policy if its window `[arrival, deadline]` overlaps an invalid interval (`arrival < end`
and `deadline >= start`), or crosses a snapshot boundary (`arrival < reset <= deadline`). An arrival
exactly at a clean reset sees the recovered state. This common rule includes policies already
filled before reset. Computed before any policy runs; per-task verdicts are persisted.

Each normalized INITIALIZATION batch is a complete snapshot. `ReferenceBook.begin_batch`
replaces resting orders and levels before applying its records, on both historical and client
books. It retains status, anomaly history and monotone priority bookkeeping. A future vendor
adapter must assemble fragmented snapshots before emitting this normalized batch. Snapshot
quotes initialize references and never become economic order flow. The study propagates
`ExperimentConfig.planned_scenario_ids` through every engine pass, including support, development,
validation, test and frozen traces; admission is common across that declared set.
Support counters exclude technically unevaluable results even if an execution occurred earlier.

## 4. Client OMS (`sim/oms.py`)

Tracks command records, the client order state (`core.views.ClientOrderState`), reported
cumulative executed quantity, reservations, idempotent report ids (duplicate report ids ignored;
a terminal order cannot be resurrected by a late ACCEPTED), execution ids (each counted once).
Invariant asserted on every transition: `reported_executed + reserved <= 1`. A reservation is
released only by a definitive report (FILL, REJECTED, CANCELLED, CANCEL_REJECTED_TERMINAL,
AGGRESSIVE_UNFILLED, TECHNICAL_RESET).

## 5. Terminal controller (`sim/controller.py`, architecture 7.3)

`enter(now)` is idempotent and absorbing. Behavior by client state at entry and on each
subsequent report (each new command is created at the callback time and incurs computation +
entry delay):
- NONE / REJECTED (no live exposure, aggressive attempt unused): send AGGRESSIVE (qty = 1 -
  reported_executed).
- IN_FLIGHT passive: wait; on ACCEPTED -> send CANCEL; on FILL -> done; on REJECTED -> AGGRESSIVE.
- WORKING: send CANCEL; on CANCELLED -> AGGRESSIVE for residual; on FILL during cancel -> wait for
  the cancel's terminal response, reconcile, no aggressive if residual 0.
- FILLED: nothing. AGGRESSIVE_UNFILLED / aggressive already used: nothing (no retries).
- TECHNICAL_RESET: nothing (task technically unevaluable).
At most one aggressive attempt per world ever (T40, T53). Policies cannot act once in controller.

## 6. Policies (`policies/base.py`, `policies/baselines.py`)

```python
class Policy(Protocol):
    policy_id: str
    def on_arrival(self, view: DecisionView) -> Literal["JOIN_BEST", "SWITCH_TO_TAKER"]: ...
    def on_checkpoint(self, view: DecisionView) -> tuple[CheckpointChoice, DecisionReason]: ...
def checkpoint_eligibility(view: DecisionView) -> DecisionReason
    # REPORTED_COMPLETE if FILLED; INELIGIBLE_PENDING_COMMAND if pending; INELIGIBLE_REJECTED if
    # REJECTED; INELIGIBLE_NOT_WORKING if not WORKING or in controller; INELIGIBLE_PARTIAL if
    # reported_executed not in (0,); else MODEL_CHOICE (eligible).
```
JOIN_BEST: limit = same-side client best bid (buy) / best ask (sell) at the decision; the
`QueueCohortProxy` for the world is created at that decision from the client book. B0:
SWITCH_TO_TAKER at arrival (enters controller). B1: JOIN_BEST; checkpoint returns
`(HOLD, NOT_APPLICABLE)`. At the checkpoint the engine calls `checkpoint_eligibility`; only if
MODEL_CHOICE does it call `policy.on_checkpoint`; otherwise HOLD with the ineligibility reason.
SWITCH enters the controller. At the cutoff `T - G` the controller is entered for every
unresolved world. The DecisionView is built from the client plane only (client book best levels,
client mid, quote age, OMS state, market features from the scenario's `MarketFeatureState`, queue
features from the world's proxy, mechanics features; `price_signal` from `price_scorer(market
features)` if provided, else empty). It never contains m0 or overlay truth.

## 7. Outcomes

At the deadline T freeze: executions with `exchange_time_ns <= T` count (an on-time fill whose
report arrives late counts, T18); status COMPLETED_ON_TIME if executed 1 else DEADLINE_MISS;
TECHNICALLY_UNEVALUABLE from the quality map (or an overlay TECHNICAL_RESET, which the quality
map should already cover). Continue processing the world after T only to drain pending
commands/reports (reconciliation); later executions populate `diagnostic_late_completion_ns`
and never change status or C_T. `z0` = client Mid2 at arrival (from the scenario's client book).
`m_T` = last valid committed historical Mid2 at or before T (`MidSeries`, may be stale; record
age). Reference samples include committed initialization quotes and quiet-gap status changes;
these are state observations, not economic flow. Compute `is_ticks_gross/net` vs m0 (completed
only) and `c_t_ticks` vs z0 using
`qexec.analysis.metrics`. Fill all `TaskResult` fields.

## 8. Label probe forking (T41, T42, T57)

With `fork_label_probe=True` the engine additionally runs a `B1_PROBE` world per task. At its
checkpoint, if `checkpoint_eligibility` is MODEL_CHOICE, it is cloned (`World.clone()`: deep copy
of overlay, OMS, controller, proxy, pending world events and timers; shared books are not
copied) into `HOLD` and `SWITCH` branches which continue in the same pass; the parent stops.
Store one `branch_labels` row per (task, action): task_id, session_id, side, action, miss
(0/1), c_t_ticks, completed, fill_mechanism, plus the checkpoint feature dict (all MARKET,
MECHANICS, QUEUE features at the checkpoint, prefixed by nothing) and checkpoint time. Both
branches must have identical state at the fork (test by comparing clones) and must not affect
each other.

## 9. Outputs (`SessionOutputs`, polars DataFrames)

`tasks` (manifest incl. ineligible with reason), `task_results` (TaskResult columns, one row per
task x policy), `decisions` (task_id, policy_id, time_ns, eligibility, choice, reason),
`executions`, `reports` (exchange and delivery times), `checkpoint_features` (B1-prefix
checkpoint feature rows for every task where a B1-prefix world reached the checkpoint, with
eligibility; used for training/support), `branch_labels`, `support` (per G2-S: for B1 worlds —
decision-eligible checkpoints, post-checkpoint passive fills before the cutoff by
fill_mechanism, fills before the checkpoint), `quality` (quality map).
`trace(task_id, policy_id) -> list[dict]` returns the ordered causal event trace for one world
(arrival, deliveries that changed its state, commands with send/arrival times, reports with
exchange/delivery times, decisions, controller steps, outcome).

## 10. Required tests

T11 (no fill before command arrival; a same-time historical batch is processed before the
arrival), T13 (fill report delayed: client unaware until delivery), T14 (fill during cancel: no
duplicate residual order), T15 (duplicate/late reports idempotent; no resurrection), T16/T39
(controller from every reachable pending state completes within guard or records miss;
enumerate states: NONE, IN_FLIGHT at cutoff, WORKING, CANCEL racing fill, aggressive unfilled),
T18 (on-time fill, late report counts), T38 (timers fire with no market events: build a session
segment with a long event gap), T40, T41 (clone identical, branches independent), T42, T48
(pending/unsupported client checkpoint -> B1 continuation, no leak of unreported fill), T53, T56
(two historical commits before first delivery: client sees the first committed state first), T57
(B3-style label value equals recomputation from the branch's executions with
`analysis.metrics.deadline_value_ticks`). Plus integration tests on a short synthetic session
(e.g. 120 s): B0 and B1 run end to end; every TaskResult status is valid; B0 completes nearly all
tasks; executed quantity <= 1 everywhere; IS/C_T reconcile with executions; `trace()` explains a
passive fill, a cutoff path, a fill during cancellation (if present), a delayed report, and a
deadline miss (construct a hand-built session for the cases the synthetic one lacks); runtime
for a 10-minute synthetic session with B0+B1+probe at L1 < 120 s on this machine (report it).
Hand-built micro-sessions should be written to `tmp_path` with `core.records_io.write_session`.
