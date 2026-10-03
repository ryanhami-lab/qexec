# Quickstart

A guided first run of QExec. **All data is synthetic; this costs nothing and makes no network
calls.** Results are software validation, not a market finding.
Dependency installation may download free packages; use `--offline` with `uv sync` or `uv run`
after the locked environment is cached to prohibit downloads.

## 0. Prerequisites

- Windows + PowerShell (the examples use PowerShell; the engine itself is cross-platform Python).
- [uv](https://docs.astral.sh/uv/) installed. If `uv` is not found in a fresh shell:

  ```powershell
  $env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' + $env:Path
  ```

Install the locked environment from the repository root:

```powershell
uv sync --locked
```

## 1. One-command demo

The fastest way to see everything is the end-to-end demo (the same command the CI smoke stage
runs). It generates synthetic sessions, runs the full study, and writes the Markdown report:

```powershell
uv run --locked qexec demo --out out\demo --sessions 2 --quick
```

Notes:

- The `--quick` preset needs at least four sessions (two training sessions for causal OOF
  fitting, one validation and one test session). If you ask for fewer, the demo raises the count
  to the required minimum and **says so**
  in its output — it never silently changes your request.
- When it finishes, open `out\demo\report\report.md`. It leads with a prominent SYNTHETIC-DATA
  disclaimer and reports every effect with denominators and intervals (null intervals print
  `not estimable`, undefined effects print `undefined`).

## 2. Generate sessions yourself

```powershell
uv run --locked qexec synth --out out\sessions --sessions 4 --duration 120 --seed 0
```

This writes immutable session directories `SYN-0001 .. SYN-0004`, each with `records.parquet`,
`status.parquet`, `instrument.json`, `session.json`, and `checksums.json`. The same `--seed`
reproduces byte-identical sessions.

## 3. Validate a session

```powershell
uv run --locked qexec replay validate --session out\sessions\SYN-0001
```

This verifies the SHA-256 checksums and replays every batch through the authoritative
`ReferenceBook`, printing a batch/record/anomaly summary. It exits **nonzero** if any checksum
fails or any book anomaly / invariant violation is found.

## 4. Build the task manifest

```powershell
uv run --locked qexec tasks build --session out\sessions\SYN-0001 --horizon-ms 1000
```

Prints the policy-independent manifest summary: eligible BUY/SELL task counts, ineligibility
reasons, the count of technically-unevaluable windows, and the manifest id.

## 5. Run a study and render its report

```powershell
uv run --locked qexec experiment run --out out\study --quick
uv run --locked qexec analysis report --study out\study
```

`experiment run` executes the 10-stage pipeline (data → support gate → labels → price models →
action models → threshold selection → freeze → test evaluation → analysis → outputs). The report
is written to `out\study\report\report.md`. You can also point `experiment run` at a JSON config:

```powershell
uv run --locked qexec experiment run --out out\study2 --config my_study.json --sessions 6 --workers 2
```

Unknown config keys are rejected; `--sessions` / `--workers` override whatever the config/preset
sets.
Every study needs a new or empty `--out` directory. This includes retries after failure and
reruns with an identical config. Earlier freezes, results and access logs are preserved.
Reports only render a completed run whose sealed artifacts still match their recorded hashes.

## 6. Trace one world

```powershell
uv run --locked qexec trace task --session out\sessions\SYN-0001 --task-id SYN-0001:00000:BUY --policy B1
```

Prints the ordered causal event trace (arrival → decision → command → reports → checkpoint →
outcome) for a single `(task, policy)` world as a readable table. Task ids follow the
`<session>:<index>:<BUY|SELL>` convention. If an id is ineligible, the trace command prints
examples of eligible ids from that session.

To trace a model policy, use a completed study and a task id from its test split, for example:

```powershell
uv run --locked --offline qexec trace task --study out\study --task-id SYN-0004:00000:BUY --policy B3
```

Study mode loads and verifies the saved models, uses the task id's own test session, and appends
every replay to the access log. `--horizon-ms`, if supplied, must match the frozen primary
horizon; `--scenario` must belong to that study's frozen scenario menu.

## 7. Run the pipeline

Before committing, run the local pipeline (it must print `PIPELINE PASSED`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ci.ps1
```

The pre-commit hook runs the same pipeline in fast mode. See `README.md` for the stage list and
the merge gate.

---

Next: `docs/reproducing.md` for exact reproduction and the (blocked) real-data path, and
`docs/limitations.md` for how to read the synthetic results.
