# Test traceability (T01–T60)

This maps every test ID in `03-implementation-plan.md` section 12 (the validation matrix) to the
test file(s) and function(s) that cover it. It is produced by searching the `tests/` tree for
each ID and for the described behaviour. **It is honest about gaps:** IDs with no implemented
coverage are marked `NOT COVERED` with the reason. All tests run on SYNTHETIC data only.

Legend: ✅ covered · ⚠️ partial / adjacent · ❌ NOT COVERED.

| ID | Case (abbreviated) | Status | Covering test(s) |
|---|---|---|---|
| T01 | Add/cancel/modify/reset visible state & priority | ✅ | `tests/unit/reference/test_book.py::test_add_cancel_modify_reset_visible_state_and_priority`, `::test_clear_then_snapshot_reinit` |
| T02 | Equal timestamps, different source order retained | ✅ | `tests/unit/adapters/test_batches.py::test_equal_timestamps_retain_source_order` |
| T03 | Non-mutating final boundary record commits batch | ✅ | `tests/unit/adapters/test_batches.py::test_non_mutating_final_boundary_record_closes_batch` |
| T04 | Snapshot initialization, no false order-flow | ✅ | `tests/unit/reference/test_book.py::test_snapshot_initialization_no_order_flow_features`; `tests/unit/adapters/test_batches.py::test_batch_kind_initialization_all_snapshot`, `::test_batch_kind_initialization_clear_then_snapshot_adds`; `tests/unit/features/test_market.py::test_initialization_batch_updates_no_flow` |
| T05 | Missing records / invalid state diagnosed | ✅ | `tests/unit/adapters/test_diagnostics.py::test_unknown_cancel_is_diagnosed_not_silently_applied`, `::test_impossible_reduction_is_diagnosed_and_capped` |
| T06 | Trade/Fill + reduction changes book once | ✅ | `tests/unit/sim/test_overlay_queue.py::test_five_ahead_execute_three_leaves_two_ahead_unfilled`; `tests/unit/sim/test_overlay_mechanisms.py::test_sell_side_queue_depletion_counts_reduction_once`; `tests/unit/reference/test_book.py::test_trade_fill_cancel_triplet_mutates_once` |
| T07 | Five ahead, execute three → two ahead, unfilled | ✅ | `tests/unit/sim/test_overlay_queue.py::test_five_ahead_execute_three_leaves_two_ahead_unfilled` |
| T08 | Exact exhaustion of queue ahead → no fill | ✅ | `tests/unit/sim/test_overlay_queue.py::test_exact_exhaustion_then_behind_fill_depletes_queue` |
| T09 | Cancellation ahead/behind → no fabricated fill | ✅ | `tests/unit/sim/test_overlay_queue.py::test_cancellation_ahead_reduces_queue_behind_does_not` |
| T10 | Quote touches limit → no fill from touch | ✅ | `tests/unit/sim/test_overlay_queue.py::test_quote_touch_does_not_fill` |
| T11 | Entry delay / same-time event → no clairvoyance | ✅ | `tests/unit/sim/test_engine_mechanisms.py::test_T11_no_fill_before_command_arrival`; `tests/unit/sim/test_engine_timing.py::test_join_command_arrival_time_t0_plus_300us` |
| T12 | Marketable-on-arrival limit classification | ✅ | `tests/unit/sim/test_overlay_passive.py::test_marketable_buy_fills_at_opposite_best`, `::test_marketable_buy_limit_above_ask_fills_at_ask`, `::test_marketable_sell_fills_at_opposite_best` |
| T13 | Delayed fill report, policy unaware until delivery | ✅ | `tests/unit/sim/test_engine_timing.py::test_fill_report_delivered_250us_after_exchange_time` |
| T14 | Fill during cancellation → no duplicate residual | ✅ | `tests/unit/sim/test_engine_mechanisms.py::test_T14_fill_during_cancel_no_duplicate_residual`; `tests/unit/sim/test_oms.py::test_fill_during_cancel_no_double_exposure`; `tests/unit/sim/test_controller.py::test_enter_working_cancels_then_fill_during_cancel_no_aggressive` |
| T15 | Duplicate/late reports idempotent; no resurrection | ✅ | `tests/unit/sim/test_oms.py::test_duplicate_report_id_ignored`, `::test_late_accepted_cannot_resurrect_terminal`, `::test_duplicate_execution_id_counted_once` |
| T16 | Every pending state at cutoff handled | ✅ | `tests/unit/sim/test_controller.py` (full suite; see `::test_at_most_one_aggressive_attempt_ever`, `::test_enter_in_flight_waits_then_accepted_cancels_then_cancelled_aggressive`) |
| T17 | Halt / missing depth → no synthetic fill | ✅ | `tests/unit/sim/test_overlay_passive.py::test_passive_rejected_when_not_trading`; `tests/unit/sim/test_engine_mechanisms.py::test_halt_in_gap_no_fabricated_aggressive_fill` |
| T18 | Fill on time, report late → judged by execution time | ✅ | `tests/unit/sim/test_engine_mechanisms.py::test_T18_on_time_fill_late_report_counts` |
| T19 | Buy/sell shortfall, fees, half ticks | ✅ | `tests/unit/analysis/test_metrics.py::test_t19_product_example_buy`, `::test_t19_product_example_sell`, `::test_t19_half_tick_midpoint`, `::test_t19_fee_sign_rebate` |
| T20 | Python/C++ replay parity | ❌ NOT COVERED | The optional native C++ extension (R11) is not built in this synthetic-data build; there is no C++ replay to compare. |
| T21 | Binding lifetimes/overflow, no sanitizer failures | ❌ NOT COVERED | No native extension built; nothing to sanitize. |
| T22 | Future input perturbation leaves earlier features unchanged | ✅ | `tests/unit/features/test_market.py::test_t22_future_perturbation_leaves_earlier_features_unchanged` |
| T23 | Outcome window crosses split → purged/handled | ✅ | `tests/integration/test_study_pipeline.py::test_sessions_whole_never_cross_split_boundary` |
| T24 | Cancelled historical order not mislabelled | ⚠️ partial | Covered indirectly by the overlay cancellation/queue tests (`tests/unit/sim/test_overlay_queue.py::test_cancellation_ahead_reduces_queue_behind_does_not`) and branch-label generation (`tests/unit/sim/test_engine_probe.py::test_T57_branch_value_equals_recomputation`). No dedicated test is named T24. |
| T25 | Oracle queue feature attempt rejected | ✅ | `tests/unit/features/test_groups.py::test_t25_oracle_and_true_queue_and_unreported_columns_rejected`, `::test_t25_prohibited_takes_precedence_over_allowlist_message` |
| T26 | Calibration/threshold on permitted dev data only | ✅ | `tests/integration/test_study_pipeline.py::test_theta_chosen_only_from_validation`; `tests/unit/models/test_decision_select_theta.py` |
| T27 | Toy P1 matches enumeration | ❌ NOT COVERED | The optional P1 optimal-stopping policy (R12) is not built in this synthetic-data build. |
| T28 | Fixed reference + delayed report cost reconciliation | ✅ | `tests/unit/analysis/test_metrics.py::test_t28_offset_identity` |
| T29 | Terminal residual mark creates no fill/completion | ⚠️ partial | Residual valuation is covered by `tests/unit/analysis/test_metrics.py::test_c_t_no_fill_residual_valuation_buy`/`_sell` and the controller's at-most-one-attempt tests; no test is explicitly named T29. |
| T30 | P1 constraint and K=1 reduction to B3 | ❌ NOT COVERED | P1 extension (R12) not built. |
| T31 | Unsupported P1 state records fallback | ❌ NOT COVERED | P1 extension (R12) not built. (Note: the B3 unsupported-fallback analogue is covered by `tests/unit/models/test_decision_risk_allowance.py::test_unsupported_falls_back_to_switch`.) |
| T32 | No common-completion tasks → undefined, counted | ✅ | `tests/unit/analysis/test_stats.py::test_t32_no_common_completion_is_undefined_and_counted`; report presentation `tests/unit/analysis/test_report.py::test_report_renders_undefined_cost_effect_with_degeneracy_flag` |
| T33 | Reproduction / clean environment, traceable CLI outputs | ✅ | `tests/integration/test_cli.py::test_demo_quick_end_to_end`, `::test_analysis_report_on_quick_study`, `::test_synth_is_deterministic`; and the `scripts/ci.ps1` FULL-mode smoke stage (`qexec demo --out <tmp> --sessions 2 --quick`). |
| T34 | Size/price modification priority loss/retention | ✅ | `tests/unit/reference/test_book.py::test_modify_price_change_loses_priority`, `::test_modify_size_increase_loses_priority_decrease_retains` |
| T35 | Duplicate records / overlapping files diagnosed | ✅ | `tests/unit/adapters/test_diagnostics.py::test_detect_duplicate_by_ordinal`, `::test_detect_duplicate_by_content_and_offset`, `::test_duplicate_detection_prevents_double_mutation`, `::test_detect_overlapping_sessions` |
| T36 | Refresh/hidden/implied ambiguity treatment | ✅ | `tests/unit/reference/test_book.py::test_modify_unknown_order_is_anomaly_not_add`; `tests/unit/sim/test_overlay_queue.py::test_behind_fill_while_ahead_positive_is_ambiguous_no_fill` |
| T37 | Reset/snapshot during hypothetical task | ✅ | `tests/unit/sim/test_overlay_commands.py::test_clear_record_while_working_is_technical_reset`, `::test_initialization_snapshot_batch_while_working_is_technical_reset`; `tests/unit/sim/test_engine_mechanisms.py::test_T37_technical_reset_on_clear_while_working` |
| T38 | No market event at checkpoint/cutoff; timers still fire | ✅ | `tests/unit/sim/test_engine_timing.py::test_timers_fire_with_no_market_events_T38` |
| T39 | Guard from every reachable pending state | ✅ | `tests/unit/sim/test_controller.py` (reachable-state suite, e.g. `::test_enter_from_flat_sends_aggressive`, `::test_in_flight_rejected_sends_aggressive`) |
| T40 | Aggressive rejection / no liquidity → no retry | ✅ | `tests/unit/sim/test_controller.py::test_at_most_one_aggressive_attempt_ever`; `tests/unit/sim/test_oms.py::test_aggressive_from_flat_reserves_then_unfilled` |
| T41 | Cloned HOLD/SWITCH checkpoint identical pre-fork | ✅ | `tests/unit/sim/test_engine_probe.py::test_T41_clone_identical_before_fork`, `::test_T41_branches_independent`, `::test_T41_two_clones_identical_at_fork`; `tests/unit/sim/test_overlay_mechanisms.py::test_copy_is_independent` |
| T42 | Failed/late probe arm: miss retained, deadline-only value | ✅ | `tests/unit/sim/test_remediation_engine_r1.py::test_R1_T42_failed_hold_branch_misses` (a HOLD branch that never fills keeps miss=1), `::test_R1_T42_late_fill_counts_as_miss_excluded_from_value` (a fill after T is a miss and is excluded from C_T); fork mechanics `tests/unit/sim/test_engine_probe.py::test_T42_engine_forks_probe_at_eligible_checkpoint` |
| T43 | Unchanged price label in explicit class 0 | ✅ | `tests/unit/labels/test_price.py::test_t43_unchanged_mid_is_class_zero_not_none`; `tests/integration/test_study_pipeline.py::test_primary_development_sample_is_labelled` |
| T44 | Stacked price-score provenance (no in-sample/future) | ✅ | `tests/unit/models/test_stacking.py::test_fit_only_sees_strictly_earlier_sessions`, `::test_purge_gap_excludes_recent_sessions`; `tests/integration/test_study_pipeline.py::test_oof_provenance_is_strictly_prior` |
| T45 | No-queue feature allowlist | ✅ | `tests/unit/features/test_groups.py::test_t45_b3_no_queue_excludes_exactly_the_queue_group`, `::test_t45_price_allowlist_has_no_queue_or_mechanics_features`, `::test_t45_queue_features_pass_for_b3_but_fail_for_b3_no_queue`; `tests/integration/test_study_pipeline.py::test_action_schemas_differ_exactly_by_queue` |
| T46 | B2 sign, threshold equality, single decision | ✅ | `tests/unit/policies/test_model_policies.py::test_b2_buy_switches_when_signal_exceeds_theta`, `::test_b2_sell_switches_on_negative_signal`, `::test_b2_equality_holds` |
| T47 | B3 risk-allowance rule, tie, missing support, eps bounds | ✅ | `tests/unit/models/test_decision_risk_allowance.py` (full suite incl. `::test_epsilon_zero_is_strict_risk_first`, `::test_epsilon_one_is_pure_cost`); `tests/unit/policies/test_model_policies.py::test_b3_end_to_end_holds_when_hold_is_safe_and_cheap`, `::test_b3_switches_when_hold_misses_more` |
| T48 | Pending/unsupported client checkpoint → safe B1 | ✅ | `tests/unit/sim/test_engine_mechanisms.py::test_T48_ineligible_checkpoint_b1_continuation_no_fill_leak`; `tests/unit/policies/test_baselines.py::test_eligibility_pending_command`, `::test_eligibility_unreported_fill_does_not_make_pending_eligible` |
| T49 | Optional policy added → primary estimate unchanged | ✅ | `tests/unit/analysis/test_stats.py::test_t49_third_policy_does_not_change_estimate`; `tests/unit/analysis/test_tables.py::test_primary_pair_estimate_unchanged_by_adding_policies` |
| T50 | Zero observed misses → no zero-risk claim | ✅ | `tests/unit/analysis/test_stats.py::test_t50_all_zero_misses_no_degenerate_interval`, `::test_t50_nonzero_spread_still_gives_interval`, `::test_zero_event_bound_values`; report presentation `tests/unit/analysis/test_report.py::test_report_renders_zero_event_upper_bound_not_as_zero_risk` |
| T51 | UTC midnight / DST boundary session assignment | ❌ NOT COVERED | Timezone/session-boundary assignment is a real-data (G0) concern; the synthetic feed uses an explicit integer-ns epoch with no DST, so there is no DST path to exercise. Blocked pending real data. |
| T52 | Reordered/missing model feature columns → hard error | ✅ | `tests/unit/models/test_schema.py::test_schema_rejects_missing_column`, `::test_schema_rejects_extra_column`, `::test_schema_rejects_reordered_columns`; `tests/unit/policies/test_model_policies.py::test_b3_schema_mismatch_is_run_error_not_fallback` |
| T53 | Early aggressive rejection then terminal timer | ✅ | `tests/unit/sim/test_controller.py::test_at_most_one_aggressive_attempt_ever`, `::test_in_flight_rejected_sends_aggressive` |
| T54 | Client queue proxy vs simulator truth | ✅ | `tests/unit/features/test_queue_proxy.py::test_t54_undelivered_prior_add_is_uncertain_not_ahead`, `::test_uncertain_add_then_cancelled_reduces_upper_band` |
| T55 | Virtual best remains after level disappears (trade-through) | ✅ | `tests/unit/sim/test_overlay_trade_through.py::test_trade_through_fills_at_own_limit_after_level_disappears`, `::test_quote_move_alone_does_not_fill`, `::test_trade_through_sell_side_mirror` |
| T56 | Two commits before delayed observation delivery | ✅ | `tests/unit/sim/test_engine_mechanisms.py::test_T56_two_commits_before_first_delivery_client_sees_earlier_first` |
| T57 | B3 labels vs P1 forced continuation reconciliation | ⚠️ partial | The P1 optimal-stopping policy (R12) is **not built** in this synthetic build, so there is no P1 forced-continuation value to reconcile against. The achievable half — B3 branch-label values reconcile with an independent recomputation — is covered by `tests/unit/sim/test_engine_probe.py::test_T57_branch_value_equals_recomputation`. The P1 side is honestly uncovered. |
| T58 | Fill-mechanism classification | ✅ | `tests/unit/sim/test_overlay_mechanisms.py::test_mechanism_queue_depletion`, `::test_mechanism_trade_through`, `::test_mechanism_aggressive`, `::test_mechanism_ambiguous_logged_not_filled` |
| T59 | G2-S selection reads support counts only | ✅ | `tests/unit/analysis/test_support.py::test_shortest_passing_horizon_is_chosen`, `::test_selection_ignores_cost_and_markout_columns` |
| T60 | Shared risk-allowance function across B2/B3/B3_NO_QUEUE | ✅ | `tests/unit/models/test_decision_shared_rule.py::test_select_theta_matches_risk_allowance_on_two_candidates`; `tests/unit/policies/test_model_policies.py::test_b3_switches_when_hold_misses_more` (routes through `risk_allowance_choice`) |

## Summary

- **Covered (✅):** T01–T19, T22, T23, T25, T26, T28, T32–T50 (except as noted),
  T52–T56, T58–T60 — the full synthetic-build gate chain G1–G7.
- **Partial / adjacent (⚠️):** T24, T29 (described behaviour exercised by related tests but no
  test carries the exact ID); T57 (P1 is not built, so only the B3-label-vs-recomputation half
  reconciles — the P1 forced-continuation side is honestly uncovered).
- **NOT COVERED (❌):** T20, T21 (native C++ extension R11 not built); T27, T30, T31 (optional P1
  extension R12 not built); T51 (UTC/DST session assignment is a real-data G0 concern, blocked
  pending paid data).

These gaps are intentional for the zero-cost synthetic-data build: the native extension and the
P1 optimal-stopping policy are optional work packages, and the real-data gate is blocked pending a
licensed market-data subscription. No coverage is claimed that cannot be pointed to above.

## External-review remediation (docs/remediation.md R1–R21)

The table maps the original review to its regression coverage. Later independent reviews found
additional gaps; those fixes and regressions are listed below. Passing tests validate the
specified fixtures and do not establish correctness for every possible feed or market mechanism.

| Item | Regression tests |
|---|---|
| R1 fork inherits pre-fork fills and pending events | `tests/unit/sim/test_remediation_engine_r1.py` |
| R2 atomic-batch effective time; no past scheduling | `tests/unit/sim/test_remediation_engine_r2.py` |
| R3 client status only via delayed observation | `tests/unit/sim/test_remediation_engine_r3.py` |
| R4, R8, R10, R11 (engine) branch quality flags, persisted eligibility, last-valid `m_T`, scenario identity | `tests/unit/sim/test_remediation_engine_schema.py`; `tests/unit/labels/test_last_valid_mid.py`; `tests/unit/sim/test_frame_schema.py` |
| R5 invalid-state intervals until trusted recovery | `tests/unit/sim/test_remediation_data_r5_quality.py` |
| R6 absent price class has probability 0 | `tests/unit/models/test_remediation_data_r6.py` |
| R7 rejected passive still gets the cutoff aggressive attempt | `tests/unit/sim/test_remediation_engine_r7.py` |
| R9 merged status/book timeline; labels None beyond coverage or in halts | `tests/unit/sim/test_remediation_data_r9_manifest.py`, `tests/unit/labels/test_remediation_data_r9_labels.py` |
| R10 (study) miss training keeps null-cost rows | `tests/unit/experiments/test_remediation_study_miss_training.py` |
| R11 (study) scenario-keyed outputs; L1-only primary diagnostics | `tests/unit/experiments/test_remediation_study_scenario_identity.py`, `test_remediation_study_scenario_columns.py`; `tests/integration/test_study_pipeline.py` (every results parquet) |
| R12 session provenance | `tests/unit/experiments/test_remediation_study_provenance.py` |
| R13, R14 run-directory protocol, write-once freeze, append-only unblinding | `tests/unit/experiments/test_remediation_study_run_protocol.py` |
| R15 complete checksum manifest; validate fails on quality flags | `tests/unit/test_remediation_study_checksums.py`, `tests/integration/test_cli_remediation.py` |
| R16 diagnostics use the deployed B3 rule; calibration metrics | `tests/unit/experiments/test_remediation_study_diagnostics.py` |
| R17 OMS reconciles terminal cancel cumulative execution | `tests/unit/sim/test_remediation_engine_r17.py` |
| R18 `ExperimentConfig` round trip | `tests/unit/test_remediation_study_config_roundtrip.py` |
| R19 saved config reloads verbatim; decision logs; causal trace; frozen B3 trace | `tests/unit/experiments/test_remediation_study_config_verbatim.py` |
| R20 support built from the exact action-training matrix; honest degeneracy | `tests/unit/experiments/test_remediation_study_support_matrix.py`; `tests/integration/test_study_pipeline.py::test_quick_study_degeneracy_reported_honestly_not_hidden` |
| R21 docs wording; tests cannot open network sockets | `tests/unit/test_remediation_study_docs.py`, `tests/unit/test_remediation_study_offline.py` |

## Completion review regressions

| Area | Regression evidence |
|---|---|
| Source/checksum/metadata/config boundaries | `tests/unit/test_final_source_integrity.py` |
| Captured status, common resets, execution attribution and price priority, evaluable support | `tests/unit/sim/test_execution_reliability.py` |
| Complete replacement snapshots | `tests/unit/reference/test_snapshot_replacement.py`; `tests/unit/sim/test_snapshot_integration.py` |
| Common scenario task population | `tests/unit/sim/test_common_scenario_population.py` |
| Eligible fits, chronological refits, both-action calibration and training-derived references | `tests/unit/experiments/test_research_integrity.py` |
| Missing validation costs cannot erase misses | `tests/unit/experiments/test_final_validation_costs.py` |
| Per-action minimum support and atomic refits | `tests/unit/experiments/test_final_model_support.py` |
| Original discrete price labels and finite features | `tests/unit/models/test_price_model.py` |
| Paired-row integrity and missing-cost denominators | `tests/unit/analysis/test_final_pair_integrity.py` |
| Hand-derived markouts and missing-reference coverage | `tests/unit/analysis/test_markout_diagnostics.py` |
| Immutable runs, verified report/trace inputs and protected access history | `tests/unit/experiments/test_release_integrity.py` |
| Persisted audit frames and markout summaries | `tests/integration/test_study_pipeline.py::test_complete_audit_frames_and_markouts_are_saved` |
