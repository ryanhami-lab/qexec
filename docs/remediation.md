# External review remediation (binding)

Source: independent external review, 2026-10-01. Every item below was confirmed by the manager by
reading the cited code. Each fix MUST begin with a failing reproduction test that encodes the
reviewer's scenario (commit-free: show it failing in your final message), then the fix, then the
passing test. Never weaken an existing assertion to make it pass; if an existing test encodes the
buggy behavior, change it and explain why in the commit message.

Package A = engine plane, B = data/labels/models plane, C = study/release plane.

| ID | Pkg | Defect | Required fix |
|---|---|---|---|
| R1 | A | Forked HOLD/SWITCH branches lose pre-fork executions (ledger filtered by branch `world_id`) and pending report deliveries keyed to the parent. | Branches inherit the parent's execution-ledger entries and every pending scheduled event for the parent (report deliveries, command arrivals), duplicated per branch. Test: fill 100 us before checkpoint (unreported at checkpoint, L1) -> both branches completed, miss=0, C_T equal to standalone B1. |
| R2 | A | Atomic batch: overlay fills use record `event_time_ns` while the batch is processed at max proxy time, so a fill can predate the command arrival and reports are scheduled into the past. | Overlay execution effective time for record-driven fills = the batch's `exchange_proxy_time` (atomic convention; document). Scheduler asserts no event is scheduled before the current time. Test: batch with record times +200us/+500us, command arrival +300us -> fill time +500us >= arrival. |
| R3 | A | Client book status taken from the exchange-plane status attached to market deliveries (leaks undelivered halts). | Client status changes ONLY via delayed status-observation events. Market deliveries never set client status. Test: halt effective +100ms, observed +800ms, book batch delivered +200ms -> view at +500ms shows TRADING. |
| R4 | A | Branch labels ignore the policy-independent quality map. | Branch rows carry `technically_unevaluable` + reason from the same quality map; C excludes them from training (counted). Test: unknown-order cancel after checkpoint -> B1 unevaluable and both branch rows flagged. |
| R5 | B | Quality map records corruption instants, not invalid-state intervals; a task after an unrecovered corruption is evaluable. | Invalid interval from a corrupting event until a trusted recovery (an INITIALIZATION snapshot batch after the corruption, with no new anomaly). A task is unevaluable if its window `[arrival, deadline]` overlaps any invalid interval (an unrecovered interval extends to session end). Update docs/engine_spec.md section 3 to match architecture section 5. Test: unknown cancel 100 ms before arrival, no recovery -> unevaluable; with a later snapshot recovery before arrival -> evaluable. Keep `is_unevaluable(...)` API. |
| R6 | B | PriceModel: an absent class gets the reference-class logit (binary DOWN/UNCH fit predicts UP ~= DOWN). | Absent classes get probability exactly 0 (explicit convention: absent-class logit = -inf, never ties a fitted class); the probabilities of fitted classes equal sklearn's predict_proba exactly. Document. Tests: 90 DOWN/10 UNCH -> p_up == 0 and p_down, p_unch match sklearn; single-class case unchanged semantics (p=1 for present class). Artifact round trip must still be exact (encode -inf safely in JSON). |
| R7 | A | Cutoff skips the controller for any terminal OMS state, including a rejected passive order. | Cutoff enters the controller unless the task is resolved: FILLED, aggressive attempt already used, or TECHNICAL. A REJECTED passive (aggressive unused) sends the single aggressive attempt. Test: passive rejected during halt, liquidity returns before cutoff -> aggressive at cutoff, completes. |
| R8 | A | `checkpoint_eligible` in task results reconstructed from optional B1 feature capture. | Persist eligibility for every world at its checkpoint independently of feature recording. Test: B3-style stub policy with `record_checkpoint_features=False` -> MODEL_CHOICE rows marked eligible. |
| R9 | B | Manifest eligibility and MidSeries apply status only when a book batch arrives (quiet-gap status ignored); labels beyond coverage or inside halts return UNCHANGED. | Both use a merged status/book timeline: a status event creates its own sample (mid None while not TRADING). `price_direction_label` returns None if `t*+tau` is beyond the last sample time / session end, or either endpoint state is invalid. Manifest client-mid check honors status observations at their delayed times per scenario. Tests for each case. |
| R10 | A | Deadline valuation uses `mid_at` (state may be invalid) instead of last valid mid; null-cost rows then dropped from miss training. | Use `MidSeries.last_valid_mid_at(T)` (already on main) for `m_T` in task results and branch labels; persist `m_t_age_ns`. C: never drop rows from miss training because cost is null; cost model trains on non-null cost rows only, counted. |
| R11 | A+C | Decisions/executions/reports lack scenario identity; primary diagnostics pool scenarios; decision keys collide. | A: every SessionOutputs frame has `scenario_id` and `horizon_ns`. C: concatenation preserves them (add if absent), all keys include scenario_id, primary diagnostics explicitly select primary horizon + L1. Test: appending L6 decisions does not change primary disagreement. |
| R12 | C | Existing sessions reused when checksums pass regardless of seed/duration/generator. | Write `sessions/provenance.json` (generator params, base_seed, duration, n_sessions, generator version hash); reuse only on exact match, else hard error with a clear message. Test seed/duration change -> error. |
| R13 | C | Failed reruns leave stale results; report renders them; CLI exits 0 "Done" after support failure. | Every invocation requires a fresh output directory, including identical retries. Preserve old history; write lifecycle status and a COMPLETE artifact seal. Reports require verified COMPLETE. Support failure exits nonzero. |
| R14 | C | Freeze manifest and first-unblinding log overwritten on rerun. | Freeze dir is write-once (refuse overwrite); unblinding log append-only with every access; first access preserved. Tests. |
| R15 | C | Empty checksums.json passes; replay validate reports OK on TRUNCATED data. | `verify_session` requires checksums for every required session file; replay validate fails (exit 1) on any batch quality flag. Tests. |
| R16 | C | Validation diagnostics use superseded strict lexicographic rule; calibration/error diagnostics missing. | Diagnostics call `risk_allowance_choice` with config epsilon and the frozen SupportRule exactly like B3Policy; report miss Brier/log loss vs base rate, cost RMSE vs constant mean, per action. Test. |
| R17 | A | OMS ignores `cumulative_executed` on CANCEL_REJECTED_TERMINAL. | Reconcile reported cumulative execution from terminal cancel responses (no aggressive intent when cumulative = 1; cost still counted once from the exchange ledger). Test. |
| R18 | C | `ExperimentConfig.from_dict(to_dict())` raises (guard_ns). | Round trip works; derived keys accepted only if consistent. Test. |
| R19 | C | Report omits causal trace; B3 prediction logs discarded; trace CLI cannot show frozen B3; "exact commands" omit custom settings. | Persist B3/B3_NO_QUEUE decision logs (p/v/support/choice/reason, scenario) to results; report includes one causal trace from a test task (stored in report_inputs); `qexec trace task --study DIR ...` loads frozen artifacts and traces B2/B3; reproduction section uses `qexec experiment run --config <saved config>` which must load the saved config verbatim. Tests. |
| R20 | C | Support region built from final-model price scores while action models train on OOF scores. | Decide with evidence. Default: the support region is built from the exact matrix the action models were fit on. If that makes B3 mostly fallback, report it honestly (degeneracy flag) rather than redefining support; any alternative must be justified in docs/research_spec.md with a diagnostic table. |
| R21 | C | Docs overclaim: README "Validated" gates; T42/T57 coverage; real-data path implies a CLI input that does not exist; QEXEC_OFFLINE implies uv is offline. | README gate table: "Implemented and tested on synthetic data" wording, plus link to this file. Traceability rewritten from actual tests after A/B land. reproducing.md: state that a real-data adapter (vendor decoding into CanonicalRecord sessions) is not implemented. ci.ps1: remove the misleading flag; add `tests/conftest.py` that blocks socket connections during tests and a test proving it; document that `uv sync` downloads free packages from PyPI on first install. |

## Python-core completion review

Scope confirmed by the user: Python core and known fixes. Optional C++ and P1 remain outside
this release. Licensed-market feasibility, vendor decoding, a real-session study entry point
and held-out market results remain additional work. A synthetic support verdict is a software
test and does not establish that the proposed market experiment will work.

Independent reviewers first reproduced defects, then cross-reviewed integrated changes outside
their original implementation areas. The review resulted in these additional repairs:

- Client eligibility uses actual captured book/status delivery order. Every stage shares the
  declared scenario population; snapshots initialize references and replace stale resting state.
- Technical exclusions cover every policy across corruption/reset windows and remove invalid
  tasks from pilot support counts. Passive fills require positive, reconciled economic groups,
  sufficient actual resting quantity and historical price priority.
- Price fitting, tuning and diagnostics exclude technical rows. Final models refit all eligible
  training rows; saved provenance names actual fits. Both action arms receive calibrated
  diagnostics against training-derived references, with separate miss/cost denominators.
- Model refits are atomic; original discrete labels and finite matrices are validated. Support
  checks require adequate action-specific miss and cost populations as well as the union ranges.
  Undefined evaluable validation costs stop threshold selection before freeze.
- Paired analysis rejects duplicate/mismatched task rows. Missing costs remain undefined and
  counted without erasing miss evidence. Descriptive markouts retain missing-reference reasons.
- Task manifests, report deliveries, quality verdicts, markouts and runtime metadata are persisted.
  Fresh runs preserve history; report/trace inputs require matching completion seals. The original
  access-log prefix is protected, while later accesses append. Frozen traces locate the requested
  test session and scenario; reproduction commands load the exact saved configuration.
- Offline CI explicitly enforces cached dependency use. Smoke cleanup verifies its resolved
  temporary directory before recursive deletion.

Regression locations are mapped in [test_traceability.md](test_traceability.md). Coverage remains
limited to the normalized synthetic protocol and disclosed assumptions. No claim of perfection,
market profitability, native acceleration or completed market validation is made.

### Final validation (2026-10-03)

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ci.ps1 -Offline` passed:

- Lock consistency and cached dependency installation.
- Formatting across 159 files, Ruff lint, and strict typing across 65 source files.
- All **608 tests** passed in **281.75 seconds**; no skipped or xfailed cases.
- Synthetic CLI study and report smoke passed in **24.7 seconds**.
- Final independent verification passed in all three review areas, including a separately
  executed fresh study, recovered-snapshot outcomes, common scenarios and protected log access.

The earlier fast suite passed 585 tests with the 23 slow tests intentionally deselected; all
23 were included in the full validation above. Tests and resource measurements characterize
the synthetic implementation, not a real venue or the statistical power of a market study.

### Publication review (2026-10-03)

Independent publication review found report-presentation defects after the core review:
the saved fallback/degeneracy verdict was not shown beside a numeric paired effect, and
literal pipes in command IDs broke GitHub Markdown trace tables. The report now displays
the saved verdict, reason, eligible counts, model-use/support fractions, fallback counts
and action disagreement denominator. Missing diagnostics remain explicitly unavailable.
Table cells escape pipes, backslashes and line breaks. The limitation text now states that
omitted market impact can understate costs without assigning a guaranteed overall bias sign.

All four new hand-derived regressions in
`tests/unit/analysis/test_publication_report.py` failed before the fixes and passed after them.
The README links a sanitized synthetic report and its exact configuration; historical plans
link the current release scope. The example prominently discloses that both B3 variants used
fallback decisions at all 21 eligible primary checkpoints. It is not a full sealed study bundle.

The publication audit scanned all reachable source blobs for credential patterns and private
paths. Existing local Git history carries personal email metadata, so publication uses a clean
snapshot with GitHub noreply author metadata and only the intended main branch. Local agent
context, generated runs, credentials and original private history are excluded from the upload.
