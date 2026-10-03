# QExec research report — study `demo`

> **Published example.** This is a sanitized report from a completed local synthetic demo. Statistical results, guardrail counts, and configuration are unchanged. Local paths and machine-specific metadata are omitted. The complete sealed run remains local; this example is a browsable illustration, not an input bundle for study verification.

> **SYNTHETIC-DATA DISCLAIMER.** Every number in this report is produced from **synthetic** limit-order-book data generated locally by `qexec.synthetic`. These results are **software validation of the research pipeline, not a market finding**. They say nothing about any real instrument, venue, or strategy. No profitability, edge, or novelty is claimed or implied.

> **Real-data gate (G0) is BLOCKED pending paid data.** The real-data feasibility decision, real-data audits, and the final real-data holdout evaluation require a licensed market-data subscription that has not been purchased, plus vendor decoding and a real-session study entry point that are not implemented. These remain future work; this build makes no real-data claim.

## Research question

On synthetic data, does the queue-aware policy **B3** reduce paired implementation-shortfall cost relative to the otherwise-identical no-queue policy **B3_NO_QUEUE**, without worsening completion risk (deadline-miss rate), under the frozen latency / horizon / epsilon protocol? This is a *software-validation* question: it checks that the estimation machinery runs end to end and reports honest denominators and intervals. It is not a claim about real markets.

Primary configuration: horizon **1000 ms (1000000000 ns)**, primary latency scenario, epsilon **0.001**, fee scenario 0 (gross) with a net variant.

## Data and coverage

Data kind: **SYNTHETIC**.

| split | sessions |
|---|---|
| train | 2 |
| validation | 1 |
| test | 1 |

Per-policy task coverage (primary configuration):

| policy | n_tasks | completed_on_time | deadline_miss | technically_unevaluable |
|---|---|---|---|---|
| B0 | 24 | 24 | 0 | 0 |
| B1 | 24 | 22 | 2 | 0 |
| B2 | 24 | 23 | 1 | 0 |
| B2_100MS | 24 | 23 | 1 | 0 |
| B3 | 24 | 24 | 0 | 0 |
| B3_NO_QUEUE | 24 | 24 | 0 | 0 |

## G2-S support and selected horizon

Selected primary horizon: **1000 ms (1000000000 ns)**. The horizon is chosen from support counts only (decision-eligible checkpoints and post-checkpoint queue-depletion fills per session); cost and markout columns cannot influence the selection.

| horizon_ns | passes | min_eligible | min_queue_depletion |
|---|---|---|---|
| 1000000000 | True | 21 | 3 |
| 5000000000 | True | 9 | 6 |

## Model diagnostics

Selected thresholds: theta_primary = **0**, theta_secondary = **0**. Detailed per-model validation diagnostics are recorded in `stages/development.json` and `stages/validation.json`.

## Primary paired effect (cost) and completion risk (miss)

Baseline **B3_NO_QUEUE** vs candidate **B3**. Positive cost effect favours the candidate (lower implementation shortfall); positive miss difference means the candidate misses the deadline *more* often (worse). Cost is a per-session paired mean on the common-completion set; the miss difference is over all technically evaluable tasks. Null intervals are rendered `not estimable`; an undefined effect is rendered `undefined`.

### Policy use and comparison guardrails

Degenerate primary comparison: **True**.

> **DEGENERATE PRIMARY COMPARISON.** This paired effect does not establish a queue-model advantage. Inspect model use and fallback counts before interpreting the numerical effect.

Saved guardrail reason: B3 model-choice fraction 0.000 < 0.5 (dominated by fallbacks); B3_NO_QUEUE model-choice fraction 0.000 < 0.5 (dominated by fallbacks); B3 and B3_NO_QUEUE decisions identical on 100% of shared eligible checkpoints while both are dominated by fallbacks (comparison degenerate by construction)

| policy | eligible checkpoints | support rate | model-choice fraction | MODEL_CHOICE | FALLBACK_UNSUPPORTED | FALLBACK_NONFINITE |
|---|---|---|---|---|---|---|
| B3 | 21 | 0 | 0 | 0 | 21 | 0 |
| B3_NO_QUEUE | 21 | 0 | 0 | 0 | 21 | 0 |

Rates use each policy's eligible checkpoints as their denominator. The pilot horizon-support gate and model feature-support rate are distinct checks.

Shared eligible checkpoints: **21**; action disagreements: **0** (rate **0**).

| quantity | estimate | 95% interval | n_sessions_defined | n_sessions_undefined | n_tasks (denominator) |
|---|---|---|---|---|---|
| cost effect (is_ticks_net) | 0 | not estimable | 1 | 0 | 24 |
| completion-risk (miss diff) | 0 | not estimable | 1 | 0 | 24 |

> **Zero-observed-miss note.** Every evaluable candidate task completed on time, so the miss rate point estimate is 0. This is **not** a zero-risk claim: the one-sided upper bound on the per-session miss probability is **0.95** (`zero_event_session_upper_bound`).

## Secondary pairs

| baseline | candidate | cost effect | cost interval | miss diff | miss interval | n_tasks |
|---|---|---|---|---|---|---|
| B2 | B3 | -0.08696 | not estimable | -0.04167 | not estimable | 23 |
| B1 | B3 | -0.1364 | not estimable | -0.08333 | not estimable | 22 |
| B0 | B1 | 0.2727 | not estimable | 0.08333 | not estimable | 22 |
| B2 | B2_100MS | -0.08696 | not estimable | 0 | not estimable | 23 |

## Latency sensitivity

Primary pair effect per latency scenario (frozen policies).

| scenario | cost effect | cost interval | miss diff | n_tasks |
|---|---|---|---|---|
| L1 | 0 | not estimable | 0 | 24 |
| L6 | 0 | not estimable | 0 | 24 |

## Epsilon sensitivity (decision-only)

Action shares recomputed from stored decision predictions as the risk-allowance epsilon varies. **Decision-only:** realized outcomes for non-primary epsilons would require a replay and are *not* claimed here.

| epsilon | share_hold | share_switch | agreement_with_primary | is_primary |
|---|---|---|---|---|
| 0 | 0 | 1 | 1 | False |
| 0.0001 | 0 | 1 | 1 | False |
| 0.001 | 0 | 1 | 1 | True |
| 0.01 | 0 | 1 | 1 | False |
| 1 | 0 | 1 | 1 | False |

## Per-policy outcome distribution

| policy | passive_fill_fraction | mean_is_ticks_net | median_is_ticks_net | miss_rate |
|---|---|---|---|---|
| B0 | 0 | 0.5 | 0.5 | 0 |
| B1 | 0.2727 | 0.2273 | 0.5 | 0.08333 |
| B2 | 0.2174 | 0.2826 | 0.5 | 0.04167 |
| B2_100MS | 0.1739 | 0.3696 | 0.5 | 0.04167 |
| B3 | 0.125 | 0.375 | 0.5 | 0 |
| B3_NO_QUEUE | 0.125 | 0.375 | 0.5 | 0 |

## Model calibration and error (validation)

Per-action miss calibration (Brier score and log loss vs the base-rate predictor) and cost error (RMSE vs the constant training-mean predictor), computed on the validation branch labels. A model that beats the reference has a lower log loss / RMSE than its reference column. B3 vs B3_NO_QUEUE action-disagreement rate: **0** (computed with the frozen risk-allowance rule, exactly as the policy decides).

| variant | action | miss_brier | miss_log_loss | base_rate_log_loss | cost_rmse | constant_mean_rmse | n_rows | n_cost_rows | n_cost_missing | n_technical_excluded |
|---|---|---|---|---|---|---|---|---|---|---|
| B3 | HOLD | 0.08981 | 0.2904 | 0.09097 | 2.032 | 0.493 | 19 | 19 | 0 | 0 |
| B3 | SWITCH | 0.08981 | 0.2904 | 0.09097 | 0.5708 | 0.4166 | 19 | 19 | 0 | 0 |
| B3_NO_QUEUE | HOLD | 0.03316 | 0.1487 | 0.09097 | 1.635 | 0.493 | 19 | 19 | 0 | 0 |
| B3_NO_QUEUE | SWITCH | 0.03316 | 0.1487 | 0.09097 | 0.5707 | 0.4166 | 19 | 19 | 0 | 0 |

### Price forecasts

Class-frequency reference probabilities are estimated on training sessions. Invalid source windows and missing labels are excluded and counted.

| label | n_rows | n_checkpoint_rows | n_technical_excluded | n_label_missing | log_loss | base_rate_log_loss |
|---|---|---|---|---|---|---|
| primary | 19 | 19 | 0 | 0 | 0.296 | 0.07411 |
| secondary | 19 | 19 | 0 | 0 | 0.389 | 0.1001 |

## Support diagnostics: which features trigger unsupported

Per-feature count of validation checkpoints falling outside the frozen support band (a row may be out of range on several features). The support region is built from the exact action-training matrix (R20); these counts explain any fallback rate honestly.

**B3**

| feature | unsupported_count |
|---|---|
| limit_offset_ticks | 3 |
| mid_change_1s_ticks | 2 |
| p_down | 19 |
| p_unch | 19 |
| p_up | 19 |
| q_depleted | 1 |
| trade_count_1s | 1 |
| trade_flow_signed_1s | 1 |
| u_signal | 19 |

**B3_NO_QUEUE**

| feature | unsupported_count |
|---|---|
| limit_offset_ticks | 3 |
| mid_change_1s_ticks | 2 |
| p_down | 19 |
| p_unch | 19 |
| p_up | 19 |
| trade_count_1s | 1 |
| trade_flow_signed_1s | 1 |
| u_signal | 19 |

## Execution markouts

Descriptive post-execution diagnostics at 10 ms, 100 ms and 1 s. References use only committed midpoints at or before each target, subject to the recorded freshness limit. Missing references are counted as missing, never zero. These are selected execution samples, not matched policy-effect estimates or evidence of market alpha.

| data_kind | fill_mechanism | horizon_ns | markout_horizon_ns | mean_markout_ticks | median_markout_ticks | n_completed | n_defined | n_missing | n_sessions | policy_id | scenario_id | side |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 12 | 12 | 0 | 1 | B0 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.5833 | -0.5 | 12 | 12 | 0 | 1 | B0 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.5 | -0.5 | 12 | 11 | 1 | 1 | B0 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 12 | 12 | 0 | 1 | B0 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.4167 | -0.5 | 12 | 12 | 0 | 1 | B0 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.5 | -0.5 | 12 | 11 | 1 | 1 | B0 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 7 | 7 | 0 | 1 | B1 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.5 | -0.5 | 7 | 7 | 0 | 1 | B1 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.2143 | -0.5 | 7 | 7 | 0 | 1 | B1 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.25 | 0.5 | 4 | 4 | 0 | 1 | B1 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 4 | 4 | 0 | 1 | B1 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.75 | 0.5 | 4 | 4 | 0 | 1 | B1 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B2 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.375 | -0.5 | 9 | 8 | 1 | 1 | B2 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B2 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.1667 | 0.5 | 3 | 3 | 0 | 1 | B2 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 3 | 3 | 0 | 1 | B2 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.8333 | 0.5 | 3 | 3 | 0 | 1 | B2 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.3889 | -0.5 | 9 | 9 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.3889 | -0.5 | 9 | 9 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B2_100MS | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.3889 | -0.5 | 10 | 9 | 1 | 1 | B2_100MS | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B2_100MS | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0 | 0 | 2 | 2 | 0 | 1 | B2_100MS | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B2_100MS | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B2_100MS | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B3 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.3889 | -0.5 | 10 | 9 | 1 | 1 | B3 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B3 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0 | 0 | 2 | 2 | 0 | 1 | B3 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B3 | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B3 | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.4091 | -0.5 | 11 | 11 | 0 | 1 | B3 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.5 | -0.5 | 11 | 10 | 1 | 1 | B3 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.3182 | -0.5 | 11 | 11 | 0 | 1 | B3 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3 | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3 | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B3_NO_QUEUE | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.3889 | -0.5 | 10 | 9 | 1 | 1 | B3_NO_QUEUE | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B3_NO_QUEUE | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0 | 0 | 2 | 2 | 0 | 1 | B3_NO_QUEUE | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B3_NO_QUEUE | L1 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B3_NO_QUEUE | L1 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.4091 | -0.5 | 11 | 11 | 0 | 1 | B3_NO_QUEUE | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.5 | -0.5 | 11 | 10 | 1 | 1 | B3_NO_QUEUE | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.3182 | -0.5 | 11 | 11 | 0 | 1 | B3_NO_QUEUE | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3_NO_QUEUE | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3_NO_QUEUE | L1 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3_NO_QUEUE | L1 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.4167 | -0.5 | 12 | 12 | 0 | 1 | B0 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.5833 | -0.5 | 12 | 12 | 0 | 1 | B0 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.5 | -0.5 | 12 | 11 | 1 | 1 | B0 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5833 | -0.5 | 12 | 12 | 0 | 1 | B0 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.4167 | -0.5 | 12 | 12 | 0 | 1 | B0 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.5 | -0.5 | 12 | 11 | 1 | 1 | B0 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 7 | 7 | 0 | 1 | B1 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.5 | -0.5 | 7 | 7 | 0 | 1 | B1 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.3571 | -0.5 | 7 | 7 | 0 | 1 | B1 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.25 | 0.5 | 4 | 4 | 0 | 1 | B1 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 4 | 4 | 0 | 1 | B1 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.75 | 0.5 | 4 | 4 | 0 | 1 | B1 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B1 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.375 | -0.5 | 9 | 8 | 1 | 1 | B2 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.375 | -0.5 | 9 | 8 | 1 | 1 | B2 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B2 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.1667 | 0.5 | 3 | 3 | 0 | 1 | B2 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 3 | 3 | 0 | 1 | B2 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.8333 | 0.5 | 3 | 3 | 0 | 1 | B2 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.7222 | -0.5 | 9 | 9 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.2778 | -0.5 | 10 | 9 | 1 | 1 | B2_100MS | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.2778 | -0.5 | 10 | 9 | 1 | 1 | B2_100MS | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B2_100MS | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0 | 0 | 2 | 2 | 0 | 1 | B2_100MS | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B2_100MS | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B2_100MS | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.5 | -0.5 | 9 | 9 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6111 | -0.5 | 9 | 9 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B2_100MS | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.2778 | -0.5 | 10 | 9 | 1 | 1 | B3 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.2778 | -0.5 | 10 | 9 | 1 | 1 | B3 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B3 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0 | 0 | 2 | 2 | 0 | 1 | B3 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B3 | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B3 | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.6 | -0.5 | 11 | 10 | 1 | 1 | B3 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.6 | -0.5 | 11 | 10 | 1 | 1 | B3 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.4091 | -0.5 | 11 | 11 | 0 | 1 | B3 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3 | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3 | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.2778 | -0.5 | 10 | 9 | 1 | 1 | B3_NO_QUEUE | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.2778 | -0.5 | 10 | 9 | 1 | 1 | B3_NO_QUEUE | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.6 | -0.5 | 10 | 10 | 0 | 1 | B3_NO_QUEUE | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 10000000 | 0 | 0 | 2 | 2 | 0 | 1 | B3_NO_QUEUE | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 100000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B3_NO_QUEUE | L6 | -1 |
| SYNTHETIC | QUEUE_DEPLETION | 1000000000 | 1000000000 | 0.5 | 0.5 | 2 | 2 | 0 | 1 | B3_NO_QUEUE | L6 | -1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 10000000 | -0.6 | -0.5 | 11 | 10 | 1 | 1 | B3_NO_QUEUE | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 100000000 | -0.6 | -0.5 | 11 | 10 | 1 | 1 | B3_NO_QUEUE | L6 | 1 |
| SYNTHETIC | AGGRESSIVE | 1000000000 | 1000000000 | -0.4091 | -0.5 | 11 | 11 | 0 | 1 | B3_NO_QUEUE | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 10000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3_NO_QUEUE | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 100000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3_NO_QUEUE | L6 | 1 |
| SYNTHETIC | TRADE_THROUGH | 1000000000 | 1000000000 | 0.5 | 0.5 | 1 | 1 | 0 | 1 | B3_NO_QUEUE | L6 | 1 |

## Causal order trace (one world)

| step | time_ns | kind | detail |
|---|---|---|---|
| 0 | 1767882607264272120 | arrival |  |
| 1 | 1767882607264272120 | arrival_decision | action=JOIN_BEST |
| 2 | 1767882607264272120 | command_created | arrival_ns=1767882607264572120, command_id=SYN-0004:00000:BUY\|B3:cmd:0:PASSIVE, command_kind=PASSIVE_LIMIT, send_ns=1767882607264322120 |
| 3 | 1767882607264572120 | command_arrival | command_id=SYN-0004:00000:BUY\|B3:cmd:0:PASSIVE, command_kind=PASSIVE_LIMIT, limit_price_fixed=4999750000000 |
| 4 | 1767882607264822120 | report_delivery | changed=True, cumulative_executed=0, exchange_time_ns=1767882607264572120, report_id=SYN-0004:00000:BUY:0, report_kind=ACCEPTED |
| 5 | 1767882607764272120 | checkpoint | choice=SWITCH, client_status=TRADING, eligibility=MODEL_CHOICE, reason=FALLBACK_UNSUPPORTED |
| 6 | 1767882607764272120 | controller_enter | state=WORKING |
| 7 | 1767882607764272120 | command_created | arrival_ns=1767882607764572120, command_id=SYN-0004:00000:BUY\|B3:cmd:1:CANCEL, command_kind=CANCEL, send_ns=1767882607764322120 |
| 8 | 1767882607764572120 | command_arrival | command_id=SYN-0004:00000:BUY\|B3:cmd:1:CANCEL, command_kind=CANCEL, limit_price_fixed=None |
| 9 | 1767882607764822120 | report_delivery | changed=True, cumulative_executed=0, exchange_time_ns=1767882607764572120, report_id=SYN-0004:00000:BUY:1, report_kind=CANCELLED |
| 10 | 1767882607764822120 | command_created | arrival_ns=1767882607765122120, command_id=SYN-0004:00000:BUY\|B3:cmd:2:AGGRESSIVE, command_kind=AGGRESSIVE, send_ns=1767882607764872120 |
| 11 | 1767882607765122120 | command_arrival | command_id=SYN-0004:00000:BUY\|B3:cmd:2:AGGRESSIVE, command_kind=AGGRESSIVE, limit_price_fixed=None |
| 12 | 1767882607765372120 | report_delivery | changed=True, cumulative_executed=1, exchange_time_ns=1767882607765122120, report_id=SYN-0004:00000:BUY:2, report_kind=FILL |
| 13 | 1767882608264272120 | deadline_freeze |  |
| 14 | 1767882608264272120 | outcome | executed_quantity=1, status=COMPLETED_ON_TIME |

## Limitations

- **Replay counterfactual.** The engine replays an immutable historical tape; the hypothetical order never affects the tape. Market impact and reactive counterparties are absent. Omitting impact can understate live costs; the overall bias is not guaranteed to have one sign because fill and queue assumptions also matter.
- **Synthetic dynamics.** The order book is generated by a parametric model, not drawn from a real venue. Any effect is a property of that model, never a market fact.
- **Real-data validation remains open.** Licensed data, vendor decoding and a real-session study entry point are not included in this Python synthetic core.
- **FIFO assumption.** Queue position and fills use a strict price-time (FIFO) priority model with a frozen ambiguity rule; real venues may differ (pro-rata, hidden liquidity, self-match prevention).
- **No real fills.** All executions are virtual; there is no real fill, slippage, or exchange acknowledgement latency beyond the modelled constant latency scenarios.
- **Small session counts.** The study uses few synthetic sessions, so paired intervals are wide and some effects are undefined or not estimable; these are reported as such, never as zero.

## Runtime and resources

Machine-specific runtime and hardware metadata are omitted from this public example. The full local report contains the measured resource table.

## Reproducibility

Exact commands (PowerShell; data is SYNTHETIC, zero cost, offline):

```powershell
# From the repository root after uv sync --locked.
# The output directory must be new or empty.
uv run --locked --offline qexec experiment run --out out/synthetic-demo-reproduction --config examples/synthetic-demo/config.json
uv run --locked --offline qexec analysis report --study out/synthetic-demo-reproduction
```

Frozen study configuration:

```json
{
  "base_seed": 0,
  "bootstrap_n": 200,
  "bootstrap_seed": 0,
  "duration_s": 120,
  "epsilon": 0.001,
  "epsilon_sensitivity": [
    0.0,
    0.0001,
    0.001,
    0.01,
    1.0
  ],
  "horizon_ladder_ns": [
    1000000000,
    5000000000
  ],
  "max_workers": 2,
  "n_pilot_sessions": 1,
  "n_sessions": 4,
  "quick": true,
  "scenarios": [
    "L1",
    "L6"
  ],
  "split_fractions": [
    0.5,
    0.2,
    0.3
  ],
  "study_id": "demo",
  "support_margin_frac": 0.5,
  "support_min_eligible": 5,
  "support_min_queue_depletion": 1
}
```

The fresh reproduction stores its freeze manifest in `out/synthetic-demo-reproduction/freeze/manifest.json`. Original run timestamps and local Git identity metadata are omitted from this example. The following model hashes are copied from the original local manifest.

- model artifact SHA-256 hashes:
  - `action_b3.json`: `01444743d3f6d2c8af228898323d16991841dd685d7f03ba4d4f3b33f58fe6e9`
  - `action_b3.npz`: `8739c76e681f900923b900c9df0ef75cf421d39cabb54650c4b9ad19b6a76d85`
  - `action_b3_no_queue.json`: `82ab1c0916c9a21ff5bc73639ebeaa637a57fa020164195c28aee9c93db17f41`
  - `action_b3_no_queue.npz`: `8739c76e681f900923b900c9df0ef75cf421d39cabb54650c4b9ad19b6a76d85`
  - `price_primary.json`: `8e25ce511ab28b34e5452740f0d768ce8a438b43ca9ec940402216281f59359e`
  - `price_primary.npz`: `faed724d780df5900e185ee8fdd499c1dd492afcfff26bb1acfea607c7572e38`
  - `price_secondary.json`: `4b6cdd8accfa41930966f5019efebbf38d54860074624c1ce4a4650e31eee14c`
  - `price_secondary.npz`: `f8f78f09f4a016777d08581bfd2b4cd7fe3ea3a00599dce201f554edfd991227`
  - `support_b3.json`: `967be5667c964e3f9935466c5a852975d8d169b52e9dd937e55caf0688a14b85`
  - `support_b3_no_queue.json`: `baaf721feadda7fb721222e920bc3521470245377f6ff1e3ef2783fe73866dc7`

Test-time models are reloaded from the frozen artifacts on disk (never refit), proving the artifacts suffice to reproduce the evaluation.

