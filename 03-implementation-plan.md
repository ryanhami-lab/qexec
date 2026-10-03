# QExec — Implementation Plan

**Version:** 2.1  
**Prepared:** September 27, 2026 · **Revised:** October 1, 2026  
**Status:** Historical research plan. See [current implementation scope](README.md#current-release) for the completed synthetic Python core and remaining work.  
**Companion documents:** [Product Specification](01-product-spec.md) · [Architecture Plan](02-architecture-plan.md)

> This is the original delivery plan for the full market study. The later authorized synthetic Python release has its own [engineering contract](docs/engineering_contract.md) and [completion review](docs/remediation.md). Real-data milestones and optional extensions below remain future work; the plan is not a completed-milestone checklist.

## 1. Delivery strategy and effort estimate

The first milestone is a **Python vertical slice**: a validated real-data reconstruction, delayed hypothetical order, B0/B1 comparison, and auditable cost calculation. Do not spend the opening months optimizing an engine without an execution experiment.

The **Python research core is the complete flagship**: a reproducible study of the incremental execution value of observable own-order queue estimates. It satisfies R01–R10 without C++. A validated C++ implementation is an optional systems extension (R11); an implemented and evaluated finite-horizon P1 is an optional research extension (R12). A negative P1 feasibility finding can strengthen the core report, but does not satisfy R12.

| Package | Scope | Focused hours, planning estimate |
|---|---|---:|
| WP0 | Research contract and data feasibility | 15–25 |
| WP1 | Decoder, event batches, Python book | 25–40 |
| WP2 | Python execution overlay, first experiment, G2-S support gate | 80–130 |
| WP4 | Features, paired action labels, temporal splits | 20–35 |
| WP5 | Models and execution-aware policies | 30–50 |
| WP6 | Robustness and final evaluation | 25–40 |
| WP7 | Report, repository, technical presentation | 25–45 |
| **Python core** | **WP0–WP2 and WP4–WP7** | **220–365** |
| Optional WP3 | C++ port and differential validation | 50–90 |
| Optional GA | Finite-horizon constrained P1 and solver validation | 60–120 |
| **All packages, before contingency** | **Core plus both extensions** | **330–575** |

Allow 25% contingency: **275–456 focused hours** for the Python core (about **23–38 weeks at 12 hours/week**), or approximately **413–719 hours** with both extensions (about **34–60 weeks**). WP2 was doubled in revision 2.1 because it combines the scheduler, latency queues, client OMS, virtual queue allocation, trade-through handling, terminal controller, checkpoint/restore, and most G2 tests—unusually difficult integration work for someone learning these concepts simultaneously. These are planning judgments, not measured delivery guarantees. Working Python, probability, statistical learning, and debugging skills are assumed; prerequisite learning is additional. Native development also assumes basic C++. Re-estimate after WP2 and G5 using measured implementation speed, data volume, runtime, and model support.

Do not defer applications until P1 or a preprint is finished.

## 2. Non-negotiable development rules

- Use quantity `1`, one potentially live child, and at most one passive submission per task. No time slicing or passive reposting in the core.
- Preserve the source event order and completed-event information boundary.
- Keep simulator-truth and client-observed state separate.
- Never count execution attribution and its book reduction twice.
- Use one shared completion controller, fee convention, and task manifest across policies.
- Keep the primary B3 versus B3_NO_QUEUE population fixed when optional policies or extensions are added.
- Core timing uses explicit task, checkpoint, and terminal timers; do not add periodic policy decisions or passive reposts.
- Never fabricate fills, impute favorable missing outcomes, or use the holdout to choose a flattering design.
- Retain negative, ambiguous, and deadline-failure results with their coverage counts.
- Do not buy data, incur compute charges, upload licensed data, or publish the repository without the user's authorization.

External libraries are encouraged for decoding, modeling, storage, bindings, and testing. Build the research semantics and validation that make the project distinctive.

## 3. WP0 — Research contract and data feasibility

**Dependencies:** None.  
**Output:** `docs/research_contract.md`, `docs/source_semantics.md`, pilot data manifest, budget estimate.  
**Gate:** G0.

### Work

1. Register B3 versus the matched `B3_NO_QUEUE` as the primary comparison. It estimates incremental value of own-order queue estimates within the declared model, beyond common depth, imbalance, order age/status, and the shared price signal. B2 versus B3 and P1 versus B3 are secondary comparisons.
2. Define the completed-task shortfall formula, failure statuses, and limits of replay claims.
3. Obtain the three contrasting pilot sessions defined in product section 4.2 for an explicit proposed `GLBX.MDP3` contract, including initialization history, definitions, status, and any comparison schema needed for the audit. Inspect availability/condition metadata and request `metadata.get_cost`, `get_record_count`, and `get_billable_size` estimates before acquisition. Specify schema, explicit symbols, inclusive/exclusive UTC bounds, and initialization scope; never omit the symbol filter and accidentally request the full dataset. Record the approved spending ceiling and stop before exceeding it. Cache immutable downloads; repeated streaming requests can incur repeated charges.[^metadata]
4. Inspect source actions, `F_LAST`, snapshots, priority, execution attribution, timestamp quality, and market status using current primary documentation.[^glbx][^tracking]
5. Verify product-specific allocation, implied/hidden mechanisms, and the aggressive-order abstraction that the study can support.
6. Audit whether the proposed exchange-proxy and capture timelines are causally usable without reordering the source.
7. Lock the core operating contract: the product section 6.3 configuration, the architecture section 7.3 guard and controller, status rules, queue estimator, task grid, fee scenario, and technical exclusions. Write the G2-S support minima and the provisional primary `epsilon` with its justification into `docs/research_contract.md` before any replay probe runs. Changes after the pilot require a versioned decision before model tuning; these settings are assumptions to test, not measured exchange latencies.
8. Choose and lock a tested Python environment. Establish a synthetic fixture that needs no credentials, an input-file deduplication rule, and a local storage/runtime budget. The pilot estimate must cover the intended study, derived features, audit traces, and temporary disk headroom.
9. Build a focused related-work table: existing question, data, method, limitations, and the narrower contribution this project would investigate. Document what has actually been read; do not claim novelty from missing or unavailable material.

### Core operating contract

The configuration values (task grid, horizon ladder, delays, price target, menus, `epsilon`, study-size seed) are owned by product section 6.3; the guard `G` and controller by architecture section 7.3; policy rules by product section 6.1. WP0 locks them; it does not redefine them. Implementation obligations specific to this plan:

- Prove guard sufficiency for every reachable modeled state in WP2 (T16, T39) rather than treating the formula as a guarantee about a real gateway. Added feed delay affects information, not the command-path guard.
- Reject at configuration validation any scenario/horizon failing `0 < H/2 < H - G`.
- Use exchange calendars and timezone-aware conversions; the task window is narrower than the CME trading day (T51).
- Treat the 60-session split as a budgeting seed. Estimate feasible final-test size from development session-level paired variability (product section 10). Freeze revisions before unblinding.

### G0 acceptance

You can explain an actual order lifecycle, distinguish execution information from book changes, preserve priority at initialization, identify unobservable mechanisms, and estimate research cost/storage. Interpretations essential to queue allocation, clocks, or the primary claim must be resolved or explicitly represented by justified alternative models before G0 passes. A to-do item is insufficient evidence. Only noncritical questions may remain open, each with an owner, impact, and resolution step.

**Stop or redirect:** No genuine order-level information; uncertain priority or execution attribution essential to the claim; unsuitable license/cost; or a clock/model assumption that cannot support the proposed experiment. Changing the data early is preferable to building a sophisticated invalid simulator.

### Learning checkpoint

Explain conditional expectation, price-time priority versus other allocation, implementation shortfall, adverse selection, and the difference between actual order outcomes and hypothetical held-order outcomes. Learn these before attempting a hazard model or dynamic program.

## 4. WP1 — Canonical ingestion and Python reference book

**Dependencies:** G0.  
**Output:** Canonical iterator, batch assembler, `ReferenceBook`, golden fixtures, real-data anomaly report.  
**Gate:** G1.

### Work

Implement the canonical record schema and preserve flags, source ordinal, IDs, timestamp types, missingness, and lineage. Stream from native files rather than materializing the entire study into memory.

Build the completed-event assembler. Non-mutating records can close a batch; snapshot records initialize state but do not become economic feature flow. Add truncated-batch handling.

Implement the Python order map and priority-ordered price levels. The source-specific adapter decides which actions change the book and which modifications affect position.[^book] Test size increases, price changes, priority-preserving reductions, snapshot timing, display-quantity refresh ambiguity, and instrument/session identity. Detect duplicated records and overlapping acquisition files before replay rather than deduplicating by timestamp alone.

Write hand-derived fixtures before relying on a vendor reference or a second implementation. Compare selected real-data committed states against an available reference representation and account for every discrepancy.

### G1 acceptance

Golden states agree exactly. Accepted intervals have no unexplained lifecycle or quantity errors. Reset/snapshot recovery works. No strategy/feature API can observe an incomplete batch. Anomalies are explained or excluded with fixed coverage rules—not ignored.

## 5. WP2 — Python overlay and first end-to-end experiment

**Dependencies:** G1.  
**Output:** Client/exchange separation, scheduler, queue overlay, terminal controller, B0/B1 ledgers.  
**Gate:** G2.

### Work

1. Implement the scheduler, exact timers, and causal microsteps using the primary constant latency scenario. Task arrival, checkpoint, terminal cutoff, and deadline are distinct scheduled events. Scheduled terminal control must fire even if no new market event arrives.
2. Implement observation delivery, order entry, and delayed reports as distinct mechanisms. Response delay applies to fills as well as acknowledgments.[^latency]
3. Implement the client OMS and exposure reservation for unresolved commands.
4. Implement the single passive attempt, fixed-price maintenance, and absorbing cancel-and-complete controller. Derive and test the guard for every pending state. Allow one aggressive attempt and no retries in the core. Controller entry is idempotent: if an early aggressive attempt rejects or fails to execute, the terminal timer cannot restart the controller or grant a second attempt.
5. Build execution-evidence accounting and virtual queue allocation, independent of historical mutations.
6. Persist a policy-independent task manifest and evaluator arrival benchmark. Ensure an initial local reference quote is available for every planned scenario from pre-task warmup.
7. Implement B0 and B1, including a passive limit that arrives marketable and the declared aggressive-order behavior. B0 sends aggressively at arrival; B1 joins at arrival and maintains its fixed-price order until the common terminal cutoff. A rejected passive submission leaves no opportunity to repost.
8. Emit task traces and shortfall/failure results. Hand-check selected tasks. Drain reports sufficiently to reconcile all executions and fees without changing whether execution occurred before the deadline. Define technical recovery during a live task; a new snapshot must not silently restore an unjustified hypothetical queue position.
9. Provide deterministic full-state checkpoint/restore for action-label construction: historical position, exchange overlay, client book/OMS, queued deliveries/commands, timers, and random state. A cloned branch may not lose a delayed report or alter the source cursor.
10. Record `fill_mechanism` on every virtual fill (architecture section 8.2) and run the G2-S support diagnostic (product section 4.4) with B1 probes across the horizon ladder on all three pilot sessions. Apply the selection rule frozen at G0; do not compute or inspect policy cost comparisons as part of this step.

### G2 acceptance

A small real-data baseline study runs from source to paired results. Traces explain a passive fill, fixed cutoff, fill during cancellation, delayed report, rejected/unfilled aggressive attempt, and deadline miss. No order fills before arrival; no unreported fill leaks into a decision; unresolved exposure cannot be duplicated.

**G2-S:** The support report lists, per session and horizon, decision-eligible checkpoints and post-checkpoint fills by mechanism. The primary horizon is selected by the frozen rule, or the study is redirected/narrowed. Full development-data acquisition is not authorized until G2-S passes and the user approves the resulting cost estimate.

This is the first demonstration milestone. It is not yet permission to claim live realism or a strategy edge.

## 6. Optional WP3 — C++ systems extension

**Dependencies:** Stable G5 contracts; profiling may begin earlier.  
**Output:** C++ core, Python bindings, differential suite, benchmark report.  
**Gate:** G3.

### Work

Port stable book, scheduler, overlay, and measured hot paths without changing semantics. Do not make this package a dependency of the Python release. Use C++20, CMake/Ninja, Catch2, and pybind11 as the initial native stack. Pin actual tested versions.

Compare both engines on committed book state, order/controller state, decisions, executions/reports, paired action-probe labels, and final metrics. Rerun the frozen comparison with the same data, policies, and manifests before advertising native results. Run hand-derived cases in addition to differential tests: two implementations can agree on the same mistake. If the port changes research behavior, diagnose it; do not retune the model to hide a parity failure.

Enable address/undefined-behavior sanitizers in suitable debug jobs. Test integer overflow boundaries and object lifetime at the binding interface.

Benchmark decoding, book updates, overlay processing, feature extraction, and boundary overhead separately. Record hardware, operating environment, build flags, input hash, repeated-run dispersion, and peak memory. Profile before adding custom allocators or specialized containers.

### G3 acceptance

Exact discrete-state parity on deterministic fixtures, agreed numerical tolerances where applicable, no sanitizer failures, a synthetic CI path, and reproducible performance measurements. No arbitrary speedup threshold is required; claims must reflect the measurements.

The first Linux environment may be WSL2 on Windows. Do not pretend those measurements establish colocated hardware latency or native Linux performance under a different configuration.

## 7. WP4 — Features, labels, and evaluation splits

**Dependencies:** G2. G3 is needed only for results that use the optional native engine.  
**Output:** Versioned feature/label schemas, dataset builder, split manifests, leakage tests.  
**Gate:** G4.

### Work

Implement a small online-equivalent feature set. Store the maximum availability time of every input. Explicitly exclude simulator-truth queue position and unreported execution state. Implement the architecture's causal own-order cohort proxy from IDs already delivered when the command is created, subsequent delivered reductions, and uncertain later adds; its bands are modeling estimates, not guaranteed bounds on the real insertion point.

Construct the primary `REPLAY_PROBE` training examples by running the common B1 prefix to the checkpoint. Branch the identical full simulator/client/scheduler snapshot into two worlds: `HOLD` maintains the existing order until the common terminal cutoff; `SWITCH` immediately enters that same terminal controller. Advance both branches against the same future historical tape to the task deadline. This is a single-checkpoint experiment, not transitions sampled from unrelated historical participant decisions.

Store both arms' deadline-miss indicators, execution-value labels, completion flags, execution/report times, and label provenance. Execution value includes costs of executions occurring by the deadline plus the declared fixed-reference midpoint valuation of residual quantity at the deadline; it includes failures as well as completions. Exclude late diagnostic fills from this target, and count on-time fills with late reports once. Use the last valid committed midpoint at or before the deadline, retaining the valid arrival midpoint if no later quote exists, and report reference age. Stale references remain explicit valuations; do not introduce a future freshness rule that changes checkpoint eligibility or fallback. Source corruption uses the policy-independent technical-coverage rule. The valuation never creates a fill and is never presented as realized shortfall. A paired difference target may supplement, but may not replace, the two arm records.

Branch only checkpoint states eligible for a discretionary choice. Unresolved command states continue B1; reported-complete states require no choice. Preserve their outcomes in the task population. Train on exactly the decision-time information the policy receives, including the realistic order age and estimated queue state after the common prefix. Submission-time fill predictions alone do not supply these continuation labels.

If feasible, add a separately labeled `HISTORICAL_ORDER` diagnostic dataset. Preserve cancellation, modification, censoring, and unknown outcomes. Do not label a cancelled actual order as a hypothetical non-fill-if-held. Historical-order outcomes do not replace the paired replay branches.

Generate the primary and secondary price labels defined in product section 6.3 from checkpoint `t*`, with the primary `tau` taken from the frozen L1 configuration. Record the target timestamp convention, freshness rule, and complete outcome windows. Keep unchanged prices; do not condition the prediction sample on a subsequent move. Both arms of a task, buy/sell probes sharing an arrival, and any repeated rows stay together in chronological session splits.

Choose chronological train/validation/test periods and preserve session blocks. Fit transforms and bins only on permitted data. For the price score used as a B3 feature, produce training scores through forward-chained out-of-fold fitting and calibration, or a disjoint earlier training slice; in-sample predictions leak fitting information into the second-stage learner. Validation/test scores come from price models fitted only on allowed earlier data. Store the score's producer, fitting/calibration period, and feature timestamp. The queue and no-queue variants receive identical price scores.

Store technical coverage exclusions independently of strategy success. Final-period availability and source-quality metadata may be inspected before unblinding; record access and keep performance sealed. Do not choose favorable final periods after inspecting outcomes.

### G4 acceptance

Changing future-only events does not alter earlier features/actions. No feature uses an input arriving after the decision. Outcome-window boundary tests pass. A dataset rebuild identifies identical inputs, schema, and split rules.

Before G4 passes, freeze numeric support thresholds using development data: minimum training/validation session coverage, usable checkpoint states per action, class counts, permitted model complexity, out-of-support rules, and a maximum unsupported-state fraction for claims. Set the final-test session target and uncertainty goal from between-session variation. Freeze the primary risk allowance `epsilon` (product section 6.3) with a written economic justification informed by development miss-rate support, not by validation policy outcomes. Record the rationale; millions of correlated rows cannot substitute for independent coverage. A failed threshold triggers simpler models, more approved development data, or a narrower descriptive result, not silent threshold relaxation.

## 8. WP5 — Models, B2, and B3

**Dependencies:** G4.  
**Output:** Versioned predictive artifacts, calibration diagnostics, B2/B3 implementations, validation report.  
**Gate:** G5.

### Work

Fit the interpretable price classifier and fixed-horizon fill diagnostic. Use chronological validation, proper scoring rules, reliability plots, discrimination, and session/regime coverage. Fit an action-conditioned deadline-miss model and full execution-value model for B3 from both probe arms. Constant-prediction models are acceptable where the data do not support fitted effects; a missing class must never silently become a validated zero-risk assertion. Add one optional nonlinear comparator only after the simple models and shared tuning allowance are frozen.

Measure post-fill markouts descriptively by predeclared regimes. Keep this diagnostic separate from the implementation-shortfall objective. Fill calibration against replay labels is not independent exchange-fill validation.

Implement B1, B2, B3, and B3_NO_QUEUE exactly as defined in product section 6.1, with menus and `epsilon` from product section 6.3 and the shared functions assigned in architecture section 9.1. Implementation obligations:

- B2 threshold selection and B3/B3_NO_QUEUE decisions call the same `risk_allowance_choice` function with the same frozen `epsilon`; selection never uses completed-only cost.
- Freeze the primary `epsilon` and its written justification at G4, before validation selection. Evaluate the full frozen `epsilon` sensitivity set from stored predictions; report decision agreement and action shares at each value, including the `epsilon = 0` and `epsilon = 1` boundaries.
- Maintain an explicit feature allowlist per variant (T45).
- Distinguish ineligibility, numerical fallback, and support fallback in counts. A model/feature schema mismatch is a run error, not a numerical fallback.
- Do not learn a low-cost action by deleting its failed-completion labels. Track every attempted threshold/model variant.

Run matched queue-feature ablations without changing controller, tuning budget, task set, or execution model. Compare action agreement/disagreement, full-population miss rates, common-completion execution cost, support, and fallback counts, not just AUC. Any conditional branch analysis is descriptive; it must not replace the prespecified task population.

### G5 acceptance

Artifacts declare data, label arms, features, calibration, numeric support thresholds, fallback rules, tuning trials, and code versions. Forward-chained price-score provenance is verified. Predictions are interpretable and evaluated out of sample. Inspect how often estimated miss-probability gaps exceed `epsilon` and whether those gaps are stable across sessions and refits; decisions driven by unstable gaps must be reported as such. State which models work and which do not; the decision rule does not validate its estimated risks.

A poor or unstable model can pass the *research* gate as a documented negative finding; it cannot be described as a successful predictive result. First investigate correctness, then accept the evidence instead of adding complexity until a backtest improves.

## 9. Optional GA — Finite-horizon research extension

**Dependencies:** G5, using the validated Python engine or G3-validated native engine.  
**Output:** Toy solver, empirical transition builder, P1 artifact, support/fallback analysis.  
**Gate:** GA.

### Work

1. Solve a two-step one-contract toy problem by hand and by exhaustive enumeration.
2. Implement the architecture's fixed-reference accounting using the initial client midpoint. Verify the constant offset to the evaluator's arrival benchmark.
3. Implement the constrained objective of architecture section 11.3 (budget-in-state or Lagrangian, predeclared): minimize expected `C_T` subject to a task-level estimated miss-probability constraint. Do not apply `epsilon` independently per decision. Terminal residual valuation is not a fill.
4. Add the smallest observable state needed to distinguish working, pending, and terminal-control situations. Do not give P1 instantaneous cancels or hidden queue position.
5. Generate controlled action probes and rollout-derived histories on training data. Use action-conditioned transitions, not outcomes from unrelated historical participant decisions.
6. Freeze state/action support minima, fallback rules, and decision grid on development data; estimate sensitivity to bins and smoothing. Use forward-chained training price scores when stacking predictions. Model selection uses validation only.
7. Serialize P1 and its fallback rules. Compare it against B3 with identical operational mechanics and information.

### GA acceptance

Toy solutions agree with enumeration; ledgers reconcile; delayed reports do not lose or duplicate cost; the terminal valuation does not manufacture completion; support and fallback frequency are visible; and P1 uses the same controller as B3. R12 additionally requires an actually implemented P1, reproducible held-out evaluation against B3, and clear qualification of its partial-observation approximation. A solved toy or a feasibility appendix alone does not satisfy it.

**Stop expansion:** Transition sparsity, unmodeled pending-state behavior, or unstable rare-event estimates make P1 indefensible. Release the core with the negative finding and the validated toy solver rather than presenting an unreliable optimizer as advanced research.

## 10. WP6 — Robustness and final evaluation

**Dependencies:** G5. Add G3 only for native results, and GA only for P1 results.  
**Output:** Freeze manifest, sensitivity campaign, final paired tables, uncertainty analysis.  
**Gate:** G6.

### Development sensitivity

Start with the product's exact L0–L6 delay tuples at the primary horizon, plus horizon sensitivities at L1 and defensible queue/evidence assumptions. Keep the task benchmark fixed across latency scenarios. Primary latency robustness uses frozen L1 models and policies; scenario-specific retraining is a separately labeled adaptation study.

Do not vary every parameter combinatorially. Start with the base configuration, one-factor sensitivities, and a small number of joint stress cases. Use training/validation results to choose the final grid, not final-test outcomes.

For each scenario, publish completion outcomes, common-completion coverage, paired cost differences, and model ambiguity. Classify each sensitivity as fixed-policy transfer or a separately retrained matched policy experiment. Keep this distinction explicit; changing the label-generating model while retaining old models is not the same experiment as refitting them. An assumption change that flips the policy ranking is a central finding, not a failed presentation.

### Statistical protocol

The primary pair is always B3 versus B3_NO_QUEUE. For session `d`, let `J_d` contain technically evaluable tasks where **those two policies** complete on time. Adding B0, B2, P1, a native port, or another plot must not change this primary sample. Secondary comparisons use their own explicitly named pairwise common-completion sets. Define:

$$
\overline{D}_d=\frac{1}{|J_d|}\sum_{j\in J_d}
(IS_{baseline,j}-IS_{candidate,j}),
\qquad
\widehat{\Delta}=\frac{1}{D_{eligible}}\sum_d\overline{D}_d.
$$

Sessions with no common-completion tasks have an undefined paired cost effect, not a zero effect. Count and report them. The estimator is conditional on common completion; it is not a failure-adjusted portfolio return.

Also compute paired deadline-miss differences over **all technically evaluable tasks**, averaging within session and then equally across sessions, with paired dependence-aware uncertainty. Publish each arm's completion distribution, residual quantities, technical exclusions, and the common-completion fraction. Lower cost conditional on completion is not evidence of overall superiority when completion differs.

Choose session/block resampling and block-length sensitivity on development data. Bootstrap compared policies jointly so pairing is retained. Report block counts, effect sizes, interval uncertainty, and limitations. Do not treat book events or overlapping task probes as independent replications.

Zero observed misses do not establish zero true risk. A bootstrap of all-zero observations is degenerate. When the independence assumption is defensible, report a one-sided 95% upper bound `1 - 0.05^(1/D)` for the probability that a comparable **independent session** contains any miss, with `D` such sessions and zero observed session events. Do not relabel this as a per-task bound. If sessions are dependent, use a defensible block-level analysis or state that the sample cannot supply a reliable bound; do not substitute the task count in a binomial formula. Preserve this limitation in any policy-superiority claim.

Use B3 versus B3_NO_QUEUE under the primary configuration as the main confirmatory comparison. Predeclare a multiple-comparison adjustment if expanding the confirmatory family. Label remaining regime slices, model variations, and extensions exploratory unless assigned their own sealed evaluation protocol in advance.

### Freeze and final run

Freeze code, environment, data scope, task rules, models, policy/controller artifacts, primary comparison, metric, and scenario grid. Record first unblinding. Run final evaluation without changing parameters to improve its outcome.

A genuine implementation bug requires an incident note and invalidation of affected results. Methodological changes prompted by holdout outcomes need a new holdout for renewed confirmatory claims, or must be labeled exploratory.

### G6 acceptance

Every conclusion is traceable to a frozen run and states an effect, uncertainty, coverage, and execution-model limitation. Deadline failures remain visible. There is no selective reporting of successful tasks as the full population.

An optional extension built after the core holdout has been examined must use a new untouched window for confirmatory claims or label the reused evaluation exploratory. Native parity verification on the old sample is permitted; it does not create a fresh research holdout.

## 11. WP7 — Paper, repository, and technical presentation

**Dependencies:** G6.  
**Output:** Research report, reproducible Python repository, runtime/resource note, walkthrough; native benchmark only when WP3 is included.  
**Gate:** G7.

### Research report

Explain the question, related work, data, source semantics, clock approximation, client information, queue/fill rules, models, policies, evaluation, results, sensitivity, and limitations. Include enough detail to reproduce the central comparison; page count is secondary.

Separate measured historical observations, replay assumptions, fitted estimates, and conclusions. Cite the exact primary documents and literature used. Do not imply real fill validation, live profitability, or publication novelty that has not been established.

Include a coverage/source-quality table, an auditable causal order trace, probability/value diagnostics against base-rate predictors, the primary paired cost result beside completion-risk uncertainty, an assumption-sensitivity figure, and limitations. Record inconclusive findings and policy collapse as plainly as positive results.

### Public repository

Provide a credential-free synthetic quickstart and a separate licensed-data reproduction path. Include acquisition scope, schemas, manifests, test commands, and a task-trace example. No restricted raw data, secrets, private correspondence, or fabricated benchmark results may be published.

The runtime note documents feasibility on the tested workstation; the optional native benchmark distinguishes replay processing speed from measured exchange latency. A repository license covers only material the author has the right to license.

### External review and interview preparation

Seek criticism of the fill assumptions, counterfactual validity, timing, labels, partial observation, and claim wording. No professor endorsement or publication is guaranteed by the plan.

Prepare a 60-second explanation, a five-minute methods/result overview, and a trace of one order through the entire pipeline. Be able to derive the cost formula, explain a cancellation race, identify what the policy cannot observe, and describe a result that would contradict the hypothesis.

### G7 acceptance

A fresh environment runs the synthetic workflow. All core figures trace to artifacts. Claims match evidence. R01–R10 are satisfied for the complete Python research release. R11 is claimed only after the optional native gate passes; R12 only after P1 implementation and held-out evaluation pass their gate. Publish a negative feasibility appendix honestly without labeling an absent extension complete.

## 12. Validation matrix

These tests are requirements to implement. This document does not claim that the project already passes them.

| Test ID | Case | Required outcome | Primary gate |
|---|---|---|---|
| T01 | Add/cancel/modify/reset | Correct visible state and priority | G1 |
| T02 | Equal timestamps, different source order | Source order retained | G1 |
| T03 | Non-mutating final boundary record | Batch commits correctly | G1 |
| T04 | Snapshot initialization | State initialized without false order-flow features | G1 |
| T05 | Missing records or invalid state | Diagnostic and explicit recovery/coverage treatment | G1 |
| T06 | Trade/Fill plus associated reduction | Book and queue quantities changed exactly once | G2 |
| T07 | Five ahead, execution of three | Two ahead; virtual order unfilled | G2 |
| T08 | Exact exhaustion of queue ahead | No virtual fill without further eligible volume | G2 |
| T09 | Cancellation ahead or behind | Position changes appropriately; no fabricated execution | G2 |
| T10 | Quote touches limit | No fill solely from a touch | G2 |
| T11 | Entry delay and same-time event | No pre-arrival fill or same-event clairvoyance | G2 |
| T12 | Marketable-on-arrival limit | Correct execution and liquidity classification | G2 |
| T13 | Delayed fill report | Policy remains unaware until delivery | G2 |
| T14 | Fill during cancellation | No duplicate residual order | G2 |
| T15 | Duplicate/late reports | Idempotence; no resurrected terminal order | G2 |
| T16 | Every pending state at terminal cutoff | Guard/controller handles modeled path or records miss | G2 |
| T17 | Halt or missing executable depth | No synthetic successful fill | G2 |
| T18 | Fill on time, report late | Completion judged by execution time | G2 |
| T19 | Buy/sell shortfall, fees, half ticks | Correct sign and units | G2 |
| T20 | Python/C++ replay | Exact discrete parity on deterministic fixtures | G3 |
| T21 | Binding lifetimes/overflow | No sanitizer failures | G3 |
| T22 | Future input perturbation | Earlier features/actions unchanged | G4 |
| T23 | Outcome window crosses split | Purged or otherwise handled by frozen rule | G4 |
| T24 | Cancelled historical order | Not mislabeled as would-never-fill-if-held | G4 |
| T25 | Oracle queue feature attempt | Feature allowlist rejects hidden state | G4 |
| T26 | Calibration/threshold selection | Fit only on permitted development data | G5 |
| T27 | Toy P1 | Matches enumeration | GA |
| T28 | Fixed reference and delayed report cost | Exact ledger reconciliation | G4/GA |
| T29 | Terminal residual mark | Does not create a fill or completion | G4/GA |
| T30 | P1 constraint and K = 1 reduction | Lower cost cannot violate the task-level risk constraint; with one decision P1 equals B3's risk-allowance choice | GA |
| T31 | Unsupported P1 state | Recorded fallback, not invented zero cost | GA |
| T32 | No common-completion tasks | Undefined cost effect and explicit count, not zero | G6 |
| T33 | Reproduction and clean environment | Synthetic CLI produces traceable outputs | G7 |
| T34 | Size/price modification and reduction | Correct priority loss/retention and queue relation | G1/G2 |
| T35 | Duplicate records or overlapping files | Explicit diagnostic; no duplicate book mutation or fill | G1 |
| T36 | Refresh or hidden/implied ambiguity | Declared allocation treatment; no fabricated aggressive volume | G1/G2 |
| T37 | Reset/snapshot during hypothetical task | Recovery or technical failure; no invented queue continuity | G2 |
| T38 | No market event at checkpoint/cutoff | Exact timers still fire; no periodic reconsideration | G2 |
| T39 | Guard from every reachable pending state | Declared path budget holds, or actual failure is recorded | G2 |
| T40 | Aggressive rejection/no liquidity | No second attempt or synthetic completion | G2 |
| T41 | Cloned HOLD/SWITCH checkpoint | Same source cursor, observations, reports, commands, timers, and RNG state | G4 |
| T42 | Failed or late-completing probe arm | Miss retained; value uses only deadline executions/residual mark | G4 |
| T43 | Unchanged price label (primary `tau` and secondary 100 ms) | Row retained in the explicit unchanged class; primary `tau` equals the frozen L1 value in every scenario | G4 |
| T44 | Stacked price-score provenance | No in-sample or future-trained score enters B3 training | G4/G5 |
| T45 | No-queue feature allowlist | Own-order queue and derived encodings removed; shared price/depth/mechanics identical | G5 |
| T46 | B2 sign, threshold equality, single decision | Correct buy/sell action; equality HOLD; no second checkpoint action | G5 |
| T47 | B3 risk-allowance rule, exact tie, missing support | Cheaper action chosen when its miss gap is within `epsilon`; safer action chosen when the gap exceeds `epsilon`; cost ties SWITCH; numerical/model-support fallback SWITCH; `epsilon = 0` and `epsilon = 1` boundary cases behave as strict risk-first and pure-cost rules | G5 |
| T48 | Pending/unsupported client checkpoint | Safe B1 continuation; unreported fills do not leak | G5 |
| T49 | Optional policy added to report | Primary B3/NO_QUEUE task set and estimate unchanged | G6 |
| T50 | Zero observed misses | No zero-risk claim or misleading all-zero bootstrap interval | G6 |
| T51 | UTC midnight and daylight-saving boundary | Correct exchange session and local task-window assignment | G0/G4 |
| T52 | Reordered/missing model feature columns | Hard schema error, not silently different predictions | G5 |
| T53 | Early aggressive rejection then terminal timer | Controller remains absorbing; sole attempt cannot reset | G2 |
| T54 | Client queue proxy versus simulator truth | Uses only delivered cohort history; no exact insertion/rank leak or claimed guaranteed band | G4 |
| T55 | Virtual best remains after historical level disappears | Supported opposing trade-through fills at own limit within its unique execution budget; quote move alone does not | G2 |
| T56 | Two historical commits before delayed observation delivery | First delivery contains only its original committed state; no mutable-book leakage | G2 |
| T57 | B3 labels versus P1 forced continuation | Same deadline value, failure indicator, fees, and unreported-fill reconciliation | G4/GA |
| T58 | Fill-mechanism classification | Queue-depletion, trade-through, aggressive, and ambiguous fixtures receive the correct `fill_mechanism` | G2 |
| T59 | G2-S selection rule | Horizon chosen from support counts only; altering cost or markout columns cannot change the selection | G2 |
| T60 | Shared risk-allowance function | B2 threshold selection, B3, and B3_NO_QUEUE invoke the same function and frozen `epsilon`; no variant-specific override | G5 |

### Requirement-to-gate traceability

| Product requirement | Required gates/evidence |
|---|---|
| R01 Data suitability | G0 source, scope, budget, and license decision; G2-S horizon/support gate |
| R02 Historical reconstruction | G1 fixtures and real-data audit |
| R03 Causal information | G2 timing and G4 leakage tests |
| R04 Exposure/controller safety | G2 races, terminal guard, and pending-state tests |
| R05 Execution/cost accounting | G2 ledgers and G4 paired branch targets |
| R06 Policy-independent tasks | G2 task manifest, G4 coverage, G6 fixed comparison population |
| R07 Interpretable models | G4 label/score provenance and G5 validation/support report |
| R08 Matched evaluation | G5 ablation and G6 paired uncertainty/denominators |
| R09 Assumption sensitivity | G0 limitations and G6 declared sensitivity campaign |
| R10 Reproducible release | G7 fresh-environment demonstration and artifact traceability |
| R11 Optional native extension | G3 parity, sanitizers, and honest benchmark |
| R12 Optional P1 extension | GA implemented solver/support tests plus G6 held-out comparison |

## 13. Dependency graph and scope control

```text
WP0 / G0
   -> WP1 / G1
   -> WP2 / G2: first end-to-end baseline
   -> G2-S: horizon/support gate; user-approved full development acquisition
   -> WP4 / G4: research datasets
   -> WP5 / G5: B2/B3 and matched ablations
   -> WP6 / G6: robustness and final evaluation
   -> WP7 / G7: complete Python research release (R01-R10)

After stable G5, optional independent branches:
   WP3 / G3: C++ systems extension -> parity and frozen metric rerun (R11)
   GA: P1 on Python or validated native -> held-out evaluation (R12)
```

WP3 and GA do not block the Python release. A native P1 needs G3; a Python P1 does not. Preserve WP identifiers when referring to artifacts and gates; the numeric identifier is not the work order. Draft methods and prepare synthetic release material as components freeze. Do not move final holdout evaluation earlier to obtain a motivating chart.

Reduce optional nonlinear models, stochastic latency, second-instrument work, UI polish, and premature optimization before reducing correctness or statistical controls. Multi-contract execution and passive reposting require a new scope version.

## 14. First ten development issues

| Order | Issue | Concrete evidence |
|---:|---|---|
| 1 | Inspect a small real sample and metadata | Acquisition/coverage/cost manifest |
| 2 | Write source semantics and limitations | Sourced action, priority, clock, and matching note |
| 3 | Implement canonical iterator | Ordering, flags, and lineage preserved |
| 4 | Implement event-batch assembler | Boundary and truncation fixtures |
| 5 | Implement Python book | Hand-derived committed snapshots |
| 6 | Reconcile execution attribution and reductions | T06–T08 fixtures |
| 7 | Audit a real session | Explained anomalies and fixed coverage rules |
| 8 | Add delayed observations/reports and client OMS | Causal and race fixtures |
| 9 | Implement one-contract B0/B1 | Auditable controller and task ledgers |
| 10 | Run the G2-S support diagnostic and produce the first paired table | Support report by session, horizon, and fill mechanism; each aggregate traces to task outcomes |

Do not begin with a dashboard, neural network, dynamic program, or custom allocator.

## 15. Proposed CLI and agent handoff

The following commands are interfaces to implement, not commands already available:

```text
qexec data inspect --config ...
qexec replay validate --config ...
qexec tasks build --config ...
qexec experiment run --config ...
qexec trace task --experiment ... --task-id ...
qexec dataset build --config ...
qexec model train --config ...
qexec policy solve --config ...
qexec analysis report --experiment ...
qexec benchmark replay --config ...
```

For every implementation issue, record purpose, dependencies, affected contracts, required tests, and non-goals. An agent handoff must identify the latest passed gate, current code commit, exact task, and restrictions. The agent reports changed files, tests actually executed, failures, and unresolved assumptions; it may not self-declare later gates passed.

No placeholder result, fictional paper contribution, or unrun test may appear in the final report or README as completed work.

## 16. Sources for implementation decisions

[^glbx]: Databento, *CME Globex MDP 3.0*. https://databento.com/docs/venues-and-datasets/glbx-mdp3
[^tracking]: Databento, *State management of resting orders*. https://databento.com/docs/examples/order-book/order-tracking
[^book]: Databento, *Limit order book construction*. https://databento.com/docs/examples/order-book/limit-order-book
[^latency]: HftBacktest, *Latency Models*. https://hftbacktest.readthedocs.io/en/latest/latency_models.html
[^metadata]: Databento, *Historical API*, acquisition estimates and metered requests. https://databento.com/docs/api-reference-historical?historical=http
