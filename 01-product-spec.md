# QExec — Product Specification

**Version:** 2.1  
**Prepared:** September 27, 2026 · **Revised:** October 1, 2026  
**Status:** Historical research plan. See [current implementation scope](README.md#current-release) for the completed synthetic Python core and remaining work.  
**Companion documents:** [Architecture Plan](02-architecture-plan.md) · [Implementation Plan](03-implementation-plan.md)

> This plan describes the full proposed market study. The later authorized implementation delivers a synthetic Python core; it does not complete the licensed-data pilot, real-market evaluation, or optional extensions. Current release evidence is recorded in [the remediation review](docs/remediation.md).

## 1. Purpose and intended contribution

Build a research-grade Python system, with an optional validated C++ extension, to answer:

> **For an existing one-contract passive order, do client-observable estimates of its queue position improve a hold-or-cross decision beyond aggregate book information and a shared price forecast, and how sensitive is that result to latency and fill assumptions?**

The deliverable is a defensible empirical study supported by inspectable software—not a live trading platform or a claim of profitable alpha. The intended audience is a quantitative researcher, quantitative research engineer, or trading-systems engineer reviewing the author's mathematical reasoning, implementation quality, and experimental judgment.

The working research title is **Queue-Aware Execution Under Latency and Adverse Selection**. The broad subject is already established: prior work studies imbalance-aware order placement and latency.[^placement] Any claim of originality must concern a specific result or method and survive a focused literature review.

A successful project may find that a simple policy is difficult to beat, that better price prediction does not improve execution, or that policy rankings are fragile. Favorable trading results are not an acceptance criterion.

### 1.1 Assessment and positioning

**Recommendation: pursue the idea, subject to the data gate.** It can be an excellent undergraduate project because one narrow experiment connects probability, statistical inference, market microstructure, causal software design, and systems engineering. That assessment is a judgment about the opportunity, not a guarantee of admission, hiring, profitability, or publication.

Its strongest fit is execution research, market microstructure, and quantitative development. For broader quantitative research recruiting, emphasize experimental design, uncertainty, and clear mathematical reasoning. The impressive deliverable is a reproducible finding whose assumptions can be challenged, supported by an inspectable order trace. A large codebase, fast benchmark, or elaborate optimizer by itself does not establish research quality.

Queue imbalance prediction and latency-aware order placement are established topics.[^imbalance][^placement] The proposed contribution is a carefully controlled study of the incremental value of **own-order queue estimates**, with explicit delayed client information and sensitivity to counterfactual fill rules. This is a candidate contribution, not a claim of a new research field.

### 1.2 Review findings incorporated in this revision

| Area | Assessment of the previous plan | Resolution |
|---|---|---|
| Research idea | Strong question; contribution broader than the experiment could isolate | Narrow the primary claim to one existing passive order and one checkpoint |
| Product scope | Too much depended on C++ and P1 | Make the Python research release complete; separate systems and sequential-control extensions |
| Policies | B2/B3 described intent without executable rules | Specify common prefix, checkpoint, action targets, ties, and fallbacks |
| Queue ablation | Could conflate aggregate imbalance with own-order priority | Freeze feature groups and share the price model |
| Training | Submission fill labels cannot price aged-order decisions | Branch both actions from the same checkpoint state and retain failures |
| Evaluation | Adding policies could change the primary common-completion sample | Freeze the primary pair and report completion risk on the full evaluable population |
| Architecture | Strong state separation; essential queue and terminal cases deferred | Specify trade-through, bounded controller timing, and delivered-state snapshots |
| Delivery | Effort and sequencing encouraged premature porting | Gate research first, re-estimate after the first real-data experiment |

Revision 2.1 (October 1, 2026) made these further changes:

| Area | Problem | Resolution |
|---|---|---|
| B3 objective | Strict risk-first ordering let negligible, noise-level differences in estimated miss probability override every cost difference | Minimize estimated cost within a declared absolute risk allowance `epsilon` (section 6.1) |
| B2 threshold selection | Same strict ordering on empirical validation miss rates | Same allowance rule (section 6.3) |
| B2 forecast horizon | 100 ms price target did not match the roughly 500 ms waiting decision | Primary target spans checkpoint to L1 terminal cutoff; 100 ms is secondary |
| Horizon and support | 1 s horizon could lack post-checkpoint passive-fill support | Predeclared horizon ladder and support gate, split by fill mechanism, before full-study purchase (section 4.4) |
| P1 risk | A per-decision allowance does not preserve a task-level risk budget | P1 requires a constrained formulation (architecture section 11.3) |
| Effort | WP2 estimate optimistic | Re-estimated in implementation plan section 1 |
| Documentation | Full rules repeated in three documents | One authoritative definition per contract (section 2.1) |

No document can establish data suitability or statistical power without the pilot. Those uncertainties have explicit owners, stop conditions, and evidence requirements below.

## 2. Document authority and terminology

This document owns scope, research tasks, policy identifiers, metrics, and release criteria. The architecture owns software and event semantics. The implementation plan owns work order, tests, and delivery gates. A design change affecting more than one document requires updating all affected documents together.

**MUST** identifies a release requirement. **SHOULD** identifies the default unless a written reason justifies a change. **MAY** identifies optional work. Numerical pilot settings are proposed defaults, not established market facts.

- **Observed:** Present in source data or reconstructable under documented source semantics.
- **Simulated:** Produced by a named hypothetical execution rule.
- **Estimated:** Learned from a declared dataset and modeling procedure.
- **Validated:** Supported by a specified test; always identify what the test establishes.

Calibration against simulated labels validates a model against those labels. It does not independently validate real-world fills.

### 2.1 Authoritative definitions

Each contract below is defined in exactly one place. Other sections and documents reference it and add only implementation responsibilities or tests; they must not restate the rule. If a restatement and its source disagree, the source governs and the restatement is a defect.

| Contract | Authoritative location |
|---|---|
| Scope, task fields, admissible actions, task outcomes | Product sections 3, 5 |
| Policy decision rules (B0–B3, B3_NO_QUEUE), risk allowance, tie and fallback rules | Product section 6.1 |
| Feature groups and ablation boundary | Product section 6.2 |
| Pilot configuration, horizon ladder, latency tuples, menus | Product section 6.3 |
| Horizon/support gate | Product section 4.4 |
| Shortfall, markout, completion metrics | Product section 7 |
| Deadline-horizon target `C_T` and miss indicator | Product section 8 |
| Event semantics, batching, priority | Architecture sections 4–5 |
| Clock, latency equations, scheduler ordering | Architecture section 6 |
| Order/report state, guard `G`, terminal controller, aggressive primitive | Architecture section 7 |
| Queue allocation and execution evidence | Architecture section 8 |
| P1 state, accounting, and constrained objective | Architecture section 11 |
| Work order, gates, effort, test matrix | Implementation plan |

## 3. Release scope

| Dimension | Core research release | Advanced release or later extension |
|---|---|---|
| Market | One venue, one product, explicit contract identifiers | Another period or instrument for transfer analysis |
| Task quantity | Exactly one contract | Small multi-contract tasks after separate validation |
| Exposure | At most one potentially live child order per task | Multiple working children only under a new specification |
| Passive behavior | One initial passive submission; one hold-or-cross checkpoint | Repeated decisions in P1; reposting requires a new scope |
| Policies | B0, B1, B2, B3 | P1 finite-horizon model-based policy |
| Models | Interpretable price, action-outcome, and fill diagnostics | One nonlinear comparator only for a defined question |
| Systems | Validated Python reference and reproducible CLI | Validated C++ port and independent-session parallelism |
| Evidence | Out-of-sample historical replay and assumption sensitivity | External review and transfer tests |

The one-contract restriction isolates the decision to wait or cross without simultaneously introducing inventory scheduling and repeated liquidity consumption. The historical-book engine still handles partial reductions and executions of historical orders. Partial fills of a multi-contract *strategy* order belong to the extension.

The **core research release is the recommended flagship**. The C++ systems extension demonstrates implementation and performance skills. P1 is a separate research extension, justified only by transition support and a useful sequential question. Neither is required to make the core impressive. A documented decision to stop P1 is a valid core appendix, not a completed P1 release.

### Explicit exclusions

The first release MUST NOT include live brokerage execution, multi-venue routing, portfolio optimization, deep reinforcement learning, endogenous multi-agent market-impact simulation, a commercial user interface, or large-order impact claims. No real-money deployment is needed.

## 4. Data selection and feasibility

### 4.1 Preferred candidate

Investigate CME E-mini S&P 500 futures through Databento's `GLBX.MDP3` dataset. This is a candidate subject to the data gate, not a commitment to use an unsuitable feed.

Databento documents CME order-level events and normalization, including snapshots and the distinction between direct and implied liquidity.[^glbx] The implementation MUST establish that the selected contract, period, and matching mechanisms support the intended queue experiment. A schema named MBO is not by itself proof that the underlying source provides true individual-order information.[^fields]

Do not assert FIFO execution solely because the feed exposes order sequence. Confirm product-specific allocation rules and the treatment of implied, hidden, priority-changing, and non-continuous-trading events before claiming venue-faithful queue behavior.

### 4.2 Acquisition sequence

1. Obtain three pilot sessions, with initialization history or a trusted snapshot. Choose them by calendar criteria fixed before inspection so that conditions contrast (for example, one scheduled-macro-announcement day, one ordinary mid-week day, one day adjacent to a roll); never select on observed fills or costs.
2. Validate event semantics, priority, timestamp quality, session boundaries, and execution attribution.
3. Estimate full-study data cost, storage, runtime, and licensing constraints.
4. Pass the horizon/support gate (section 4.4) on the pilot sessions, then expand the development sample.
5. Acquire or designate the untouched test interval only after the evaluation protocol is defined.

Use explicit contracts and time-effective instrument metadata. Do not reconstruct an executable book from back-adjusted continuous prices. Contract selection for a historical task MUST use a fixed roll rule or information available beforehand—not the same day's final volume or other future information.

The acquisition step MUST request a cost estimate and user approval before incurring unapproved charges. No fixed data budget or free-credit assumption is established by this specification.

### 4.3 Feasibility gate G0

The pilot passes only if the team can explain source actions, priority changes, event boundaries, initialization, timestamp meanings, and material unobserved mechanisms. It must produce an anomaly report and a permitted reproducibility plan.

An unresolved interpretation that changes fills, priority, or causal timing blocks G0 unless the study explicitly admits it as a named alternative model and narrows its claims accordingly. Merely writing down a future resolution step does not pass the gate. Genuine CME order-level data must come from the MBOFD era; use a modern interval with explicit contract identity and verify its coverage.[^glbx]

Before acquisition, specify dataset, schema, **explicit symbols**, dates, and initialization. Estimate billable size, records, and cost through vendor metadata; cache the authorized download and prohibit accidental whole-dataset or repeated paid retrieval.[^acquisition] The contract, dates, budget, license, and hardware/storage cap remain decisions for G0, not permissions inferred from this plan.

If the data is inadequate, change dataset or venue before proceeding to the research engine and learned policies. A market-by-price fallback requires a separately narrowed claim; it is not an equivalent substitute for individual-order reconstruction.

### 4.4 Horizon and support gate G2-S

Book depth alone does not determine whether a joined order fills; turnover, cancellations, queue position, and conditions matter. Support is therefore measured, not assumed, and is measured where the primary decision occurs.

**Decisive diagnostic.** Run B1 replay probes (L1, all three pilot sessions) at each horizon in the predeclared ladder `H in {1 s, 5 s, 30 s}`. Among tasks that are **decision-eligible at the checkpoint** (client-reported working, unfilled, no pending command), count HOLD-continuation passive fills before the terminal cutoff and classify each by mechanism:

- `QUEUE_DEPLETION`: filled by same-price execution evidence after estimated quantity ahead was exhausted;
- `TRADE_THROUGH`: filled by opposing execution at a price through the limit (architecture section 8.2);
- `AMBIGUOUS`: attribution unresolved under the frozen ambiguity rule.

Report per session: arrivals, technically evaluable tasks, decision-eligible checkpoints, fills by mechanism, and fills before the checkpoint. A preliminary descriptive estimate from real historical orders that joined at the touch MAY be made at G0; it does not replace the replay diagnostic.

**Selection rule, frozen at G0 before any probe is run.** Numeric minima for (a) decision-eligible checkpoints per session and (b) `QUEUE_DEPLETION` fills per session are written into the research contract. The primary horizon is the **shortest** ladder horizon meeting both minima in every pilot session. Queue-depletion fills are required specifically because queue position chiefly matters for that mechanism; a horizon whose fills are almost all trade-throughs weakens H1 even with a high overall fill rate. Selection uses only these support counts—never cost, markout, or policy comparisons. If no ladder horizon passes, redirect product/venue or narrow the claim before buying the full study.

**Consequences of a longer horizon.** Arrival spacing is `max(10 s, 2H)` so that same-side task windows never overlap; this reduces tasks per session and must be reflected in the precision estimate (section 10). Guard validity `0 < H/2 < H - G` and the price-target horizon (section 6.3) are recomputed from the chosen `H`.

Full development-data acquisition requires passing G2-S. One session can expose an obvious failure; passing requires all three.

## 5. Canonical execution task

Each task is created independently of the evaluated policy:

```text
Task
  task_id
  instrument_id and explicit contract
  side: BUY | SELL
  quantity: 1
  arrival_time_ns
  deadline_ns
  arrival_reference_midprice
  eligibility_rule_version
  task_manifest_id
```

The same task manifest is used by all policies and latency scenarios in a comparison. Task arrivals follow a predeclared time grid or seeded rule, not a search for favorable entries. Treat tasks as independent counterfactual probes, not orders in one simultaneously deployed portfolio. Prefer non-overlapping task windows; paired buy/sell probes at an arrival remain separate worlds.

### 5.1 Arrival benchmark

The primary benchmark `m0` is the midpoint of the **committed historical direct book** at task arrival in the declared exchange-proxy timeline. It is evaluator metadata, not information automatically available to a delayed policy. This fixed benchmark permits comparisons across latency scenarios.

Eligibility requires a valid benchmark and adequate pre-task initialization. Pre-task warmup must provide a valid initial client-observed reference quote in every planned latency scenario; this check uses only information available by task arrival. Future missing data may make a task technically unevaluable, but that status must be applied by a policy-independent rule, counted, and disclosed. Future market adversity, such as a halt, is an outcome—not a retrospective reason to erase the task.

### 5.2 Admissible actions

- `WAIT`: Take no new exposure while no command is pending.
- `JOIN_BEST`: Submit one limit order at the same-side best price currently observable to the client.
- `MAINTAIN`: Leave the existing order working.
- `SWITCH_TO_TAKER`: Irrevocably enter the shared cancel-and-complete controller.

The controller is the only route to aggressive execution. `HOLD` below means `MAINTAIN` until the common terminal cutoff, or `WAIT` if an initial submission was rejected. It is a continuation rule, not a new exchange order type.

A live passive order cannot be silently repriced as the book moves. The first release permits no passive reposting. An order intended to be passive can arrive marketable; classify and process it using the declared limit-order semantics.

B0's immediate execution means **send immediately, then execute after the modeled delays**. No policy may buy at an old observed ask without accounting for order arrival.

### 5.3 Deadline and incomplete tasks

All policies use the same terminal controller. It begins early enough to resolve outstanding submissions, cancel working exposure, receive the necessary reports, and send the residual aggressive order under the declared bounded-latency assumptions.

Core control uses exact scheduled timers, immediate report handling, and **one** residual aggressive attempt; there are no aggressive retries. The terminal cutoff is `deadline - G`, where the conservative guard `G` and its state-dependent paths are defined in architecture section 7.3 (`G = 1.400001 ms` under L1). A configuration is valid only if `0 < H/2 < H - G`; the guard protects timing, not liquidity availability.

The core aggressive primitive is the synthetic displayed-liquidity abstraction `SYNTHETIC_DIRECT_TOP` and the passive-arrival rules defined in architecture section 7.3. It is a research abstraction, not a claim to reproduce CME order protection, implied matching, or a broker order type.

Completion is defined by actual simulated execution time, not report receipt time. A late report of an on-time execution is not a missed execution deadline.

The controller MUST NOT create a fill during a halt or when executable liquidity is unavailable. Record actual residual quantity and one of:

- `COMPLETED_ON_TIME`
- `DEADLINE_MISS`
- `TECHNICALLY_UNEVALUABLE`

A deadline miss retains its trace and any subsequent diagnostic completion time. Technical failures and economic deadline misses must never be conflated.

## 6. Research hypotheses and comparisons

**H1 — Own-order queue information:** Adding client-observable own-order queue estimates improves the checkpoint decision relative to an otherwise matched model using aggregate book information and the same price forecast.

**H2 — Prediction versus decisions:** Better price-forecast metrics do not necessarily imply lower execution cost.

**H3 — Selection into fills:** Post-fill outcomes differ across observable states; passive fills cannot be treated as a random sample of opportunities.

**H4 — Robustness:** Latency and defensible execution-model changes may alter policy rankings.

These are questions to test, not predetermined findings.

| ID | Policy | Role |
|---|---|---|
| B0 | Immediate aggressive submission | Simple completion benchmark |
| B1 | Join at arrival; hold to the common terminal cutoff | Transparent waiting benchmark |
| B2 | Same initial join; one price-rule decision at the checkpoint | Price-information comparator |
| B3 | Same initial join; one action-outcome decision at the checkpoint | Execution-value comparator |
| P1 | Finite-horizon model-based policy | Tests sequential optimization beyond B3 |

B2 may use mechanical fields required to operate, such as time remaining and outstanding-order status. “Price-only” means no learned fill/queue-cost model, not ignorance of its own orders.

For H1, use `B3_NO_QUEUE` for the matched B3 ablation; its precise meaning is **no own-order queue estimate**. An optional P1 ablation is `P1_NO_QUEUE`. This is an incremental predictive/decision-value experiment within replay, not identification of a causal market effect. B2 versus B3 changes several ingredients and does not isolate H1.

Time slicing is out of scope for a one-contract task.

### 6.1 Executable core policy rules

All policies receive the same initial task and information. B0 immediately enters the completion controller. B1/B2/B3 and the matched ablation submit the same one-contract same-side limit at arrival; their trajectories are identical until the checkpoint `t0 + H/2`. Each runs in its own counterfactual world.

At the checkpoint:

1. A discretionary choice requires a client-reported **working, unfilled order with no pending command**. A reported completed task requires no action. Rejected, unresolved, or unsupported client states follow B1's safe continuation; record the reason. An unreported exchange fill must not affect this decision.
2. B1 keeps its original continuation. B2 computes `u = P(up) - P(down)` from the shared price model (target defined in section 6.3) and switches if `s*u > theta`, otherwise holds; equality holds. Positive `s*u` predicts a move that makes waiting more expensive. The cutoff controller always overrides discretion.
3. B3 predicts for each action `a in {HOLD, SWITCH}` the pair `(p_a, v_a)`: estimated deadline-miss probability `E[d_a | x]` and expected deadline-horizon cost `E[C_T,a | x]`, both defined in section 8. It then applies the **risk-allowance rule**:

   $$
   A_\varepsilon(x)=\{a:\hat p_a(x)\le \min_b \hat p_b(x)+\varepsilon\},\qquad
   a^*=\arg\min_{a\in A_\varepsilon(x)}\hat v_a(x).
   $$

   Choose the lowest estimated cost among actions whose estimated miss probability is within the absolute allowance `epsilon` of the best. Exact cost ties within `A_epsilon` choose `SWITCH`. Nonfinite predictions or inadequate support choose `SWITCH`; report fallback frequency. `B3_NO_QUEUE` uses precisely the same rule, `epsilon`, and tie/fallback conventions.
4. There is no second discretionary core decision. At the common cutoff, every unresolved task enters the shared terminal controller. Controller entry is idempotent: a prior aggressive rejection consumes the sole attempt, and the cutoff cannot restart it. Initial passive rejection consumes the passive attempt; it does not authorize a repost.

Core B3 is a one-step decision under a fixed continuation. P1 investigates the additional value of repeated decisions; a per-decision allowance does not carry over to P1 (architecture section 11.3).

**Status of `epsilon`.** The allowance is an economic design choice—how much additional estimated completion risk the study will accept for lower expected cost—not a numerical tolerance. It is **absolute** in probability units because miss probabilities are expected to be near zero, where a relative allowance becomes unstable and dominated by estimation noise. Its primary value requires a written justification, is frozen before validation-based selection (section 6.3), and is accompanied by a frozen sensitivity set including the boundary cases `epsilon = 0` (strict risk-first ordering) and `epsilon = 1` (pure cost minimization). The rule still relies on estimated risks; it does not bound realized completion risk, which is reported separately (section 7.3). A result in which the rule chooses one action everywhere is an admissible negative result.

### 6.2 Feature groups and matched comparisons

| Group | Examples | B3 | B3_NO_QUEUE |
|---|---|---|---|
| Shared market information | Spread, top depth, imbalance, recent delivered flow, volatility, quote age | Included | Identical |
| Shared mechanics | Side, original limit, order age, time remaining, reported status | Included | Identical |
| Shared price signal | Frozen price-model probabilities | Included | Same model and prediction |
| Own-order queue estimates | Estimated quantity ahead, insertion uncertainty band, estimated depletion ahead | Included | Removed, including derived encodings |
| Hidden/oracle information | Actual simulated rank, unreported execution, future flow | Prohibited | Prohibited |

The price model may use shared aggregate book features but never the own-order group. Keeping aggregate depth/imbalance is intentional: the question concerns additional own-order information. Use the same training rows, action labels, estimator families, model selection allowance, calendar, and controller. Fit each ablation separately with the same finite tuning menu. Publish held-out action disagreement and coverage as well as cost.

H2 is secondary: if a nonlinear price model is added, feed both price models into the same B2 rule with identical threshold-selection allowance and tasks. Better prediction alone does not establish better execution; an actual differing outcome must be measured before claiming this finding.

### 6.3 Proposed pilot configuration and research freeze

These values make the plan executable; they are modeling assumptions, not measured exchange or network performance. G0/G2 may revise them using cost, support, and coverage diagnostics. Freeze the primary choice before validation-based policy selection, and record every revision.

| Setting | Starting proposal | Resolution rule |
|---|---|---|
| Market scope | ES, one explicit liquid expiry per session; fixed roll rule between sessions, no back-adjusted book | Verify period-specific metadata and source semantics at G0; no task spans a contract change |
| Session scope | Full exchange trading days; tasks only 09:30–16:00 America/New_York on normal full days | Use exchange calendar and timezone-aware DST conversion; this is the study window, not CME's full session |
| Quantity / horizon | One contract; `H` from the ladder `{1 s, 5 s, 30 s}`, provisional primary `H = 1 s` | Primary `H` is the shortest ladder horizon passing G2-S (section 4.4); never chosen for favorable policy performance |
| Task arrivals | One arrival every `max(10 s, 2H)` with a fixed seeded daily phase, both sides in separate worlds | Same task IDs across policies; no overlapping horizons within one side |
| Decision times | Arrival, checkpoint at `H/2`, terminal cutoff | Reports update state when delivered; no periodic core reconsideration |
| Delays: added delivery / computation / entry / response | `0 / 50 / 250 / 250` microseconds | Assumed base; historical capture transit already present |
| Latency sensitivity | Named exact tuples in the table below | One-factor changes and one joint stress case; no Cartesian search |
| Horizon sensitivity | The other ladder horizons that pass guard validity, with proportional checkpoint | Separate task-horizon manifests; a horizon failing G2-S support is reported as unsupported, not evaluated |
| Price target (primary) | Market-direction change `m(t* + tau) - m(t*)`, classes down/unchanged/up, with `tau = H/2 - G_L1` (the checkpoint-to-L1-cutoff waiting interval; `498.599999 ms` at `H = 1 s`) | `tau` is fixed at its L1 value for all latency scenarios so that frozen-policy robustness does not silently change the forecasting task; no side adjustment in the label; B2 applies `s` exactly once |
| Price target (secondary) | Same classes at `tau = 100 ms` | Separate labeled B2 variant for H2; never the primary comparator |
| Risk allowance `epsilon` | Absolute; provisional primary `0.001`; frozen sensitivity set `{0, 0.0001, 0.001, 0.01, 1}` | Primary value justified in writing and frozen at G4 from development miss-rate support, before validation selection; identical for B3, B3_NO_QUEUE, and B2 threshold selection |
| Price-rule threshold menu | `theta in {0, 0.1, 0.2}` | On validation, keep thetas whose session-mean miss rate is within `epsilon` of the menu minimum; among them minimize session-mean `C_T` over all technically evaluable tasks; exact ties prefer smaller theta; log all trials |
| Markout horizons | 10 ms, 100 ms, 1 s after fill | Separate diagnostic; source coverage and freshness disclosed |
| Initial acquisition | Three contrasting pilot sessions (section 4.2) | Estimate full-study cost and pass G2-S before expansion |
| Study-size planning seed | 60 additional sessions: 30 train, 10 validation, 20 test, chronologically | A budgeting starting point, not a power guarantee; revise using development session variability and resources before unblinding |

Latency tuples below are in microseconds, ordered as `(added delivery, computation, entry, response)`. The measured historical capture transit remains present, including L0.

| ID | Tuple | Purpose |
|---|---|---|
| L0 | `(0, 0, 0, 0)` | Idealized additional-delay diagnostic, not zero historical feed latency |
| L1 | `(0, 50, 250, 250)` | Primary assumed scenario |
| L2 | `(1000, 50, 250, 250)` | Observation-delay sensitivity |
| L3 | `(0, 500, 250, 250)` | Computation-delay sensitivity |
| L4 | `(0, 50, 2500, 250)` | Entry-delay sensitivity |
| L5 | `(0, 50, 250, 2500)` | Response-delay sensitivity |
| L6 | `(1000, 500, 2500, 2500)` | Joint stress scenario |

Run these at the primary horizon. Run horizon sensitivity at L1 only unless a further joint case is preregistered. Report a policy frozen at L1 under other scenarios to measure robustness; retraining per scenario is a separate labeled adaptation study with the same development-only tuning allowance. Retraining and frozen-policy results must not be pooled.

Use the smallest stable model set: a regularized multinomial logistic price model, interpretable action-risk/value models, and a simple fill diagnostic. One candidate nonlinear model is optional. All preprocessing, regularization grids, calibration, support rules, and fallback choices must be a short finite manifest before tuning. Failure to attain useful precision yields an exploratory or inconclusive study, not a relaxed significance standard.

## 7. Metrics and accounting

### 7.1 Implementation shortfall

For a completed task define:

$$
IS_{ticks}=\frac{s\sum_i q_i(p_i-m_0)}{Q\delta}
+\frac{\sum_i f_i}{Q\delta M}.
$$

Here `s = +1` for a buy and `s = -1` for a sell; `Q = 1`; `q_i` is executed quantity; `p_i` and `m0` are in quoted price units; `delta` is tick size; `M` is currency per quoted price unit per contract; and `f_i` is a signed monetary fee, positive for a charge. A rebate would be negative only where justified by the actual fee model.

**Positive shortfall means worse execution.** Report gross and fee-adjusted values separately. Tick values, multipliers, and fees must come from versioned instrument metadata or explicit scenario assumptions. Exchange fee schedules depend on participant status and transaction characteristics, so the fee scenario names the assumed status. A constant identical per-contract fee cancels between two alternatives that both complete; it does not cancel when completion differs, because the residual valuation in `C_T` (section 8) carries no fee.

Unit example, purely synthetic: `delta = 0.25`, `M = 50`, `m0 = 100`, one-contract buy at `100.25`, and fee `2.50` gives `1.20 ticks`. Selling at `99.75` under the same parameters also gives `1.20 ticks`.

Do not add a post-fill markout penalty to this objective. That is a different objective and can double-count price effects.

### 7.2 Post-fill markout

$$
Markout_h=s\frac{m_{t_{fill}+h}-p_{fill}}{\delta}.
$$

Positive is favorable. Markout uses a declared future committed-book reference, with a freshness/status rule and missing-value treatment. Missing horizons are not silently replaced by a convenient later price.

Markout is a diagnostic of conditional outcomes, not proof that a passive policy has lower completed-task cost.

### 7.3 Completion and comparisons

Report deadline misses, residual quantities, passive-fill fraction, completion time, action count, and cost distributions alongside shortfall.

The primary paired cost table uses tasks on which **B3 and B3_NO_QUEUE both complete on time**, explicitly labeled their pairwise common-completion subset. Adding B0, B1, B2, C++ variants, or P1 cannot change that primary sample. Other pairwise comparisons use separately identified sets; an all-policy table is secondary. Publish denominators and exclusion counts, and each policy's complete outcome distribution.

When completion differs, lower conditional cost alone MUST NOT be described as overall superiority. Do not assign an arbitrary invisible penalty to failures. Any scalar failure penalty requires separate preregistration and sensitivity analysis.

Per-session paired differences are the default aggregation unit; average session effects equally. Provide task-weighted results as a labeled secondary analysis. Positive `IS_baseline - IS_candidate` favors the candidate.

Also report the paired deadline-miss-rate difference on **all technically evaluable tasks**, averaged by session, where positive `miss_candidate - miss_baseline` is worse. Pair buy/sell probes within their session block; they are not independent replicates. Keep primary effect and completion-risk uncertainty together. Zero observed failures do not establish zero risk, and few independent sessions cannot support precise rare-event claims. Without a defensible completion comparison, describe a conditional cost improvement only.

Report a secondary effect within the shared, client-defined checkpoint-eligible population to show how often the decision matters. Eligibility uses only the identical pre-checkpoint histories, never future fill success or policy choice. The full task population remains primary; do not hide dilution from early fills or frequent fallbacks.

## 8. Modeling requirements

Begin with an interpretable multinomial logistic price model and a fixed-horizon fill diagnostic. B3 additionally needs **action-conditioned risk and value models**; a submission-time fill predictor is insufficient for an aged-order checkpoint. One gradient-boosted comparator is permitted after the simple models are stable. A hazard model is outside the core.

B3 training branches `HOLD` and `SWITCH` from the **same B1-prefix checkpoint** on development data, including simulator state, pending commands, and delayed observations/reports. Each branch runs to the deadline under its fixed continuation. The learner receives only checkpoint client features. Store both complete branch outcomes; never drop expensive failures from cost training. Paired probe construction identifies outcomes under the simulator's assumptions, not outcomes from a real randomized exchange experiment.

For each branch, let `T` be the task deadline, `q_T` the quantity executed by `T`, `r_T = 1 - q_T` the residual, `z0` the fixed initial client midpoint (architecture section 11.2), and `m_T` the last valid committed reference midpoint at or before `T`. This section is the single authoritative definition of the deadline-horizon pair used by B3 labels, B2 threshold selection, and P1:

$$
d_a = 1[r_T > 0], \qquad
C_{T,a} = \sum_{i:\,t_i\le T}\left[\frac{s\,q_i(p_i-z_0)}{\delta}+\frac{f_i}{\delta M}\right]
+\frac{s\,r_T(m_T-z_0)}{\delta}.
$$

Predict `p_a = E[d_a | client features]` and `v_a = E[C_T,a | client features]`. The residual mark is a **valuation**, not a fill, cash realization, or primary shortfall, and carries no invented fee. An eligible task has a valid arrival midpoint, so a last valid reference exists; allow a stale reference for this auxiliary valuation and record its age. Source corruption instead produces policy-independent technical unevaluability. Never let future reference availability or realized label status determine checkpoint fallback; only frozen model support and current client features may do so. Late diagnostic executions are outside this deadline target. On-time executions with late reports count exactly once.

High fill probability alone is not an objective. A decomposition into fill/non-fill conditional costs must recover the same unconditional target; never multiply fill probability by an unrelated return forecast. If development contains no deadline misses, a declared constant empirical risk estimate is acceptable, with support disclosed; it is not evidence that risk is zero. Report sensitivity of B3 decisions across the frozen `epsilon` set and any collapse to a single action.

Generate shared price-score inputs for B3 training with **forward-chained out-of-fold** training/calibration or an earlier disjoint training slice. In-sample fitted scores create a stacking mismatch. Freeze a final shared price model for validation/test. All rows from a task, its two actions, and its side pair remain within the same session split.

Use only features available to the client at decision time. Simulator-truth queue position and unreported fills are prohibited inputs. An oracle-state diagnostic is allowed only as an explicitly unrealistic upper-information ablation.

For probability models report proper scoring rules, calibration, class support, and regime behavior; for value models report error and calibration of predicted versus realized paired costs. Compare to unconditional/base-rate predictors. Report checkpoint eligibility, disagreement, and fallback rates. P1 uses the same target and controller as B3 but recursively evaluates future decisions under the constrained objective in architecture section 11.3; its optimality holds only in its estimated finite model and depends on the partial-observation approximation.

## 9. Replay limitations and scientific controls

Historical replay leaves subsequent historical order flow unchanged by hypothetical orders.[^replay] One-contract sizing reduces some impact and repeated-consumption complications but does not prove that the market is unaffected.

Every result MUST name its queue/fill rule, timing convention, latency settings, fee assumptions, and unsupported mechanisms. The report must distinguish code correctness from validity of the counterfactual model.

Fill-model labels are tagged as `REPLAY_PROBE` or `HISTORICAL_ORDER`. The former cannot independently validate the replay. The latter reflect actual participants' cancellation and modification decisions; cancellation is not evidence that the order could never have filled if held.

Only perturb genuinely uncertain aspects of queue insertion and execution attribution. An “optimistic” model may not allocate more volume than its stated execution evidence allows. An “invariant across models” result is robustness within those models, not a mathematical bound on reality.

## 10. Evaluation and holdout discipline

Use chronological training, validation, and final-test periods. Choose dates and sufficient independent sessions after the pilot; no particular split percentage guarantees adequacy.

Use development session-level paired variability to choose a practically meaningful precision target in ticks and a feasible test duration. A proposed planning target is a 95% interval half-width of 0.1 tick; this is a resource/precision goal, not a promise or a minimum publishable positive result. If the required budget is impractical, reduce the claim to an exploratory study and explain the achieved uncertainty. Base the estimate on development session-level paired differences, including tasks where the compared policies take the same action (they contribute zero difference: this dilutes the effect and its variance together, so the relevant quantity is effect relative to standard error, not interval width alone). A longer primary horizon reduces tasks per session through the arrival-spacing rule and must be reflected. Metadata-only holdout integrity checks are permitted and logged; policy performance remains unexamined until the freeze.

Learn scalers, features, bins, thresholds, and calibration on permitted development data. Retain label windows and purge training examples whose outcomes cross evaluation boundaries. Keep session blocks intact where possible. All variants in a frozen comparison campaign share its final-test window; a later extension follows the new-holdout rule in section 11.

Predeclare primary comparisons, metrics, effect-size thresholds where used, regime definitions, bootstrap/block choices, and the sensitivity grid. Record all attempted model/policy variants.

Freeze the research code, configuration, and model/policy artifacts before examining final-test performance. A genuine bug may invalidate a run; document the bug and rerun. Methodological changes prompted by test outcomes make that sample exploratory, not untouched confirmation.

Use dependence-aware uncertainty estimates. Report the number of independent session blocks and limitations when the test sample is small. Predeclare multiple-comparison handling when making several confirmatory superiority claims.

## 11. Acceptance criteria and required artifacts

| ID | Requirement | Acceptance evidence |
|---|---|---|
| R01 | Suitable data and source semantics | Approved G0 pilot, source note, and passed G2-S support gate |
| R02 | Correct historical reconstruction | Hand-worked fixtures, real-data comparison, anomaly ledger |
| R03 | Causal observations and responses | Timing/non-leakage tests |
| R04 | Safe order exposure and completion controller | Race, cancellation, and deadline tests |
| R05 | Single-counted execution and cost accounting | Volume and arithmetic fixtures |
| R06 | Fair, policy-independent tasks | Immutable task manifest and outcome counts |
| R07 | Interpretable statistical models | Calibrated evaluation and declared label provenance |
| R08 | Matched policy comparisons and uncertainty | Reproducible paired result tables |
| R09 | Assumption sensitivity and narrow claims | Named scenario results and limitations |
| R10 | Reproducible public release | Credential-free synthetic demonstration and licensed-data instructions |
| R11 | Optional C++ systems extension | Differential tests, sanitizers, honest benchmark |
| R12 | Optional P1 research extension | Implemented toy/empirical solver, support diagnostics, held-out comparison to B3 |

Core release requires R01–R10; R07 permits a documented negative modeling result rather than fabricated calibration success. The systems extension adds R11. The P1 extension adds R12, using Python or a validated native engine. A stopped feasibility study does not satisfy R11 or R12. If an extension is developed after the core test is unblinded, treat that test as exploratory for the extension or acquire a new untouched test window.

Deliver a substantive research report, publication-ready source repository, synthetic fixtures, dependency locks, immutable experiment manifests, task-level audit traces, and a short technical walkthrough. Actual external publication requires authorization. A performance benchmark accompanies R11; a basic resource/runtime report accompanies the core. Page count is not a quality gate.

The report must include a data/coverage table, one causal order trace, probability/value diagnostics, the primary cost effect with completion-risk outcomes, an assumption-sensitivity figure, and an honest limitations section. A reviewer must be able to reproduce a synthetic task and trace a headline number to its input manifest. Presentation quality matters, but a dashboard is unnecessary.

## 12. Tools, constraints, and change control

Use a vendor SDK for acquisition/decoding; NumPy and scikit-learn for modeling; Parquet plus one primary dataframe/query tool for derived data; established test/build tools; and GitHub Actions for CI. The C++ extension uses pybind11. Exact choices and build-versus-buy boundaries are in the architecture.

Use a local workstation first; cloud compute is not a prerequisite. Do not impose an arbitrary events-per-second target or claim a hiring outcome. Secure user approval before paid data or compute, external uploads, or publication.

Unresolved implementation inputs are **gates**, not facts to invent: exact contract/dates, matching and implied-liquidity treatment, data budget, fee scenario, clock-quality tolerance, numerical latency scenarios, G2-S support minima and primary horizon, primary risk allowance `epsilon`, holdout size, and novelty assessment. Resolve these on development data and source documentation before the affected milestone.

## 13. Sources and interpretation

Sources were checked for this planning revision on September 27, 2026. They support factual premises; proposed architecture, scope, and acceptance requirements are project decisions.

[^placement]: Lehalle and Mounjid, *Limit Order Strategic Placement with Adverse Selection Risk and the Role of Latency*. https://arxiv.org/abs/1610.00261
[^imbalance]: Gould and Bonart, *Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book*. https://arxiv.org/abs/1512.03492
[^glbx]: Databento, *CME Globex MDP 3.0*. https://databento.com/docs/venues-and-datasets/glbx-mdp3
[^fields]: Databento, *Common fields, enums and types*. https://databento.com/docs/standards-and-conventions/common-fields-enums-types
[^replay]: HftBacktest, *Order Fill*. https://hftbacktest.readthedocs.io/en/latest/order_fill.html
[^acquisition]: Databento, *Historical API reference*, including metadata cost, record-count, and billable-size estimates. https://databento.com/docs/api-reference-historical?historical=http
