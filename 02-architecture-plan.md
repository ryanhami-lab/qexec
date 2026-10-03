# QExec — Architecture Plan

**Version:** 2.1  
**Prepared:** September 27, 2026 · **Revised:** October 1, 2026  
**Status:** Historical research plan. See [current implementation scope](README.md#current-release) for the completed synthetic Python core and remaining work.  
**Companion documents:** [Product Specification](01-product-spec.md) · [Implementation Plan](03-implementation-plan.md)

The product specification is authoritative for quantity, policy identifiers, task outcomes, metrics, and release scope. This document defines how to implement those requirements without mixing observed data, hypothetical orders, and client knowledge.

## 1. Architectural decisions

Build a modular local application. A validated Python research study is the core flagship. A C++ port is an optional systems extension after stable Python models/policies at G5; P1 is a separate optional research extension. Neither is required to make the core a complete project.

| Decision | Rationale |
|---|---|
| Three separate state planes | Prevent historical reconstruction and hidden simulator state from leaking into policy decisions. |
| Single ordered replay stream per instrument | Preserve causality and priority; parallelize independent sessions instead. |
| Native raw files plus derived Parquet | Avoid an unnecessary full second copy of high-volume event data. |
| One-contract, one-passive-attempt tasks | Bound counterfactual and control complexity. |
| Constant nonnegative latency scenarios first | Make timing testable before adding stochastic delays. |
| One core decision checkpoint | Make the queue-information comparison identifiable and the action labels directly testable. |
| Fixed-reference decision accounting | Give B3 and optional P1 the same explicit treatment of completion and cost. |
| Small explicit interfaces | Allow a Python reference, C++ core, and external cross-checks without multiple incompatible semantics. |

## 2. Components and data flow

```text
Vendor files + instrument/status metadata
                   |
                   v
           Vendor adapter / decoder
                   |
                   v
      Canonical records / batch assembler
                   |
          +--------+---------------------+
          |                              |
          v                              v
 Historical direct book          Observation delivery queue
          |                              |
          v                              v
 Hypothetical exchange overlay    Client-observed book
          ^                              |
          |                              v
 Delayed order commands           Causal features + client OMS
          ^                              |
          |                              v
    Command gateway <---------------- Policy
          |
          +-- exchange reports --> delayed report queue --> client OMS

All components --> audit ledger --> task outcomes --> statistics/report
```

### State planes

1. **Historical state:** Reconstructed orders and depth from the recorded source. Hypothetical orders never mutate this book.
2. **Hypothetical exchange state:** Simulated acceptance, queue allocation, fills, and cancellations under the named model.
3. **Client state:** Delivered market observations, received order reports, outstanding commands, and estimates.

The policy receives an immutable `DecisionView` of plane 3. It cannot access planes 1 or 2, even indirectly through a model feature.

## 3. Repository and storage

```text
qexec/
  pyproject.toml
  uv.lock
  CMakeLists.txt          # optional C++ extension
  CMakePresets.json       # optional C++ extension
  configs/
    synthetic/
    data/
    experiments/
  schemas/
  python/qexec/
    adapters/
    reference/
    features/
    labels/
    models/
    policies/
    experiments/
    analysis/
    cli/
  cpp/                   # optional C++ extension
    include/qexec/
    src/
    bindings/
    tests/
    benchmarks/
  tests/
    golden/
    differential/
    integration/
  docs/
  paper/
  data/          # ignored; licensed raw files
  artifacts/     # ignored except explicitly permitted small fixtures
```

Raw vendor files are immutable. Each has a checksum, acquisition query, coverage, SDK/decoder version, and license note. Stream decoded records in bounded chunks; only persist full canonical copies when repeated decoding cost justifies the storage.

Derived tables include `tasks`, `features`, `labels`, `predictions`, `decisions`, `executions`, `reports`, `task_results`, and `quality_intervals`. Version schemas explicitly. Every derived artifact identifies its input and configuration hashes.

Use Parquet with Polars as the initial dataframe implementation. Add DuckDB only where SQL queries provide a concrete benefit. Notebooks are optional exploration/presentation surfaces; the CLI owns reproducible experiments.

## 4. Canonical records and event boundaries

### 4.1 Record contract

```text
CanonicalRecord
  schema_version
  dataset_id
  publisher_id
  channel_id, when present
  instrument_id
  session_epoch
  source_ordinal
  source_sequence, when present
  action
  side
  price_fixed
  quantity
  order_id
  event_time_ns
  capture_time_ns
  flags
  source_file_hash
  source_record_offset
```

A field absent from the source remains explicitly absent. Do not fabricate a sequence number or interpret a publisher/channel gap without checking the source's sequencing domain.

Order keys include publisher, instrument, reset/session context, and order ID. A sequence number or timestamp alone is not a unique record key. `source_ordinal` preserves normalized order exactly.

Store prices in checked integer fixed-point units. Validate tick alignment against time-effective instrument definitions before converting to ticks. Preserve undefined-value sentinels until validation. Midpoints may require half-tick or rational representation; never round the arrival benchmark to an executable tick.

Instrument definitions and trading-status events have separate typed representations. Unknown status is not silently treated as continuous trading. Load the latest valid definitions/status available before initialization and merge subsequent updates by their documented effective timeline. Preserve a shared source sequence when one exists; when separate schemas provide no common sequencing, record the deterministic tie convention, apply effective status/definition updates before same-time order commands, and flag material ordering ambiguity. Deliver status observations to the client only at their own availability times. Future status or revised metadata cannot initialize an earlier state.

### 4.2 Source-specific event effects

For Databento's normalized MBO actions, Add/Modify/Cancel/Clear update resting-book state; Trade/Fill/None do not independently mutate it. Execution attribution is accompanied by book-changing updates. `F_LAST` closes the per-instrument event.[^tracking]

Implement this as a source adapter, not as a universal exchange protocol. The adapter outputs:

```text
EventBatch
  batch_id
  instrument_id
  ordered_records
  exchange_proxy_time
  capture_complete_time
  initialization_or_live
  quality_flags
  semantic_effects
```

`semantic_effects` distinguishes additions, priority changes, reductions, and uniquely counted executions. Preserve links from every effect to the original records.

Never expose an incomplete event to features or strategies. Retain non-mutating records that carry boundary flags. A truncated final batch invalidates that segment until appropriate recovery.

### 4.3 Priority and initialization

Implement the selected adapter's documented treatment of price changes and size increases; do not assume all modifications retain priority. Databento's reference implementation also includes a source-normalization case in which a modification of an unknown order is handled as an addition.[^book]

Such a rule must be explicitly enabled and tested for the chosen adapter. It is not permission to ignore arbitrary missing-order errors. Unknown cancels, impossible reductions, and conflicting adds require diagnosis.

CME normalization preserves priority through published record ordering, including supported snapshots.[^glbx] Preserve that ordering rather than sorting equal-time records yourself. Do not infer original order age from the timestamp of a synthetic snapshot. Separate initialization from economic order flow so snapshot adds do not become trade-signal features.

## 5. Historical book and quality handling

The Python reference maintains an order map and priority-ordered price levels. The C++ version can use an ID-to-handle map, ordered levels, and stable nodes or handles for efficient removal. Avoid raw pointer ownership across the Python boundary.

Required operations:

```text
apply_record(record)
commit_batch(batch_id)
snapshot(depth)
best_bid_ask()
get_order(order_key)
visible_quantity_ahead(order_key)
validate_invariants()
```

Validate nonnegative sizes, unique active identities, level totals, valid price alignment, and priority order. Assess spread/crossing invariants only at committed boundaries and in eligible trading states.

Compare selected committed states against a reference representation. A vendor-generated alternate schema is useful for finding implementation errors, but it is not independent verification of the vendor's raw feed.

Treat missing/corrupt state as a technical gap. Resume only after a trusted reconstruction path, such as a validated snapshot plus replay. Do not silently carry a broken queue forward or interpolate order-level state.

Build the quality-interval ledger independently of policy outcomes. A task whose required observation/execution window crosses an unrecoverable technical gap receives the same technical exclusion in every policy of that comparison. Economic halts and unavailable liquidity remain outcomes. Exclusion decisions and counts are persisted before comparative metrics are calculated.

Legitimate but unsupported economic mechanisms are different from corrupt data. Tag them as model ambiguity, apply the predeclared treatment, and report coverage. Do not quietly exclude inconvenient implied or hidden-liquidity activity and then claim an unrestricted venue result.

## 6. Clock and scheduler contract

### 6.1 Meaning of timestamps

Databento's capture timestamp is not the user's strategy receipt timestamp. For CME, the documented event timestamp represents matching-engine receipt, not an independently measured instant at which every resulting execution became effective.[^timestamps]

The system therefore calls its historical effective timeline an **exchange-proxy timeline**. Snapshot timing is handled separately. For live economic batches, define the proxy and capture-completion times from documented fields and preserve source order. A proposed baseline is the maximum valid event timestamp and maximum valid capture timestamp within the completed batch.

G0 must verify that this choice is causally usable for the selected intervals. A decreasing proxy time, invalid timestamp, or capture-before-proxy inconsistency triggers a diagnostic and an explicit recovery/exclusion decision—not undocumented sorting or clamping. Atomic batching is itself a modeling convention and is recorded in the manifest.

### 6.2 Latency equations

For valid committed data and constant scenario delays:

```text
observation_time = capture_complete_time + added_delivery_delay
send_time        = decision_time + computation_delay
arrival_time     = send_time + entry_delay
report_time      = exchange_effective_time + response_delay
```

All delays are nonnegative integer nanoseconds. Historical exchange-to-capture delay is already represented; do not add it again. A zero-added-delivery scenario is not automatically a zero-feed-latency scenario.

Order response delay applies to acceptances, cancellations, rejections, and fills. It is distinct from entry and feed delays.[^latency]

Scenario values, including the primary L1 tuple, are owned by product section 6.3. They are controlled experimental assumptions, not measured venue/client latencies. The recorded exchange-to-capture delay remains in every scenario. Resolve every configuration to integer nanoseconds.

### 6.3 Deterministic ordering

Within one timestamp, process already scheduled events using a declared priority: historical batch commit, exchange command processing, client delivery, then decisions. Existing working orders may interact with the historical batch; a new same-time arrival is placed after it in the baseline convention.

Use a causal microstep to ensure descendants occur after their causes. A zero-delay command generated by a decision cannot be inserted before the market event that triggered the decision. Bound and test zero-time cascades.

Preserve original historical order within a batch and across equal-time batches. Do not simulate a strategy inside a normalized atomic batch.

The core has exact timers at task arrival, the single decision checkpoint, the terminal cutoff, and the deadline. There is no periodic discretionary loop. Controller callbacks run when the required report arrives and schedule their next command with the same computation and entry delays. At a deadline timestamp, process already scheduled historical/exchange effects before the deadline assessment; an execution at the deadline is on time. Zero-delay descendants retain causal ordering.

A delayed observation must represent the state committed for its original batch. Enqueue immutable deltas or retained immutable state versions at commit; never dereference a mutable live historical book at delivery. Retain only the versions/deltas needed by pending deliveries, and test two commits occurring before the first delivery. The client must receive the earlier state first, without later changes leaking backward.

Stochastic latency is optional. Its transport ordering, sampling keys, and resequencing behavior need an explicit extension. For paired stochastic experiments, use stable per-task/event random keys where appropriate rather than letting different action counts shift one global random sequence.

## 7. Client state, exposure, and terminal control

### 7.1 DecisionView

Expose only delivered depth/order flow, observed quote age, own limit price, send/acknowledgment history, estimated queue features, reported execution totals, command status, and time remaining.

True exchange queue position may be retained in the audit ledger but is not a policy feature. The core estimator is a deterministic **observable cohort proxy**. At passive command creation, freeze the same-price order IDs and quantities from that decision's delivered client book, before computation or entry delay. Decrease/remove this cohort only through subsequently delivered reductions, cancellations, and priority-losing changes under the adapter's semantics; count linked execution/reduction information once. Do not silently place later adds ahead. Record separately the delivered additions associated with the modeled entry interval and an insertion-uncertainty indicator. Its feature schema fixes normalization and clipping. This is an observable proxy, not a guaranteed rank or confidence bound, and cannot inspect the true arrival queue. Order age and shared market features remain outside the removable own-order group.

An oracle diagnostic has a different explicit policy/configuration ID and never appears as a realistic comparator.

### 7.2 Order/report state

Maintain separate command, exchange-order, and client-order records. Relevant transitions include creation, in-flight, accepted, rejected, working, filled, cancel-pending, and cancelled. A fill during cancellation is valid; a late acceptance cannot resurrect a filled order.

Every report has an idempotent report ID and cumulative executed quantity. Execution details also have stable execution IDs, quantity, price, fee, and economic time so cumulative reconciliation and later detail reports cannot recognize a cost twice. On a final cancel response, reconcile cumulative execution before deciding the residual. This modeled gateway contract prevents overexecution when individual fill reports arrive late.

Reserve quantity at command creation, including the scheduled computation period before send. Maintain `reported_executed + reserved_potential_quantity <= 1`; a reservation remains until a definitive response reconciles cumulative execution. A later report can update knowledge but cannot create another execution. The exchange ledger separately enforces total executed quantity at most one. The core permits one potentially live child, one passive attempt, and at most one aggressive attempt. Rejections consume the corresponding attempt; there are no core retries or passive reposts. While any command is unresolved, discretionary policies can only defer to the existing order/controller path.

### 7.3 Terminal controller

`SWITCH_TO_TAKER` enters an absorbing control mode:

1. Freeze discretionary orders.
2. Resolve an in-flight submission if any.
3. Request cancellation of remaining working exposure.
4. Reconcile cumulative execution from terminal responses.
5. Submit only the residual quantity aggressively.

Controller entry is idempotent. Repeated calls, late reports, or the terminal timer cannot reset the aggressive-attempt flag, create a second residual order, or restart an exhausted controller.

Let `c`, `e`, and `r` be the constant computation, entry, and response delays. This section is the authoritative definition of the guard: `G = 3(c+e) + 2r + 1 ns`, with terminal cutoff `T-G`. Its longest path is completion of an unresolved submission, its acceptance report, cancel command and terminal response, then residual aggressive entry. From confirmed working state the required path is `2(c+e)+r`; from flat it is `c+e`. The extra nanosecond avoids making the guard depend on a deadline tie. These bounds assume immediate controller callbacks, bounded scheduled computation, reliable command/report delivery, and one aggressive attempt. A modeled cancel terminates remaining simulated resting quantity at its arrival; an already terminal order returns its cumulative terminal status. Lost messages and arbitrary nonterminal cancel failures require a different guard model. Verify every reachable pending state and zero-delay path. The primary scenario gives `G = 1.400001 ms`.

All core scenarios require `0 < H/2 < H-G`; otherwise reject the configuration for this protocol. The same guard/controller applies to every policy within a scenario. Different latency scenarios may require different cutoffs while keeping task arrivals, deadlines, and arrival benchmarks fixed. A sufficient communication budget does not guarantee that liquidity exists.

The core aggressive primitive is **SYNTHETIC_DIRECT_TOP**: at exchange-proxy arrival, execute one remaining contract at the current valid opposite-side best direct price when the status permits trading and displayed quantity is at least one. Otherwise terminate that attempt unfilled with an explicit reason. It never rests, invents depth, or retries. This is a research abstraction, not a claim to implement CME market-with-protection or an actual exchange IOC order type.

The passive command is a one-contract limit at the same-side best in the client view frozen at its initial decision. At exchange arrival, if valid opposite direct liquidity is executable within the limit, fill at that opposite best and classify it as aggressive. Otherwise accept at its unchanged limit behind all eligible currently resting orders at that price under the admitted FIFO model. Invalid/unknown market status or an invalid limit causes a modeled rejection. A subsequently moving quote never reprices the order. Admission and any unsupported source mechanism are explicit scenario rules audited at G0.

At the deadline, freeze the economic outcome using executions at or before `T` and actual residual quantity at `T`. Drain already scheduled commands/reports afterward only for exposure reconciliation and diagnostic late completion; these later executions do not rewrite deadline success or enter a deadline-horizon model target. Halted markets, absent executable depth, and unbounded latency cannot be solved by inventing a fill.

## 8. Queue allocation and hypothetical execution

### 8.1 Separation from historical state

At simulated arrival, place the virtual order relative to the historical displayed queue under the configured insertion convention. Store which historical orders are ahead. Later orders at the same price normally join behind in the admitted FIFO setting; relevant priority changes update that relation.

Historical state follows the feed unchanged. The overlay records hypothetical allocations separately. Each task is an independent counterfactual; it is not sharing or competing for liquidity with other task probes.

### 8.2 Execution evidence ledger

A trade summary, its resting-order fill attributions, and associated book reductions describe related information, not independent quantities to add. CME may also report a fill attribution for an aggressor that previously rested on the book.[^glbx] Select the appropriate resting side and reconcile attribution before crediting virtual fills.

Build one `ExecutionEvidence` object per uniquely identified economic execution group. It includes per-price quantities, aggressor direction when identifiable, passive allocations, source records, and ambiguity flags. Never infer a unique trade identity just from equal timestamps. Reconcile evidence against the pre-effect queue and ordered intra-batch effects; a post-batch snapshot alone cannot identify whether a reduction was a cancellation or the same execution already accounted for.

Book mutations are applied once. Queue advancement from execution and the corresponding reduction is also counted once. Cancellations can advance position but do not create aggressive volume.

**Mandatory fixture:** Five contracts are ahead. A three-contract execution is followed by its three-contract book reduction. Two contracts remain ahead, and the virtual order is unfilled.

If observed execution exhausts quantity ahead and leaves attributable eligible volume, allocate at most the remaining virtual quantity. Exactly exhausting the queue ahead does not by itself fill the virtual order. A quoted touch does not prove a fill.

The admitted direct-FIFO baseline must handle both same-price and supported trade-through execution. A buy at limit `L` can receive only identifiable opposing sell execution evidence at `L` after quantity ahead, or at a price below `L` showing that direct opposing demand passed its priority position; reverse the inequalities for a sell. Allocate at the virtual order's own limit, not the worse historical print. An execution at a better historical price cannot by itself fill the virtual order. Each execution group's eligible quantity is one budget across all its same-price and through-price records, and the virtual allocation is at most one contract. Identify the displaced historical allocation in the audit ledger while leaving the historical book unchanged.

Required trade-through fixture: all historical quantity ahead of a virtual bid cancels, the historical best bid falls below its limit, and a subsequent supported direct sell execution occurs below that limit. The virtual buy fills at its limit using one unit of that execution evidence. Cancellation, a quote move, or a touch without execution evidence still gives no fill. This case matters because the historical tape does not display the hypothetical order. Unsupported implied/hidden interactions, conflicting priority, or unresolved grouping are tagged under the frozen ambiguity rule; do not fabricate residual aggressor size or join unrelated trades into an invented sweep.

Every virtual fill records a `fill_mechanism` of `QUEUE_DEPLETION`, `TRADE_THROUGH`, `AGGRESSIVE` (marketable on arrival or controller), or `AMBIGUOUS`, derived from the execution evidence that produced it. The product's horizon/support gate (product section 4.4) depends on this classification, so it is part of the execution ledger, not a post-hoc label.

### 8.3 Counterfactual limitations

Replacing a historical allocation with a hypothetical one would have changed the subsequent real book. The immutable-tape model does not enact that change. Matching allocation stops after the first one-contract fill; reports and pending cancellation reconciliation still run. This reduces subsequent simulated interactions, but counterfactual validity remains an assumption.[^replay]

Multi-contract or reposting extensions need additional virtual-consumption accounting and tests. Do not reuse a depleted historical quote to grant multiple hypothetical fills within one task.

## 9. Interfaces and language boundary

A shared policy registry exposes B0 (immediate aggressive), B1 (fixed passive cutoff), B2 (price-aware rule), B3 (one-step action-value comparison), and optional P1 (finite-horizon policy). Every implementation uses the same action validator, client information boundary, and terminal controller. Matched ablations use separate explicit identifiers.

The interfaces below are contracts to implement, not existing SDK methods:

```text
EventSource.iter_batches() -> ordered EventBatch stream
HistoricalBook.apply_batch(batch) -> committed state
ExchangeOverlay.process(batch_or_command) -> executions/reports
ObservationGateway.deliver(now) -> immutable client updates
ClientOMS.apply(report) -> client state
Policy.decide(DecisionView) -> admissible Action
ExperimentRunner.run(config) -> manifest and ledgers
```

Python owns the complete core: reference replay, order mechanics, features, model fitting, policy evaluation, analysis, and orchestration. Optional C++ owns only profiled stable components after Python gate G5 and must preserve its tested semantics.

For that extension, use pybind11 with narrow value/array interfaces and batch across the boundary. No mutable historical pointers are exposed to policies.

Stream each historical session once per compatible clock/execution scenario, fan committed effects out to active independent task overlays, and cache shared causal market features. Buy/sell probes and policies occupy separate counterfactual worlds. Do not reconstruct a complete day from the beginning for every task. For B3 action labels, fork the small task/client/delivery state at the checkpoint while sharing immutable tape/state versions; bound active tasks and pending queues, and report their memory footprint. Optimizing independent probes must not let them consume one another's liquidity.

### 9.1 Policy implementation responsibilities

Decision rules, the risk allowance, tie and fallback conventions, the price target, and the threshold menu are defined only in product sections 6.1 and 6.3; feature groups in product section 6.2. This section assigns implementation responsibilities and must not restate those rules.

- **Timers.** Schedule task arrival, the single checkpoint `t0 + H/2`, the terminal cutoff `T-G` (section 7.3), and the deadline as exact events.
- **Eligibility check.** A single `checkpoint_eligibility(DecisionView)` function, shared by every policy, returns `ELIGIBLE`, `REPORTED_COMPLETE`, or a named ineligibility reason. Policies cannot reimplement it.
- **Action comparison.** B3 and B3_NO_QUEUE call one shared `risk_allowance_choice(p_hat, v_hat, epsilon)` function, which also owns tie and nonfinite/support fallback handling. B2 threshold selection calls the same function on validation aggregates. The function receives `epsilon` from the frozen configuration only.
- **Fallback provenance.** Fallback depends only on current client features and frozen support rules, never on future reference availability or outcome. Record fallback and ineligibility reasons as distinct codes.
- **Feature allowlists.** Each policy variant has an explicit allowlist (section 10.1); B3_NO_QUEUE removes exactly the product's own-order queue group, including derived encodings.
- **Price target.** Label generation computes the product's primary target with `tau` taken from the configuration's frozen L1 value, not from the active scenario's guard; it retains and purges the full outcome window even where it extends beyond a task deadline in a sensitivity scenario.
- **Logging.** Persist per-decision `p_hat`, `v_hat`, the allowance set, the chosen action, and the reason, so that sensitivity across the frozen `epsilon` set can be recomputed from stored predictions without rerunning replay.

## 10. Features, labels, and predictive models

### 10.1 Features

Start with spread, same/opposite displayed depth, imbalance, recent delivered order flow, observed quote age, short-horizon volatility, own limit distance from the observed best, order age, estimated queue state, and time remaining. Define every lookback and normalization in the feature schema.

Each row stores `task_id`, `decision_time`, maximum input-availability time, feature version, and source window. Realized future order arrivals, exact simulated insertion position, and unreported fills are prohibited.

The leakage test changes events available only after a decision and verifies that earlier features/actions remain unchanged. Labels and audit-truth columns live in separate tables and are excluded by an explicit feature allowlist.

### 10.2 Labels

`REPLAY_PROBE` labels follow a fixed declared action/controller under a named model and include the feature time, action arrival, outcome window, fill time, and completion status. They train predictions of that experiment's counterfactual outcomes—not actual exchange fills.

For each supported training checkpoint, fork identical B1-prefix historical-reference, hidden-overlay, client, and scheduled-command/report states into HOLD and SWITCH continuations. Preserve the same subsequent tape, scenario, and outcome horizon. Generate both action targets from these paired branches, including already executed but unreported quantity and price/fee details. Hidden state is used to produce labels only; the model row contains the pre-action client view. Test that the branches are identical before the action and cannot affect one another. A fresh-submission fill dataset cannot substitute for these aged-working-order action labels.

`HISTORICAL_ORDER` labels track real observed orders under their actual lifecycle. Cancellation, modification, end-of-data censoring, and fill are distinct. Participant cancellations can be informative. Never treat cancellation as proof that an order would not have filled if held, or use an unqualified non-informative-censoring assumption.

A fixed-horizon fill model may predict from submission-time features. A time-varying hazard model can condition only on covariates delivered so far; it cannot integrate a future realized feature path when producing a present-time forecast. Records derived from the same probe stay together in temporal splits.

Price labels must specify direction/horizon and tie handling. Fixed-horizon targets retain a no-change class or a declared alternative; do not silently discard unchanged prices. Next-move labels need an explicit maximum horizon and censoring rule.

### 10.3 Artifact contracts

Every trained model records feature names/types/order, training period, preprocessing, calibration method, random seed, model parameters, dependency versions, code commit, and source dataset hashes. Schema mismatch is a hard error.

For the interpretable B3 baseline, fit one binary miss model and one regularized mean-cost model per action on those paired rows, with a frozen feature schema and validation procedure. The same estimator families and tuning allowance apply to B3_NO_QUEUE. A fill-probability model remains a separate diagnostic and cannot replace the full-action cost target. Report miss calibration/proper scores, cost error, action support, predicted action gaps, and realized paired consequences. If an action's training labels contain only one miss class, use the recorded empirical constant for that component and disclose its sample size/uncertainty; an observed zero rate is not evidence of true zero risk.

If the common price model's score is a B3 feature, generate forward-chained out-of-fold scores on B3 training rows, with full label-window purging, or fit the price model on a disjoint earlier slice. Never feed in-sample fitted price scores into second-stage training. Validation predictions use a price model fitted only on the permitted training data. Any final refit on combined development data must regenerate honest second-stage training scores and freeze the entire resulting pipeline before the test is opened. Queue features cannot enter the shared market-only price model indirectly.

## 11. Shared decision accounting and optional finite-horizon policy P1

### 11.1 State and action scope

P1 is an optional extension that tests sequential hold/switch optimization after the same initial join, under the same task, controller, and information boundary. Specify its additional decision grid before fitting; it gains no reposting, larger quantity, or better execution primitive. Use a compressed *observable* state:

```text
x_k =
  time remaining
  reported remaining quantity
  command/controller state
  observable command ages and remaining deterministic timers
  passive attempt used or unused
  own-limit position relative to an initial client reference
  observed-mid position relative to that same reference
  observed spread/imbalance regime
  estimated queue/age state
  optional price-signal bucket
```

Model all pending states, or delegate them to the shared deterministic controller with no discretionary action. Never let the solver cancel and cross instantaneously.

Transition observations come from controlled action probes and rollouts of admissible policies on training data. Include the full hidden simulator state only for producing outcomes; fit the model using observable `x_k`. Sample relevant order-age and pending-state histories, rather than fitting all transitions from fresh orders.

State compression generally does not establish a true Markov property. Document the approximation and its deployment-distribution dependence. Store support counts; coarsen or smooth on development data. Unsupported P1 states fall back to the frozen B1 hold-to-cutoff path/controller, with frequency reported; calling a checkpoint-only B3 model at an unsupported age is not a valid fallback. Do not claim exact optimality for the real partially observed market. Pending-state rollouts retain all scheduled events even when the compressed policy cannot observe them.

### 11.2 Fixed-reference cost accounting

Let `z0` be the initial client-observed midpoint for this task/scenario. It is fixed independently of policy decisions. Task warmup must establish it before the first decision.

For a completed task, cost relative to `z0` differs from the product's primary benchmark `m0` by a task-specific constant:

$$
IS(m_0) = IS(z_0) + s\frac{z_0-m_0}{\delta}.
$$

Thus policies can optimize relative to an observable reference without receiving the evaluator's hidden arrival midpoint. Include price displacement from `z0` in the modeled state; do not quietly drop it while assuming it is Markov.

Define step cost as fees and signed execution cost relative to `z0`, recognized when the client receives a fill report. Economic ledgers retain the actual execution time separately. Deduplicate reports. At the final boundary reconcile and recognize any already executed but unreported cost exactly once.

B3's whole-action labels and P1's summed step/terminal costs both use the deadline-horizon pair `(d, C_T)` defined in product section 8. Implementation requirements for that target:

- `m_T` is the last valid committed historical direct midpoint at or before `T`; retain its age and stale/status flags without applying a freshness cutoff to this auxiliary valuation. The valid arrival midpoint `m0` ensures that such a reference exists.
- A source-corruption interval makes the task technically unevaluable under the policy-independent quality map; it does not trigger a future-informed action fallback.
- Post-deadline diagnostic fills are excluded.
- For P1, the sum of step costs plus the terminal residual valuation must equal `C_T` exactly (test T57). The `m0` versus `z0` offset remains task-constant because executed plus residual quantity equals one.

This accounting convention avoids a moving-midpoint rebase term. **Do not mix this fixed-reference backup with an earlier moving-reference formula or add markout again.**

### 11.3 Completion-aware constrained objective

P1 must not optimize cheap incomplete trades, and it cannot simply reuse B3's per-decision allowance. Applying `epsilon` independently at each of `K` decisions lets estimated miss risk accumulate across steps, so it does not preserve a task-level risk budget.

P1 instead solves a constrained problem in its estimated finite model: minimize expected `C_T` subject to estimated task-level miss probability at most `min_pi P_pi(miss) + epsilon`, where the minimum is over admissible policies from the initial state and `epsilon` is the product's frozen allowance. Predeclare one of these formulations before fitting:

- **Budget-in-state:** augment `x_k` with the remaining risk budget and solve by dynamic programming over a discretized budget grid; or
- **Lagrangian:** minimize `E[C_T] + lambda * P(miss)`, choosing `lambda` on development data as the smallest value meeting the constraint, then freezing it.

Either way, step costs have zero failure component and the terminal failure component is the actual miss indicator in a probe, or its conditional expectation in the estimated model. For the terminal cost component, include unreported execution cost plus the residual **valuation** from product section 8. The residual mark is not a fill, is not counted as completion, and is not primary realized shortfall. For completed tasks this target reduces to the ordinary fixed-reference execution cost.

Exact ties choose SWITCH when admissible. The policy is constrained-optimal only in its estimated finite model; rare-event miss estimates may be unreliable and require support diagnostics. Empirical all-zero miss labels do not establish zero-risk actions. With `K = 1` decision and no budget carry-over, either formulation must reproduce B3's choice for the same predictions (test T30).

At the terminal cutoff the action set becomes the shared completion controller. The terminal boundary must account for all pending commands and unreported fills without exposing that hidden state to the policy. Rollout targets may use simulator truth to evaluate consequences, just as offline labels may use future data.

### 11.4 Required solver tests

- Two-step toy problem agrees with exhaustive enumeration.
- Fixed-reference costs reconcile to the economic ledger and `m0` metric.
- Delaying reports does not lose or double-count execution cost.
- Terminal residual marking does not create executions or completion.
- P1 meets its declared task-level risk constraint in the estimated model; a lower-cost policy violating the constraint does not win.
- With one decision and no budget carry-over, P1's constrained choice equals B3's risk-allowance choice on identical predictions.
- Unsupported states use the recorded fallback, never a zero-cost invented transition.
- P1 and B3 call exactly the same order controller and fill rules.
- B3 whole-action labels equal the summed P1 accounting on the same forced continuation.
- Unreported fills present at a checkpoint remain identical in both action branches and never allow duplicate exposure.

## 12. Experiment, analysis, and reproducibility contracts

A validated configuration resolves instrument metadata, data hashes, task manifest, eligible sessions, feature/model/policy IDs, latency values, fill rule, fee scenario, exact core decision timers or optional P1 grid, terminal rule, seeds, and output paths. Unknown keys fail validation; units are explicit. Product section 6.3 owns scenario IDs L0–L6 and the fixed-policy versus retrained-policy distinction.

Use separate commands for development and final-test runs. Final runs require a freeze manifest. Analytical code reads immutable task ledgers and never silently reruns strategy logic.

Each run directory contains:

```text
manifest.json
resolved_config.json
environment_lock_copy
input_checksums.json
model_and_policy_hashes.json
task_manifest.parquet
decisions.parquet
executions.parquet
reports.parquet
task_results.parquet
quality_summary.json
metrics.json
figure_inputs/
logs/
```

For the final-test artifact, record its first unblinding and subsequent access. A local flag is a workflow guard, not a security guarantee.

The paired analysis preserves session clustering and common-task matching. Report both policy-independent technical losses and policy-dependent deadline outcomes. Do not let a failed run appear as an empty, successful experiment.

Logical determinism means identical discrete states, actions, executions, and metrics under a fixed environment/configuration. Compressed-file bytes or learned floating-point model outputs need not be identical across different library/platform versions. Specify numerical tolerances where needed.

## 13. Build-versus-buy and development environment

| Component | Proposed tool or decision |
|---|---|
| Vendor acquisition/decoding | Official Databento SDK; do not reimplement the wire format |
| Research numerics/models | NumPy and scikit-learn; one optional boosted-tree package |
| Tables | Parquet/PyArrow and Polars; DuckDB optional |
| Optional C++ build | C++20, CMake, Ninja; pin tested toolchains after Python G5 |
| Optional binding | pybind11 |
| Tests | pytest; Catch2 for the native extension; optional property-based testing |
| Python quality | Ruff and one type checker |
| Optional C++ quality | clang-format, clang-tidy where useful, address/undefined-behavior sanitizers |
| Packaging | Python lockfile; pinned native dependencies only for the extension |
| CI | GitHub Actions on synthetic fixtures only |
| Report | Markdown and LaTeX or Typst |

These are proposed selections, not a claim that arbitrary latest versions are compatible. Select and lock a tested environment during WP0.

The Python core should run in a pinned local environment with portable paths. For the optional C++ workflow on Windows, Linux through WSL2 is a reasonable first target. Do not assume a particular local username or directory, and state the actual benchmark environment.

Build the event semantics, client/exchange separation, execution controller, audit ledger, research task generator, and policy/model integration. Reuse infrastructure that is not the contribution.

HftBacktest is a useful comparison implementation, not a correctness oracle. Align data and assumptions before comparing outputs.[^replay] Do not simultaneously adopt another engine and build a second one without a clear validation purpose. Avoid brokers, distributed services, MLflow servers, and paid compute unless profiling or collaboration creates a concrete need.

## 14. Testing, performance, and operations

The core requires unit tests, hand-derived golden traces, and a small real-data integration audit. The optional native extension adds Python/C++ differential tests and sanitizers. Shared implementations can share a bug, so differential agreement does not replace independent fixtures.

Benchmark separately: decoding, book application, overlay mechanics, feature computation, and bindings. Record hardware, operating environment, build flags, data hash, warmup, repetitions, runtime, and peak memory. Offline replay throughput is not an exchange round-trip latency measurement.

Optimize algorithms/data layout first, then allocations and boundary overhead. Parallelize independent days, probes, or experiment scenarios with stable IDs. Do not force intra-book parallel execution at the cost of event ordering.

Core CI runs synthetic end-to-end experiments and Python lint/type checks. Add native tests, sanitizers, and binding builds only when the optional C++ extension exists. No licensed data or API credentials are needed for the public smoke test.

Structured diagnostics identify run/task/order/batch IDs, clock type, transition, cause, and source location. A `trace task` command reconstructs each decision and fill. Debug logging may be expensive but must not change semantics.

## 15. Data governance and implementation-agent constraints

Raw data, credentials, and private model artifacts are gitignored. Never embed an API key in a config or notebook. Publish only fixtures created by the project or explicitly licensed for redistribution. Acquisition scripts and metadata instructions can substitute for distributing paid data.

An implementation agent must read all three documents, implement one gated work package at a time, and report executed tests honestly. It must not fabricate data, mark incomplete tests as passed, alter the final holdout to obtain favorable results, loosen timing rules, add paid dependencies, publish externally, or claim novelty without evidence.

Any change to quantity, action scope, metrics, source interpretation, or failure treatment requires a recorded decision and coordinated document updates.

## 16. Primary references

These references establish source semantics and known implementation limitations. The component design and solver conventions are project decisions.

[^tracking]: Databento, *State management of resting orders*. https://databento.com/docs/examples/order-book/order-tracking
[^book]: Databento, *Limit order book construction*. https://databento.com/docs/examples/order-book/limit-order-book
[^glbx]: Databento, *CME Globex MDP 3.0*, including priority, event normalization, and trade attribution. https://databento.com/docs/venues-and-datasets/glbx-mdp3
[^timestamps]: Databento, *Matching engine latencies*. https://databento.com/docs/examples/algo-trading/latency
[^latency]: HftBacktest, *Latency Models*. https://hftbacktest.readthedocs.io/en/latest/latency_models.html
[^replay]: HftBacktest, *Order Fill*. https://hftbacktest.readthedocs.io/en/latest/order_fill.html
