# Reproducing QExec results

The study calculations in this build are **deterministic, synthetic, offline, and free**. This document gives
the exact steps to reproduce a study and its report, how to verify reproducibility, and — as
**instructions only** — the licensed real-data path that remains blocked pending paid data.

## 1. Deterministic synthetic reproduction

```powershell
# Locked environment from the local cache. First-time setup may need uv sync --locked
# to download the free pinned dependencies; --offline fails if anything is uncached.
uv sync --locked --offline

# Run the study. The quick preset is fully specified and seeded.
uv run --locked --offline qexec experiment run --out out\repro --quick

# Render the report from the persisted artifacts.
uv run --locked --offline qexec analysis report --study out\repro

# Repeat the saved configuration into a NEW output directory.
uv run --locked --offline qexec experiment run --out out\repro2 --config out\repro\config.json
uv run --locked --offline qexec analysis report --study out\repro2
```

Determinism guarantees in this build:

- **Seeded generation.** Synthetic sessions are produced by `qexec.synthetic` from an explicit
  integer seed; the same seed reproduces byte-identical `records.parquet` (verify with
  `qexec synth` twice and comparing `checksums.json`).
- **No wall-clock / no unseeded randomness** affects computed data or statistical results.
  Status timestamps, `frozen_at`, access-log times, resource measurements, saved paths and source commit metadata can
  differ between runs; they are provenance and are not inputs to the calculations.
- **Parallel == sequential.** Test-evaluation runs are order-stable: `--workers 2` and
  `--workers 1` produce identical analysis tables (asserted by
  `tests/integration/test_study_pipeline.py::test_parallel_and_sequential_tables_identical`).

## 2. What a run writes (and how to verify it)

A study `out_dir` contains (research spec section 2, stage 10):

```text
out_dir/
  status.json                 lifecycle, config hash, completion artifact hashes
  config.json                 frozen StudyConfig (+ data_kind = SYNTHETIC)
  sessions/SYN-####/          immutable synthetic sessions with checksums.json
  stages/                     support_gate.json, development.json, validation.json
  freeze/
    manifest.json             config, primary horizon, theta selections, model hashes,
                              git commit (if available), frozen_at (UTC)
    unblinding.log            append-only test-evaluation and trace-access history
    *.json / *.npz            frozen model artifacts
  results/
    *.parquet                 tasks, task_results, decisions, executions, reports, quality,
                              markouts, branch_labels_dev, support, horizon_sensitivity
    metrics.json              headline numbers with denominators + intervals
    resources.json            measured runtime through analysis, input storage, workers/platform
    report_inputs.json        everything the report renderer consumes
    tables/*.csv              per-table CSVs (data_kind = SYNTHETIC)
  report/report.md            the Markdown research report (from `qexec analysis report`)
```

Reproducibility checks you can run yourself:

- **Freeze hashes match artifacts.** Each file listed in `freeze/manifest.json`
  `model_artifact_hashes` has a SHA-256 that matches the file on disk.
- **Models loaded from disk.** Test-time evaluation reloads the frozen artifacts (never refits);
  `StudyResult.loaded_models_from_disk` is `True` and `freeze/unblinding.log` records first test
  access.
- **Config round-trips.** `config.json` (minus `data_kind`) parses back into an identical
  `StudyConfig` (unknown keys are rejected).

The report's **Reproducibility** section embeds the exact commands, the frozen configuration JSON,
the freeze manifest path, the git commit, `frozen_at`, and the model artifact hashes.
Those commands use the actual saved location and a new sibling output directory. If that
directory already exists, choose another empty location. Completed, failed, interrupted and
unrecognized nonempty output directories are all preserved; automatic resume is not supported.
The completion-time unblinding-log prefix is protected by its byte length and SHA-256. Later
trace accesses may append; removing or changing that protected history makes the study invalid.
Runs written before the completion-seal protocol must be preserved and reproduced into a fresh
directory; report/trace access does not silently migrate them.
Reports reject missing, malformed or non-COMPLETE status and changes to sealed artifacts.
Frozen-study traces additionally validate every session's checksum manifest and data files,
use the task id's test session, preserve the frozen horizon and scenario menu, and append an
access record for every replay. Hashes detect accidental drift; they are not tamper-proof
signatures and do not protect against deliberate rewriting of the entire directory.

## 3. Pipeline parity

To reproduce the build gates exactly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ci.ps1
```

This runs lock → sync → format → lint → types → tests → smoke and prints `PIPELINE PASSED`. The
FULL-mode smoke stage is the demo command `qexec demo --out <tmp> --sessions 2 --quick`.

## 4. Licensed real-data path (INSTRUCTIONS ONLY — BLOCKED pending paid data)

> **This path is NOT runnable in this build and MUST NOT be run without explicit cost approval.**
> It requires a paid, licensed market-data subscription. The G0 real-data feasibility decision,
> any real-data audits, and the final real-data holdout are blocked. **No real-data adapter is
> implemented in this build** — there is no code that decodes a vendor feed into
> `CanonicalRecord` sessions, and no CLI flag accepts a real-data source. The steps below
> document the *intended* procedure and required future development; they make no real-data
> claim and cannot be executed today.

When (and only when) a licensed subscription and an explicit **cost approval** are in place:

1. **Cost approval first.** Obtain written approval for the specific dataset, date range, and
   estimated spend. Do not issue any metadata, cost, or data call before approval. Confirm any
   charges against the provider's own pricing/cost tools — do not estimate spend from inside this
   repository.
2. **Credentials out of the repo.** Provide the data-provider API key via an environment variable
   in your shell only; never commit it. A real-data adapter (vendor decoding into the same
   `CanonicalRecord` stream the synthetic generator emits) is **not implemented in this build**
   and would have to be written first; downstream code is designed to be unchanged once it emits
   that stream.
3. **Ingest to the same session format.** Convert the licensed feed into the immutable session
   directory format (`records.parquet` + `status.parquet` + `instrument.json` + `session.json` +
   `checksums.json`) used by `qexec.core.records_io`. Verify checksums with
   `qexec replay validate`.
4. **Re-decide G0 on real data.** The synthetic-mode research constants (tick/multiplier, support
   minima, epsilon, splits) in `docs/engineering_contract.md` section 4 are *stand-ins*. They
   must be re-decided from the real data's measured properties before any real-data study is run.
5. **Implement a real-data study entry point.** The current `qexec experiment run` always
   generates synthetic sessions and has no real-session input flag. Add and validate a separate
   ingestion/study configuration path before attempting a real-data run. Review the report's
   data-kind handling and disclosure before it can represent real-data evidence.
6. **Hold out the final test.** Keep the final real-data holdout blinded until the freeze, exactly
   as the synthetic pipeline does (`freeze/unblinding.log`).

Until all of the above are satisfied, QExec reports exactly what it is: **synthetic software
validation, zero cost, no real-data claim.**
