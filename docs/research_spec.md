# WP-RESEARCH specification (binding)

Owned paths: `python/qexec/policies/model_policies.py`, `python/qexec/experiments/` (all files),
`python/qexec/analysis/support.py`, `python/qexec/analysis/tables.py`, tests in
`tests/unit/experiments/`, `tests/unit/policies/test_model_policies.py`,
`tests/unit/analysis/test_support.py`, `tests/unit/analysis/test_tables.py`,
`tests/integration/test_study_*.py`. Everything else is read-only (report needed changes).

Reuse, do not reimplement: `SessionEngine`/`SessionOutputs` (`sim.engine`), `B0Policy`/`B1Policy`
and `checkpoint_eligibility` (`policies`), `features.groups` (allowlists, `assert_allowed`,
`mechanics_features`), `labels.price` (`MidSeries`, `price_direction_label`), `models.*`
(`PriceModel`, `ActionModels`, `FeatureSchema`, `SupportRule`, `ModelArtifact`,
`forward_chained_oof_scores`, `risk_allowance_choice`, `select_theta`), `analysis.metrics`,
`analysis.stats`, `synthetic.generate_study` / `write_synthetic_session`, `core.config` constants.

All results are synthetic software validation. Every written artifact (`metrics.json`, tables)
carries `"data_kind": "SYNTHETIC"`.

## 1. Model policies (`policies/model_policies.py`)

- `PriceSignalScorer(primary: PriceModel, secondary: PriceModel | None)`: callable on a
  market-feature dict -> `{"p_down","p_unch","p_up","u_signal"}` (+ `"u_signal_100ms"` from the
  secondary model). Feature order from the PRICE allowlist; unknown/missing feature -> hard error.
- `B2Policy(theta, signal_key="u_signal", policy_id="B2")`: JOIN_BEST at arrival; at an eligible
  checkpoint SWITCH iff `side * view.price_signal[signal_key] > theta` (strict; equality HOLD),
  reason MODEL_CHOICE. Missing/nonfinite signal -> (SWITCH, FALLBACK_NONFINITE). `B2_100MS` =
  `B2Policy(theta_100, "u_signal_100ms", "B2_100MS")`.
- `B3Policy(models: ActionModels, schema: FeatureSchema, support: SupportRule, epsilon,
  policy_id)`: JOIN_BEST at arrival; at an eligible checkpoint build x in `schema` order from
  `view.market_features | mechanics_features(view, tick) | view.price_signal |
  view.queue_features`, call `assert_allowed(schema.names, policy_id)`, predict p_hat/v_hat, then
  `risk_allowance_choice(p_hat, v_hat, epsilon, supported=support.is_supported(x))`. Keep a
  `decision_log` list of dicts (task_id, time_ns, p_hold, p_switch, v_hold, v_switch, supported,
  choice, reason) so epsilon sensitivity can be recomputed from stored predictions without
  rerunning replay. Same class serves `B3` and `B3_NO_QUEUE` (different models/schema/policy_id).
  Schema mismatch is a run error, never a fallback (T52).

## 2. Study pipeline (`experiments/`)

`StudyConfig` (frozen dataclass, JSON round-trip, unknown keys rejected): `study_id`,
`n_sessions` (default 10), `duration_s` (default 600), `base_seed`, `epsilon` (0.001),
`epsilon_sensitivity` (core default), `horizon_ladder_ns` (core default), `scenarios`
(default all L0-L6), `split_fractions` (0.5, 0.2, 0.3), `support_min_eligible` (30),
`support_min_queue_depletion` (3), `support_margin_frac` (0.5), `n_pilot_sessions` (3),
`bootstrap_n` (2000), `bootstrap_seed`, `max_workers` (default min(8, cpu_count)),
`quick` (bool).

`run_study(out_dir, cfg) -> StudyResult` executes, writing every stage's artifacts under
`out_dir` (layout below) and logging each stage:

1. **Data**: require a fresh directory and generate synthetic sessions `SYN-0001..`.
   Record generator source SHA-256, parameters and session metadata. Chronological splits need
   at least two train sessions for OOF scores, one validation and one test. Pilots stay in train.
   All stages share the declared scenario task population; configuration rejects ambiguous
   integer coercions, nonfinite or duplicated menus and pilots extending outside training.
2. **G2-S support gate** (product 4.4) on the first `n_pilot_sessions` train sessions, L1,
   B1-only runs at each ladder horizon valid under every planned scenario (`checkpoint_valid`). Selection rule frozen
   in config before running: primary H = shortest horizon whose every pilot session has
   technically evaluable `decision_eligible_checkpoints >= support_min_eligible` and `post_checkpoint_queue_depletion
   >= support_min_queue_depletion`. Selection reads support counts only (T59: a test must show
   that changing cost/markout columns cannot change the selection). If none passes, the study
   stops with a written `support_gate.json` verdict `FAILED` and no further stages.
3. **Development labels** at primary H, L1, on train+validation sessions: engine with B1 and
   `fork_label_probe=True` -> `branch_labels` and `checkpoint_features`. Price labels for each
   checkpoint row via `MidSeries.from_session` and `price_direction_label` at tau =
   `primary_price_tau_ns(H)` (primary) and 100 ms (secondary), rows retained with class 0 (T43);
   source-invalid rows are excluded from all fits, tuning and diagnostics (counted). Remaining
   rows with None labels are excluded from price fitting (counted). Complete replacement
   snapshots initialize references; labels crossing source discontinuities are missing.
4. **Price models** (MARKET features only): forward-chained OOF over train sessions in order
   produces honest price scores for B3 training rows (T44); train sessions without an OOF score
   are excluded from B3 training (count reported). Final primary and secondary price models are
   fit on all train sessions (chronological inner validation = last train session for C
   selection) for validation and test.
5. **Action models**: `ActionModels` for B3 (allowlist `B3`) and B3_NO_QUEUE (allowlist
   `B3_NO_QUEUE`) on identical train rows (both arms per task), identical estimator families and
   grids; support ranges from the exact union training rows. Admission additionally requires
   each action's miss and cost fit population to meet the frozen minimum. Missing-cost rows
   remain in miss fitting; cost fits use the finite-cost subset. Ridge alpha is chosen on inner
   chronological training sessions, then refit on every eligible cost row. Assert schemas differ by
   `QUEUE_FEATURES` (T45). Validation diagnostics on validation branch labels: miss Brier score
   and log loss vs training-derived base rate, cost RMSE vs training-derived mean, with separate
   miss/cost/missing/technical denominators for both actions; price-model log
   loss vs class base rates; action disagreement rate B3 vs B3_NO_QUEUE; fallback rate.

   **Support rule and margin (defect fix).** The `SupportRule` is built with a *per-feature
   absolute margin* `support_margin_frac * (training max - min)` (`support_margin_frac` is a
   frozen `StudyConfig` field, default `0.5`, JSON round-trip). The margin is always finite and
   is exactly `0.0` for a constant feature (zero training spread); **every** feature stays in
   the support check — none is dropped — but a test value up to a modest fraction outside the
   observed `[min, max]` is still supported. This is a declared extrapolation allowance, not
   evidence of generalization. Constant OOF score features can still make final-model scores
   unsupported; the allowance does not guarantee a nondegenerate experiment. A
   value slightly outside the training range but inside the margin is supported; a value far
   outside is unsupported. The realized per-variant support rate is recorded and reported.

   **Degeneracy guardrails (never hidden).** `metrics.json` and `report_inputs.json` carry a
   `degeneracy_guardrails` block reporting, for B3 and B3_NO_QUEUE: the count of eligible-
   checkpoint decisions by reason (`MODEL_CHOICE` / `FALLBACK_UNSUPPORTED` /
   `FALLBACK_NONFINITE`), the ineligible-checkpoint reason counts, the number of eligible
   checkpoints, the fraction decided by the model (`model_choice_fraction`), and the support
   rate; plus the B3 vs B3_NO_QUEUE action-disagreement rate on the eligible checkpoints both
   decided (from the test decision logs). The block sets
   `"degenerate_primary_comparison": true` with a human-readable `degeneracy_reason` when the
   model decided `< 50%` of eligible checkpoints for either variant, or when the two variants'
   decisions are identical on 100% of shared eligible checkpoints **and** both are dominated by
   fallbacks (or when either variant has zero eligible checkpoints). The flag is always present
   (defaults to `false`); the degeneracy is never suppressed.
6. **Validation selection**: one engine pass per validation session (L1) with `B2` policy
   instances for every theta in the menu (policy ids `B2@0.0`, ...) and similarly `B2_100MS`;
   build rows (theta, session_id, miss, c_t) from technically evaluable task results and call
   `select_theta` (epsilon from config). Evaluable missing or nonfinite C_T is a hard error before
   freeze, never a reason to silently drop a deadline miss. Log all trials.
7. **Freeze**: write `freeze/manifest.json` (config, primary H, theta selections, epsilon, model
   artifact hashes (sha256 of saved files), session checksums, git commit from `git rev-parse
   HEAD` if available, and an explicit `frozen_at` UTC wall time), save every model with
   `ModelArtifact`. After the
   freeze, models are loaded back from disk for test evaluation (proves artifacts suffice).
   Artifact metadata names actual eligible fit sessions. Append every test access to
   `freeze/unblinding.log`; completion protects its existing bytes while permitting suffixes.
8. **Test evaluation** (frozen L1 models; never refit): for every test session and every
   configured scenario, an engine pass with policies B0, B1, B2, B2_100MS, B3, B3_NO_QUEUE.
   Horizon sensitivity: at L1 only, for every other ladder horizon that is checkpoint-valid,
   run B0 and B1 (descriptive; models are horizon-specific and are not transferred — state this
   in the outputs). Independent (session, scenario) runs may execute in a
   `ProcessPoolExecutor(max_workers)`; results are concatenated in a deterministic order.
9. **Analysis** (`analysis/tables.py`): with `analysis.stats`, for the primary configuration
   (primary H, L1): primary pair B3_NO_QUEUE (baseline) vs B3 (candidate) on `is_ticks_net`
   common-completion set; paired miss difference on all evaluable tasks; joint session
   bootstrap intervals; `zero_event_session_upper_bound` when misses are all zero; secondary
   pairs (B2 vs B3, B1 vs B3, B0 vs B1, B2 vs B2_100MS) each with their own pairwise sets.
   Per-policy outcome distribution (counts by status, passive fill fraction, mean/median IS,
   miss rate). Checkpoint-eligible secondary effect. Latency table: primary pair per scenario
   (frozen policies). Epsilon sensitivity: from `B3Policy.decision_log` of the test runs,
   recompute the chosen action for each epsilon in the sensitivity set and report action shares
   and agreement with the primary epsilon (no replay rerun; state that realized outcomes for
   other epsilons would require replay and are not claimed). Support table (G2-S). T49: adding
   policies cannot change the primary estimate (test).
10. **Outputs**: `out_dir/results/{tasks,task_results,decisions,executions,reports,quality,
    markouts,branch_labels_dev,support,horizon_sensitivity}.parquet`,
    `out_dir/results/metrics.json` (all headline numbers with denominators,
    interval, n sessions defined/undefined), `out_dir/results/tables/*.csv`, and
    `out_dir/results/report_inputs.json` (everything the report needs). Return `StudyResult` with
    paths and the headline numbers. Post-fill markouts at 10 ms, 100 ms and 1 s are descriptive,
    task-weighted diagnostics by policy, scenario, side and mechanism. References require covered,
    continuous source windows, valid trading state and a frozen maximum age of 1 s; missing
    references retain reasons and denominators. Markouts never enter selection or causal effects.
    Completion seals all result/stage/freeze artifacts; report and trace access fail closed.
    `resources.json` records measured runtime through analysis, generated input bytes, worker
    count and environment. Memory is explicitly unmeasured. Resource metadata can vary across
    machines and never influences generated data, decisions or statistical calculations.

`quick=True` (for CI smoke and demo): 4 sessions x 120 s, scenarios (L1, L6), bootstrap 200,
horizon ladder (1 s, 5 s), pilot sessions 1, support minima scaled down (eligible >= 5,
queue depletion >= 1), max_workers 2. Must finish in < 180 s on this machine.

## 3. Required tests

T23 (outcome windows never cross split boundaries: sessions are whole; a task's rows all in one
split; assert no session id appears in two splits), T26 (theta chosen only from validation rows),
T44 (OOF provenance recorded and verified), T45, T46 (B2 sign for buy/sell, equality HOLD, single
decision), T47 via B3Policy end to end on a stub ActionModels, T49, T52, T59, T60 (B2 selection,
B3, B3_NO_QUEUE all route through `risk_allowance_choice`/`select_theta` with the same epsilon),
plus an integration test running `run_study(quick=True)` end to end in tmp_path asserting every
output file exists, `data_kind == "SYNTHETIC"`, the primary sample is labeled, freeze manifest
hashes match saved artifacts, and test-time models were loaded from disk.
