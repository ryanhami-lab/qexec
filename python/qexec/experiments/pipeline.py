"""The 10-stage research study pipeline (research spec section 2).

``run_study(out_dir, cfg)`` executes the full study and writes every stage's artifacts under
``out_dir``:

1. Data: generate (or reuse) synthetic sessions; chronological split by session index.
2. G2-S support gate on pilot train sessions (support counts only, T59).
3. Development labels at the primary horizon (branch labels + price direction labels).
4. Price models: forward-chained OOF scores for honest B3 training rows (T44); final models.
5. Action models for B3 and B3_NO_QUEUE on identical train rows (schemas differ by QUEUE, T45).
6. Validation threshold selection for B2 / B2_100MS (theta from validation rows only, T26).
7. Freeze: manifest with hashes, git, frozen_at; reload models from disk for test.
8. Test evaluation across scenarios (frozen L1 models; never refit), process-pool parallel.
9. Analysis tables (primary/secondary pairs, latency, epsilon sensitivity, distributions).
10. Outputs: parquet frames, metrics.json, tables/*.csv, report_inputs.json; StudyResult.

The pipeline reuses the merged engine, models, metrics, and synthetic generator unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from qexec.analysis.diagnostics import summarize_markouts
from qexec.analysis.support import SupportGateResult, select_support_horizon
from qexec.analysis.tables import build_all_tables
from qexec.core.config import (
    LATENCY_SCENARIOS,
    PRIMARY_LATENCY_ID,
    SECONDARY_PRICE_TAU_NS,
    THETA_MENU,
    ExperimentConfig,
    checkpoint_valid,
    primary_price_tau_ns,
)
from qexec.core.records_io import read_instrument, read_meta, verify_session
from qexec.core.tasks import CheckpointChoice, DecisionReason
from qexec.experiments.config import DATA_KIND, StudyConfig
from qexec.experiments.integrity import (
    artifact_hashes,
    configuration_hash,
    seal_unblinding_log,
    sha256_file,
)
from qexec.experiments.layout import StudyLayout
from qexec.experiments.worker import (
    EvalResult,
    EvalTask,
    frame_from_ipc,
    load_support_rule,
    run_eval_task,
)
from qexec.features.groups import (
    MARKET_FEATURES,
    QUEUE_FEATURES,
    allowlist,
)
from qexec.labels.price import MidSeries, price_direction_label
from qexec.models.action import ActionModels
from qexec.models.artifacts import ModelArtifact
from qexec.models.decision import risk_allowance_choice, select_theta
from qexec.models.price import PRICE_CLASSES, PriceModel
from qexec.models.schema import FeatureSchema, SupportRule
from qexec.policies.base import Policy
from qexec.policies.baselines import B0Policy, B1Policy
from qexec.policies.model_policies import B2Policy, B3Policy, PriceSignalScorer
from qexec.sim.engine import SessionEngine
from qexec.synthetic import generate_study
from qexec.synthetic import generator as synthetic_generator
from qexec.synthetic.generator import STUDY_EPOCH_NS, SyntheticParams


def _load_price_model(path: str) -> PriceModel:
    return PriceModel.from_state(ModelArtifact.load(Path(path)).state)


def _load_action_models(path: str) -> ActionModels:
    return ActionModels.from_state(ModelArtifact.load(Path(path)).state)


__all__ = [
    "RunDirectoryError",
    "SessionProvenanceError",
    "StudyResult",
    "StudyStatus",
    "append_unblinding_access",
    "config_hash",
    "ensure_freeze_writable",
    "read_status",
    "run_study",
    "write_status",
]

_PRICE_CLASS_SIGNAL = ("p_down", "p_unch", "p_up", "u_signal")


class StudyStatus(Enum):
    """Lifecycle states recorded in ``status.json`` (R13).

    A study run transitions ``RUNNING -> COMPLETE`` on success, ``RUNNING ->
    STOPPED_SUPPORT_GATE`` when the G2-S support gate fails (an expected, non-error stop), or
    ``RUNNING -> FAILED`` when any stage raises. Report generation requires ``COMPLETE``.
    """

    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    STOPPED_SUPPORT_GATE = "STOPPED_SUPPORT_GATE"
    FAILED = "FAILED"


class RunDirectoryError(RuntimeError):
    """Raised when the run directory protocol is violated (R13/R14).

    Every run requires a new or empty output directory. Earlier runs are preserved,
    including failed runs and their audit history.
    """


def config_hash(cfg: StudyConfig) -> str:
    """Stable SHA-256 binding the saved study configuration to its artifacts."""
    return configuration_hash(cfg.to_dict())


def write_status(
    layout: StudyLayout, state: StudyStatus, cfg_hash: str, *, detail: str | None = None
) -> None:
    """Write ``status.json`` with the lifecycle state, config hash, and UTC timestamp (R13)."""
    layout.status_json.write_text(
        json.dumps(
            {
                "data_kind": DATA_KIND,
                "state": state.value,
                "config_hash": cfg_hash,
                "updated_at": datetime.now(UTC).isoformat(),
                "detail": detail,
                **(
                    {
                        "artifact_hashes": artifact_hashes(layout),
                        "unblinding_log_seal": seal_unblinding_log(layout),
                    }
                    if state == StudyStatus.COMPLETE
                    else {}
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )


def read_status(layout: StudyLayout) -> dict[str, Any] | None:
    """Read ``status.json`` if present, else ``None``."""
    if not layout.status_json.is_file():
        return None
    data: dict[str, Any] = json.loads(layout.status_json.read_text())
    return data


def append_unblinding_access(layout: StudyLayout, detail: str) -> None:
    """Append one line to the append-only unblinding log, preserving every prior access (R14)."""
    layout.freeze_dir.mkdir(parents=True, exist_ok=True)
    detail = detail.replace("\r", "\\r").replace("\n", "\\n")
    line = f"access={datetime.now(UTC).isoformat()} {detail}\n"
    with layout.unblinding_log.open("a", encoding="utf-8") as fh:
        fh.write(line)


def ensure_freeze_writable(layout: StudyLayout) -> None:
    """Refuse to overwrite a write-once freeze manifest (R14).

    Once a manifest exists, no invocation may replace it. Run a new study in a fresh
    output directory to preserve the old frozen artifacts and access history.
    """
    if layout.freeze_manifest.is_file():
        raise RunDirectoryError(
            f"freeze is write-once: {layout.freeze_manifest} already exists; refusing to "
            "overwrite the frozen artifacts. Use a fresh --out directory for a new freeze."
        )


class SessionProvenanceError(RuntimeError):
    """Raised when existing sessions do not match the current config's generator provenance.

    Reuse requires an exact match on generator params (base_seed, duration_s, n_sessions, and the
    generator source hash). A mismatch is a hard error rather than silently reusing or
    overwriting an incompatible session set (R12).
    """


def _session_provenance_payload(cfg: StudyConfig) -> dict[str, Any]:
    return {
        "data_kind": DATA_KIND,
        "generator_source_sha256": sha256_file(Path(synthetic_generator.__file__)),
        "generator_module": synthetic_generator.__name__,
        "base_seed": int(cfg.base_seed),
        "duration_s": int(cfg.duration_s),
        "n_sessions": int(cfg.n_sessions),
    }


def _write_session_provenance(path: Path, cfg: StudyConfig) -> None:
    """Write ``sessions/provenance.json`` recording the generator params used (R12)."""
    path.write_text(json.dumps(_session_provenance_payload(cfg), indent=2, sort_keys=True))


def _session_provenance(path: Path) -> dict[str, Any]:
    """Read a recorded ``sessions/provenance.json``."""
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise SessionProvenanceError(f"invalid session provenance: {path}") from exc
    if not isinstance(data, dict):
        raise SessionProvenanceError(f"invalid session provenance object: {path}")
    return data


@dataclass(frozen=True, slots=True)
class StudyResult:
    """Return value of :func:`run_study` (paths + headline numbers)."""

    out_dir: Path
    support_verdict: str
    primary_horizon_ns: int | None
    theta_primary: float | None
    theta_secondary: float | None
    metrics: dict[str, Any]
    table_paths: dict[str, Path]
    freeze_manifest_path: Path | None
    loaded_models_from_disk: bool


# ---------------------------------------------------------------------------
# Split helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Split:
    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]
    session_dirs: dict[str, Path]


def _chronological_split(cfg: StudyConfig, session_paths: list[Path]) -> _Split:
    """Chronological split by session index (min 1 each)."""
    ids = [p.name for p in session_paths]
    dirs = {p.name: p for p in session_paths}
    n_train, n_val, _n_test = cfg.split_counts
    train = tuple(ids[:n_train])
    validation = tuple(ids[n_train : n_train + n_val])
    test = tuple(ids[n_train + n_val :])
    return _Split(train=train, validation=validation, test=test, session_dirs=dirs)


def _with_scenario(frame: pl.DataFrame, scenario_id: str) -> pl.DataFrame:
    """Add a constant ``scenario_id`` column unless the frame already carries one."""
    if "scenario_id" in frame.columns:
        return frame
    return frame.with_columns(pl.lit(scenario_id, dtype=pl.Utf8).alias("scenario_id"))


def _experiment_config(cfg: StudyConfig, horizon_ns: int, scenario_id: str) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_id=f"{cfg.study_id}:{horizon_ns}:{scenario_id}",
        horizon_ns=horizon_ns,
        latency_id=scenario_id,
        planned_scenario_ids=cfg.scenarios,
        epsilon=cfg.epsilon,
        fee_per_contract_fixed=0,
    )


def _generate_or_reuse_sessions(cfg: StudyConfig, layout: StudyLayout) -> list[Path]:
    """Stage 1 data: generate synthetic sessions, reusing an existing set only on exact match.

    Reuse requires both (a) a ``sessions/provenance.json`` recording generator params that match
    the current config exactly, and (b) that every expected session verifies its checksums. A
    provenance *mismatch* (different seed / duration / session count / generator source) is a
    hard :class:`SessionProvenanceError`, never a silent reuse or overwrite (R12). If no sessions
    and no provenance exist yet, we generate and record provenance.
    """
    sessions_dir = layout.sessions_dir
    sessions_dir.mkdir(parents=True, exist_ok=True)
    provenance_path = sessions_dir / "provenance.json"
    expected = [f"SYN-{i + 1:04d}" for i in range(cfg.n_sessions)]
    existing = [sessions_dir / sid for sid in expected]

    if provenance_path.is_file():
        recorded = _session_provenance(provenance_path)
        wanted = _session_provenance_payload(cfg)
        diffs = {
            key: (recorded.get(key), wanted[key])
            for key in wanted
            if recorded.get(key) != wanted[key]
        }
        if diffs:
            raise SessionProvenanceError(
                "existing sessions provenance does not match the requested config "
                f"(mismatch {diffs}); refusing to reuse. Use a fresh --out directory."
            )
        # Provenance matches: the sessions must all be present and verify.
        if not all(d.is_dir() for d in existing):
            raise SessionProvenanceError(
                f"sessions/provenance.json matches but expected sessions are missing under "
                f"{sessions_dir}; refusing to partially regenerate."
            )
        for index, d in enumerate(existing):
            try:
                verify_session(d)
                params = SyntheticParams(
                    seed=cfg.base_seed + index,
                    session_id=d.name,
                    start_ns=STUDY_EPOCH_NS + index * 86_400_000_000_000,
                    duration_s=cfg.duration_s,
                )
                meta = read_meta(d)
                if (
                    meta.params != asdict(params)
                    or meta.session_id != d.name
                    or meta.dataset_id != "SYNTH.MBO"
                    or meta.start_ns != params.start_ns
                    or read_instrument(d) != params.instrument()
                ):
                    raise ValueError(
                        "session metadata does not match expected generator parameters"
                    )
            except (ValueError, FileNotFoundError) as exc:
                raise SessionProvenanceError(
                    f"existing session {d} failed checksum verification on reuse: {exc}"
                ) from exc
        return existing

    # No provenance recorded. If sessions already exist without provenance, we cannot certify
    # their origin -> hard error rather than silent reuse (R12).
    if any(d.is_dir() for d in existing):
        raise SessionProvenanceError(
            f"sessions exist under {sessions_dir} without a provenance.json; refusing to reuse "
            "sessions of unknown origin. Use a fresh --out directory."
        )

    paths = generate_study(
        sessions_dir,
        n_sessions=cfg.n_sessions,
        base_seed=cfg.base_seed,
        duration_s=cfg.duration_s,
    )
    _write_session_provenance(provenance_path, cfg)
    return paths


def _valid_ladder_horizons(cfg: StudyConfig) -> list[int]:
    """Ladder horizons that are checkpoint-valid under the primary latency (L1)."""
    guard = max(LATENCY_SCENARIOS[s].guard_ns for s in cfg.scenarios)
    return [h for h in cfg.horizon_ladder_ns if checkpoint_valid(h, guard)]


def _run_support_gate(cfg: StudyConfig, split: _Split) -> SupportGateResult:
    """Stage 2: G2-S support gate on the first ``n_pilot_sessions`` train sessions (counts only)."""
    pilots = list(split.train[: cfg.n_pilot_sessions])
    support_by_horizon: dict[int, pl.DataFrame] = {}
    for horizon in _valid_ladder_horizons(cfg):
        frames: list[pl.DataFrame] = []
        for sid in pilots:
            config = _experiment_config(cfg, horizon, PRIMARY_LATENCY_ID)
            engine = SessionEngine(
                split.session_dirs[sid],
                config,
                [B1Policy()],
                scenario_id=PRIMARY_LATENCY_ID,
                fork_label_probe=False,
                price_scorer=None,
                record_checkpoint_features=True,
            )
            out = engine.run()
            frames.append(out.support)
        support_by_horizon[horizon] = (
            pl.concat(frames) if frames else pl.DataFrame({"session_id": []})
        )
    return select_support_horizon(
        support_by_horizon,
        pilots,
        support_min_eligible=cfg.support_min_eligible,
        support_min_queue_depletion=cfg.support_min_queue_depletion,
    )


@dataclass(frozen=True, slots=True)
class _DevelopmentData:
    """Per-session development artifacts at the primary horizon (train + validation)."""

    branch_labels: dict[str, pl.DataFrame]
    """session_id -> branch_labels frame with ``price_label`` and ``price_label_100ms`` columns."""
    n_price_label_none: int
    """Count of checkpoint rows excluded from price-model fitting for a None label."""
    primary_tau_ns: int


def _attach_price_labels(
    branch: pl.DataFrame, mid_series: MidSeries, primary_tau_ns: int, tick: int
) -> pl.DataFrame:
    """Attach primary and secondary price-direction labels to each branch row by checkpoint time.

    Rows with class 0 are retained (T43); rows with a None label keep a null and are excluded
    from price-model fitting downstream only. The HOLD and SWITCH rows of one task share the same
    checkpoint time, so both receive the same price label (the label is a property of the market,
    not the action).
    """
    if branch.height == 0:
        return branch.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("price_label"),
            pl.lit(None, dtype=pl.Int64).alias("price_label_100ms"),
        )
    primary: list[int | None] = []
    secondary: list[int | None] = []
    for t in branch.get_column("checkpoint_time_ns").to_list():
        if t is None:
            primary.append(None)
            secondary.append(None)
            continue
        t_int = int(t)
        primary.append(price_direction_label(mid_series, t_int, primary_tau_ns, tick))
        secondary.append(price_direction_label(mid_series, t_int, SECONDARY_PRICE_TAU_NS, tick))
    return branch.with_columns(
        pl.Series("price_label", primary, dtype=pl.Int64),
        pl.Series("price_label_100ms", secondary, dtype=pl.Int64),
    )


def _run_development(cfg: StudyConfig, split: _Split, horizon_ns: int) -> _DevelopmentData:
    """Stage 3: branch labels at the primary horizon, L1, on train+validation sessions."""
    primary_tau = primary_price_tau_ns(horizon_ns)
    branch_by_session: dict[str, pl.DataFrame] = {}
    n_none = 0
    dev_sessions = list(split.train) + list(split.validation)
    for sid in dev_sessions:
        config = _experiment_config(cfg, horizon_ns, PRIMARY_LATENCY_ID)
        engine = SessionEngine(
            split.session_dirs[sid],
            config,
            [B1Policy()],
            scenario_id=PRIMARY_LATENCY_ID,
            fork_label_probe=True,
            price_scorer=None,
            record_checkpoint_features=True,
        )
        out = engine.run()
        tick = _session_tick(split.session_dirs[sid])
        mid_series = MidSeries.from_session(split.session_dirs[sid])
        labelled = _attach_price_labels(out.branch_labels, mid_series, primary_tau, tick)
        branch_by_session[sid] = labelled
        # Count distinct checkpoint rows (one per task; HOLD/SWITCH share a label) with no label.
        if labelled.height > 0:
            hold_rows = _checkpoint_rows(_evaluable_rows(labelled))
            n_none += int(hold_rows.filter(pl.col("price_label").is_null()).height)
    return _DevelopmentData(
        branch_labels=branch_by_session,
        n_price_label_none=n_none,
        primary_tau_ns=primary_tau,
    )


def _session_tick(session_dir: Path) -> int:
    return int(read_instrument(session_dir).tick_size_fixed)


@dataclass(frozen=True, slots=True)
class _PriceStage:
    primary_model: PriceModel
    secondary_model: PriceModel
    primary_schema: FeatureSchema
    oof_signal_by_session: dict[str, pl.DataFrame]
    """session_id -> frame keyed by task_id with OOF price-signal columns (train sessions only)."""
    oof_scored_sessions: tuple[str, ...]
    oof_unscored_sessions: tuple[str, ...]
    train_index_log: dict[str, list[str]]
    primary_log_loss: float | None
    secondary_log_loss: float | None
    fit_provenance: dict[str, Any]
    """Exact fitting/tuning populations, exclusions, and OOF history (never planned sessions)."""
    diagnostics: dict[str, Any]


def _checkpoint_rows(branch: pl.DataFrame) -> pl.DataFrame:
    """One row per task's checkpoint (dedupe HOLD/SWITCH), carrying market features + labels."""
    if branch.height == 0:
        return branch
    return branch.filter(pl.col("action") == "HOLD")


def _market_matrix(rows: pl.DataFrame, schema: FeatureSchema) -> np.ndarray:
    if rows.height == 0:
        return np.empty((0, schema.n_features), dtype=np.float64)
    cols = [f"feat_{name}" for name in schema.names]
    missing = [c for c in cols if c not in rows.columns]
    if missing:
        raise ValueError(f"branch rows missing market feature columns: {missing}")
    x = rows.select(cols).to_numpy().astype(np.float64, copy=False)
    if not np.isfinite(x).all():
        raise ValueError("market training/scoring features must be finite")
    return x


def _fit_price_on_sessions(
    schema: FeatureSchema,
    branch_by_session: dict[str, pl.DataFrame],
    session_ids: list[str],
    *,
    label_col: str,
    provenance: dict[str, Any] | None = None,
) -> PriceModel | None:
    """Choose C chronologically, then refit every eligible row of the supplied train sessions."""
    frames: list[pl.DataFrame] = []
    fitted_sessions: list[str] = []
    per_session: dict[str, dict[str, int]] = {}
    for sid in session_ids:
        raw = _checkpoint_rows(branch_by_session[sid])
        evaluable = _checkpoint_rows(_evaluable_rows(branch_by_session[sid]))
        labelled = (
            evaluable.filter(pl.col(label_col).is_not_null()) if evaluable.height else evaluable
        )
        per_session[sid] = {
            "n_checkpoint_rows": raw.height,
            "n_technical_excluded": raw.height - evaluable.height,
            "n_label_missing": evaluable.height - labelled.height,
            "n_training_rows": labelled.height,
        }
        if labelled.height:
            frames.append(labelled)
            fitted_sessions.append(sid)
    log: dict[str, Any] = {
        "training_sessions": fitted_sessions,
        "tuning_train_sessions": fitted_sessions[:-1] if len(frames) >= 2 else [],
        "tuning_validation_sessions": fitted_sessions[-1:] if len(frames) >= 2 else [],
        "n_training_rows": sum(f.height for f in frames),
        "per_session": per_session,
    }
    if provenance is not None:
        provenance.update(log)
    if not frames:
        return None
    selection = PriceModel(schema)
    if len(frames) >= 2:
        inner_train, inner_val = pl.concat(frames[:-1]), frames[-1]
        selection.fit(
            _market_matrix(inner_train, schema),
            inner_train.get_column(label_col).to_numpy(),
            _market_matrix(inner_val, schema),
            inner_val.get_column(label_col).to_numpy(),
        )
        chosen_c = selection.chosen_c
    else:
        chosen_c = selection.c_grid[0]
    train = pl.concat(frames)
    model = PriceModel(schema, c_grid=(chosen_c,))
    model.fit(_market_matrix(train, schema), train.get_column(label_col).to_numpy())
    if provenance is not None:
        provenance.update({"c_selection_grid": list(selection.c_grid), "chosen_c": chosen_c})
    return model


def _price_signal_frame(model: PriceModel, rows: pl.DataFrame) -> pl.DataFrame:
    """Compute price-signal columns for ``rows`` (task_id keyed), including u_signal."""
    schema = model.schema
    x = _market_matrix(rows, schema)
    proba = model.predict_proba(x)
    p_down = proba[:, PRICE_CLASSES.index(-1)]
    p_unch = proba[:, PRICE_CLASSES.index(0)]
    p_up = proba[:, PRICE_CLASSES.index(1)]
    return rows.select("task_id").with_columns(
        pl.Series("p_down", p_down),
        pl.Series("p_unch", p_unch),
        pl.Series("p_up", p_up),
        pl.Series("u_signal", p_up - p_down),
    )


def _run_price_models(cfg: StudyConfig, split: _Split, dev: _DevelopmentData) -> _PriceStage:
    """Stage 4: forward-chained OOF price scores for B3 training rows (T44) + final models."""
    schema = FeatureSchema.of(list(MARKET_FEATURES))
    train_ids = list(split.train)

    # Forward-chained OOF over train sessions in chronological order (T44). For session k, fit on
    # the labelled checkpoint rows of sessions < k and score session k's checkpoint rows.
    oof_signal: dict[str, pl.DataFrame] = {}
    scored: list[str] = []
    unscored: list[str] = []
    train_index_log: dict[str, list[str]] = {}
    fit_provenance: dict[str, Any] = {"oof": {}}
    for k, sid in enumerate(train_ids):
        prior = train_ids[:k]
        rows_k = _checkpoint_rows(_evaluable_rows(dev.branch_labels[sid]))
        if not prior or rows_k.height == 0:
            unscored.append(sid)
            train_index_log[sid] = []
            fit_provenance["oof"][sid] = {
                "training_sessions": [],
                "n_scored_rows": 0,
                "reason": "NO_PRIOR_SESSIONS" if not prior else "NO_EVALUABLE_CHECKPOINTS",
            }
            continue
        log: dict[str, Any] = {}
        model = _fit_price_on_sessions(
            schema, dev.branch_labels, prior, label_col="price_label", provenance=log
        )
        train_index_log[sid] = list(log["training_sessions"])
        fit_provenance["oof"][sid] = log
        if model is None:
            unscored.append(sid)
            log.update({"n_scored_rows": 0, "reason": "NO_PRIOR_LABELLED_ROWS"})
            continue
        oof_signal[sid] = _price_signal_frame(model, rows_k)
        scored.append(sid)
        log["n_scored_rows"] = rows_k.height

    # Final primary and secondary models on all train sessions (last train session = inner val).
    fit_provenance["primary"] = {}
    fit_provenance["secondary"] = {}
    primary = _fit_price_on_sessions(
        schema,
        dev.branch_labels,
        train_ids,
        label_col="price_label",
        provenance=fit_provenance["primary"],
    )
    secondary = _fit_price_on_sessions(
        schema,
        dev.branch_labels,
        train_ids,
        label_col="price_label_100ms",
        provenance=fit_provenance["secondary"],
    )
    if primary is None or secondary is None:
        raise RuntimeError("price models could not be fit (no labelled training rows)")

    diagnostics = {
        "primary": _price_diagnostics(primary, dev, split, "price_label"),
        "secondary": _price_diagnostics(secondary, dev, split, "price_label_100ms"),
    }
    return _PriceStage(
        primary_model=primary,
        secondary_model=secondary,
        primary_schema=schema,
        oof_signal_by_session=oof_signal,
        oof_scored_sessions=tuple(scored),
        oof_unscored_sessions=tuple(unscored),
        train_index_log=train_index_log,
        primary_log_loss=diagnostics["primary"]["log_loss"],
        secondary_log_loss=diagnostics["secondary"]["log_loss"],
        fit_provenance=fit_provenance,
        diagnostics=diagnostics,
    )


def _price_diagnostics(
    model: PriceModel, dev: _DevelopmentData, split: _Split, label_col: str
) -> dict[str, Any]:
    """Validation log loss against a class-frequency baseline estimated on training only."""

    def rows_for(ids: tuple[str, ...]) -> tuple[pl.DataFrame, dict[str, int]]:
        frames = [_checkpoint_rows(dev.branch_labels[s]) for s in ids]
        raw = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        clean_frames = [_checkpoint_rows(_evaluable_rows(dev.branch_labels[s])) for s in ids]
        clean = pl.concat(clean_frames, how="diagonal_relaxed") if clean_frames else pl.DataFrame()
        labelled = clean.filter(pl.col(label_col).is_not_null()) if clean.height else clean
        return labelled, {
            "n_checkpoint_rows": raw.height,
            "n_technical_excluded": raw.height - clean.height,
            "n_label_missing": clean.height - labelled.height,
            "n_rows": labelled.height,
        }

    train, train_counts = rows_for(split.train)
    val, val_counts = rows_for(split.validation)
    train_y = train.get_column(label_col).to_numpy()
    base = np.asarray([np.mean(train_y == c) for c in PRICE_CLASSES], dtype=np.float64)
    result: dict[str, Any] = {
        **val_counts,
        "training": train_counts,
        "training_class_probabilities": base.tolist(),
        "log_loss": None,
        "base_rate_log_loss": None,
    }
    if val.height:
        y = val.get_column(label_col).to_numpy()
        result["log_loss"] = PriceModel._log_loss(
            model.predict_proba(_market_matrix(val, model.schema)), y
        )
        result["base_rate_log_loss"] = PriceModel._log_loss(np.tile(base, (val.height, 1)), y)
    return result


@dataclass(frozen=True, slots=True)
class _ActionStage:
    b3_models: ActionModels
    b3_no_queue_models: ActionModels
    b3_schema: FeatureSchema
    b3_no_queue_schema: FeatureSchema
    b3_support: SupportRule
    b3_no_queue_support: SupportRule
    n_train_rows: int
    excluded_sessions_no_oof: tuple[str, ...]
    diagnostics: dict[str, Any]
    training_sessions: tuple[str, ...]


def _action_training_frame(
    split: _Split, dev: _DevelopmentData, price: _PriceStage
) -> tuple[pl.DataFrame, list[str]]:
    """Build the B3 training frame (train sessions with an OOF score; both arms, both actions).

    Each branch-label row (task x action) is joined to its forward-chained OOF price signal by
    task id, so the price-signal features entering B3 training are honest (never in-sample)."""
    frames: list[pl.DataFrame] = []
    excluded: list[str] = []
    for sid in split.train:
        if sid not in price.oof_signal_by_session:
            excluded.append(sid)
            continue
        branch = dev.branch_labels[sid]
        if branch.height == 0:
            continue
        signal = price.oof_signal_by_session[sid]
        joined = branch.join(signal, on="task_id", how="inner")
        frames.append(joined)
    if not frames:
        return pl.DataFrame(), excluded
    return pl.concat(frames, how="vertical"), excluded


def _schema_matrix(rows: pl.DataFrame, schema: FeatureSchema) -> np.ndarray:
    """Build x in schema order. Market/mechanics/queue come from ``feat_*``; price from bare."""
    if rows.height == 0:
        return np.empty((0, schema.n_features), dtype=np.float64)
    cols: list[str] = []
    for name in schema.names:
        if name in _PRICE_CLASS_SIGNAL:
            cols.append(name)
        else:
            cols.append(f"feat_{name}")
    missing = [c for c in cols if c not in rows.columns]
    if missing:
        raise ValueError(f"action training rows missing columns: {missing}")
    return rows.select(cols).to_numpy().astype(np.float64, copy=False)


def _fit_action_arm(
    schema: FeatureSchema,
    train: pl.DataFrame,
    support_frame: pl.DataFrame,
    min_rows: int,
    margin_frac: float,
) -> tuple[ActionModels, SupportRule]:
    if not support_frame.equals(train):
        raise ValueError("support must use the exact action-training frame")
    models = ActionModels(schema)
    population_counts: list[int] = []
    # Fit per action. R10 (C half): the miss model trains on EVERY evaluable action row, never
    # dropping a row because its cost is null; the cost model trains on the non-null-cost rows
    # only (counted). R4: rows flagged technically_unevaluable are excluded (counted) when the
    # engine provides that column.
    for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
        evaluable = _evaluable_rows(train).filter(pl.col("action") == action.value)
        cost_rows = evaluable.filter(pl.col("c_t_ticks").is_not_null())
        population_counts.extend((evaluable.height, cost_rows.height))
        if evaluable.height == 0:
            raise ValueError(f"{action.value} has no evaluable miss training rows")
        if cost_rows.height == 0:
            raise ValueError(f"{action.value} has no evaluable cost training rows")
        # Alpha selection uses only earlier training sessions versus their last session.
        # Refit below uses every eligible cost row; miss fitting always uses all eligible rows.
        alpha = models.alpha_grid[0]
        if "session_id" in cost_rows.columns:
            sessions = cost_rows.get_column("session_id").unique(maintain_order=True).to_list()
            if len(sessions) >= 2:
                inner = cost_rows.filter(pl.col("session_id") != sessions[-1])
                val = cost_rows.filter(pl.col("session_id") == sessions[-1])
                selection = ActionModels(schema)
                selection.fit_action(
                    action,
                    _schema_matrix(inner, schema),
                    inner.get_column("miss").to_numpy(),
                    inner.get_column("c_t_ticks").to_numpy(),
                    x_val=_schema_matrix(val, schema),
                    cost_val=val.get_column("c_t_ticks").to_numpy(),
                )
                alpha = selection.chosen_alpha(action)
        x_cost = _schema_matrix(cost_rows, schema)
        cost = cost_rows.get_column("c_t_ticks").to_numpy().astype(np.float64)
        models.fit_action(
            action,
            _schema_matrix(evaluable, schema),
            evaluable.get_column("miss").to_numpy(),
            cost,
            x_cost_train=x_cost,
            cost_alpha=alpha,
        )
    # SupportRule on the union of both arms' rows from the SAME matrix the action models were fit
    # on (R20): ``support_frame`` is the action-training frame itself, so the support region and
    # the fitted models share one feature matrix. The per-feature margin is
    # ``margin_frac * (training max - min)`` (finite; 0 for constant features). Without a margin,
    # any test value just outside the training min/max of ANY feature forces a
    # FALLBACK_UNSUPPORTED; the margin keeps every feature in the support check (none is dropped)
    # while admitting values a modest fraction outside the observed range. See
    # docs/research_spec.md section 2 step 5. If the exact-matrix support still makes B3 mostly
    # fallback, that is reported honestly via the degeneracy guardrails, never hidden.
    all_rows = _evaluable_rows(support_frame)
    x_all = _schema_matrix(all_rows, schema)
    margins = _support_margins(x_all, schema.n_features, margin_frac)
    support = SupportRule.from_training(schema, x_all, min_training_rows=min_rows, margin=margins)
    support = replace(support, n_min_fit_rows=min(population_counts))
    return models, support


def _evaluable_rows(frame: pl.DataFrame) -> pl.DataFrame:
    """Exclude branch rows flagged ``technically_unevaluable`` (R4) when the column is present.

    The engine (plane A) stamps ``technically_unevaluable`` on branch labels from the shared
    quality map. When present, a row with ``technically_unevaluable == True`` is excluded from
    training here (counted upstream); when the column is absent (older engine output), all rows
    pass through.
    """
    if frame.height == 0 or "technically_unevaluable" not in frame.columns:
        return frame
    if "task_id" in frame.columns:
        # Evaluability belongs to the task, not the action: exclude both branches even if an
        # upstream defect or imported frame marked only one branch.
        bad_tasks = (
            frame.filter(pl.col("technically_unevaluable").fill_null(False))
            .select("task_id")
            .unique()
        )
        return frame.join(bad_tasks, on="task_id", how="anti", maintain_order="left")
    return frame.filter(~pl.col("technically_unevaluable").fill_null(False))


def _support_margins(x: np.ndarray, n_features: int, margin_frac: float) -> tuple[float, ...]:
    """Per-feature absolute support margin ``margin_frac * (max - min)`` (0 for constant/empty).

    Always finite and nonnegative; a feature with zero training spread (constant) gets margin 0,
    so it is still checked (equality to the frozen value) rather than dropped from the rule.
    """
    if x.ndim != 2 or x.shape[0] == 0:
        return tuple(0.0 for _ in range(n_features))
    spread = x.max(axis=0) - x.min(axis=0)
    return tuple(float(margin_frac) * float(s) for s in spread)


def _run_action_models(
    cfg: StudyConfig, split: _Split, dev: _DevelopmentData, price: _PriceStage
) -> _ActionStage:
    """Stage 5: action models for B3 and B3_NO_QUEUE on identical train rows (schemas differ)."""
    b3_schema = FeatureSchema.of(list(allowlist("B3")))
    b3_nq_schema = FeatureSchema.of(list(allowlist("B3_NO_QUEUE")))
    # The two schemas must differ exactly by QUEUE_FEATURES (T45).
    b3_set, nq_set = set(b3_schema.names), set(b3_nq_schema.names)
    if b3_set - nq_set != set(QUEUE_FEATURES) or nq_set - b3_set != set():
        raise RuntimeError("B3 and B3_NO_QUEUE schemas must differ exactly by QUEUE_FEATURES")

    train, excluded = _action_training_frame(split, dev, price)
    if train.height == 0:
        raise ValueError("action models have no eligible training rows with an OOF price score")
    n_rows = _evaluable_rows(train).height
    min_rows = max(1, cfg.support_min_eligible // 2)
    # R20 (decided with evidence): the support region is built from the EXACT matrix the action
    # models are fit on (the honest OOF-signal ``train`` frame), never a separately re-scored
    # frame. If this makes B3 mostly fallback it is reported honestly via the degeneracy
    # guardrails, not hidden by redefining support.
    b3_models, b3_support = _fit_action_arm(
        b3_schema, train, train, min_rows, cfg.support_margin_frac
    )
    b3_nq_models, b3_nq_support = _fit_action_arm(
        b3_nq_schema, train, train, min_rows, cfg.support_margin_frac
    )

    diagnostics = _action_diagnostics(
        split,
        dev,
        price,
        b3_schema=b3_schema,
        b3_nq_schema=b3_nq_schema,
        b3_models=b3_models,
        b3_nq_models=b3_nq_models,
        b3_support=b3_support,
        b3_nq_support=b3_nq_support,
        epsilon=cfg.epsilon,
    )
    diagnostics["price_fit_provenance"] = price.fit_provenance
    diagnostics["price_validation"] = price.diagnostics
    diagnostics["training_population"] = {
        sid: {
            "n_branch_rows": dev.branch_labels[sid].height,
            "n_technical_excluded": dev.branch_labels[sid].height
            - _evaluable_rows(dev.branch_labels[sid]).height,
            "n_evaluable_without_oof": _evaluable_rows(dev.branch_labels[sid]).height
            if sid in excluded
            else 0,
            "n_training_rows": train.filter(pl.col("session_id") == sid).height,
        }
        for sid in split.train
    }
    diagnostics["action_training"] = {
        "b3": _action_fit_provenance(train, b3_models),
        "b3_no_queue": _action_fit_provenance(train, b3_nq_models),
    }
    return _ActionStage(
        b3_models=b3_models,
        b3_no_queue_models=b3_nq_models,
        b3_schema=b3_schema,
        b3_no_queue_schema=b3_nq_schema,
        b3_support=b3_support,
        b3_no_queue_support=b3_nq_support,
        n_train_rows=n_rows,
        excluded_sessions_no_oof=tuple(excluded),
        diagnostics=diagnostics,
        training_sessions=tuple(
            _evaluable_rows(train).get_column("session_id").unique(maintain_order=True).to_list()
        ),
    )


def _action_fit_provenance(train: pl.DataFrame, models: ActionModels) -> dict[str, Any]:
    """Separate risk/value fitting and value-tuning populations, in chronological order."""
    result: dict[str, Any] = {}
    for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
        rows = _evaluable_rows(train).filter(pl.col("action") == action.value)
        costs = rows.filter(pl.col("c_t_ticks").is_not_null())
        sessions = costs.get_column("session_id").unique(maintain_order=True).to_list()
        result[action.value] = {
            "n_miss_rows": rows.height,
            "n_cost_rows": costs.height,
            "n_cost_missing": rows.height - costs.height,
            "miss_training_sessions": rows.get_column("session_id")
            .unique(maintain_order=True)
            .to_list(),
            "cost_training_sessions": sessions,
            "cost_tuning_train_sessions": sessions[:-1] if len(sessions) >= 2 else [],
            "cost_tuning_validation_sessions": sessions[-1:] if len(sessions) >= 2 else [],
            "chosen_alpha": models.chosen_alpha(action),
        }
    return result


def _unsupported_feature_counts(
    rows: pl.DataFrame, schema: FeatureSchema, support: SupportRule
) -> dict[str, int]:
    """Count, per feature, how many checkpoint rows fall outside its support band (R20).

    This is the honest diagnostic of *which features most often trigger* ``FALLBACK_UNSUPPORTED``:
    for each row we check each feature independently against the frozen
    ``[min - margin, max + margin]`` band and tally the features that are out of range. A row can
    contribute to several feature counts; the counts are per-feature (not a partition).
    """
    if rows.height == 0:
        return {}
    x = _schema_matrix(rows, schema)
    counts = {name: 0 for name in schema.names}
    lo = np.asarray(support.feature_min) - np.asarray(support.feature_margin)
    hi = np.asarray(support.feature_max) + np.asarray(support.feature_margin)
    for i in range(x.shape[0]):
        row = x[i]
        out = (~np.isfinite(row)) | (row < lo) | (row > hi)
        for j, name in enumerate(schema.names):
            if out[j]:
                counts[name] += 1
    # Report only features that ever triggered, sorted by descending count then name.
    triggered = {k: v for k, v in counts.items() if v > 0}
    return dict(sorted(triggered.items(), key=lambda kv: (-kv[1], kv[0])))


def _action_diagnostics(
    split: _Split,
    dev: _DevelopmentData,
    price: _PriceStage,
    *,
    b3_schema: FeatureSchema,
    b3_nq_schema: FeatureSchema,
    b3_models: ActionModels,
    b3_nq_models: ActionModels,
    b3_support: SupportRule,
    b3_nq_support: SupportRule,
    epsilon: float,
) -> dict[str, Any]:
    """Validation diagnostics on validation branch labels (research spec stage 5; R16).

    The action choice is computed through :func:`risk_allowance_choice` with the config epsilon
    and the frozen ``SupportRule`` -- exactly like :class:`B3Policy` -- so the reported
    disagreement rate matches what the policy actually decides (R16). Calibration (miss Brier /
    log loss vs the base rate) and error (cost RMSE vs the constant training mean) diagnostics
    are reported per action and per variant.
    """
    # Score validation branch rows with the final price model to form price-signal features.
    val_frames: list[pl.DataFrame] = []
    for sid in split.validation:
        branch = dev.branch_labels[sid]
        if branch.height == 0:
            continue
        signal = _price_signal_frame(price.primary_model, _checkpoint_rows(_evaluable_rows(branch)))
        # Keep invalid rows only for exclusion accounting; never score them or use them below.
        val_frames.append(branch.join(signal, on="task_id", how="left"))
    raw_val = pl.concat(val_frames, how="vertical") if val_frames else pl.DataFrame()
    val = _evaluable_rows(raw_val)
    train, _ = _action_training_frame(split, dev, price)
    diag: dict[str, Any] = {
        "n_validation_rows": val.height,
        "n_validation_technical_excluded": raw_val.height - val.height,
    }

    # Action disagreement rate B3 vs B3_NO_QUEUE over validation checkpoints, using the SAME
    # decision rule B3Policy uses (risk_allowance_choice with epsilon + frozen support, R16).
    chk = _checkpoint_rows(val)
    x_b3 = _schema_matrix(chk, b3_schema)
    x_nq = _schema_matrix(chk, b3_nq_schema)
    disagree = 0
    for i in range(chk.height):
        pb = b3_models.predict(x_b3[i])
        pn = b3_nq_models.predict(x_nq[i])
        cb, _ = risk_allowance_choice(
            {a: pb[a].p_hat for a in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)},
            {a: pb[a].v_hat for a in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)},
            epsilon,
            supported=b3_support.is_supported(x_b3[i]),
        )
        cn, _ = risk_allowance_choice(
            {a: pn[a].p_hat for a in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)},
            {a: pn[a].v_hat for a in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH)},
            epsilon,
            supported=b3_nq_support.is_supported(x_nq[i]),
        )
        if cb is not cn:
            disagree += 1
    diag["action_disagreement_rate"] = disagree / chk.height if chk.height else None
    diag["n_validation_checkpoints"] = chk.height
    # R16: calibration (miss Brier / log loss vs base rate) + error (cost RMSE vs constant mean).
    diag["calibration"] = {
        "b3": _calibration_by_action(raw_val, b3_schema, b3_models, train),
        "b3_no_queue": _calibration_by_action(raw_val, b3_nq_schema, b3_nq_models, train),
    }
    # R20: which features most often trigger FALLBACK_UNSUPPORTED (per-feature counts over the
    # validation checkpoints, against the frozen support band). Reported for both variants.
    diag["unsupported_feature_counts_b3"] = _unsupported_feature_counts(chk, b3_schema, b3_support)
    diag["unsupported_feature_counts_b3_no_queue"] = _unsupported_feature_counts(
        chk, b3_nq_schema, b3_nq_support
    )
    return diag


def _calibration_by_action(
    val: pl.DataFrame, schema: FeatureSchema, models: ActionModels, train: pl.DataFrame
) -> dict[str, dict[str, float | int | None]]:
    """Score every evaluable miss and each available cost against TRAIN-only baselines."""
    out: dict[str, dict[str, float | int | None]] = {}
    clean = _evaluable_rows(val)
    train = _evaluable_rows(train)
    for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
        rows = clean.filter(pl.col("action") == action.value) if clean.height else clean
        raw = val.filter(pl.col("action") == action.value) if val.height else val
        training = train.filter(pl.col("action") == action.value)
        training_cost = training.filter(pl.col("c_t_ticks").is_not_null())
        base = float(training.get_column("miss").to_numpy().mean()) if training.height else None
        mean_cost = (
            float(training_cost.get_column("c_t_ticks").to_numpy().mean())
            if training_cost.height
            else None
        )
        n = rows.height
        n_cost = rows.filter(pl.col("c_t_ticks").is_not_null()).height if n else 0
        block: dict[str, float | int | None] = {
            "miss_brier": None,
            "miss_log_loss": None,
            "miss_base_rate_log_loss": None,
            "miss_base_rate_brier": None,
            "cost_rmse": None,
            "cost_constant_mean_rmse": None,
            "n_rows": n,
            "n_cost_rows": n_cost,
            "n_cost_missing": n - n_cost,
            "n_technical_excluded": raw.height - n,
            "training_miss_rate": base,
            "training_mean_cost": mean_cost,
            "n_training_miss_rows": training.height,
            "n_training_cost_rows": training_cost.height,
        }
        out[action.value] = block
        if n == 0:
            continue
        x = _schema_matrix(rows, schema)
        miss = rows.get_column("miss").to_numpy().astype(np.float64)
        cost = rows.get_column("c_t_ticks").to_numpy().astype(np.float64)
        p_pred = np.empty(n, dtype=np.float64)
        v_pred = np.empty(n, dtype=np.float64)
        for i in range(n):
            pred = models.predict(x[i])[action]
            p_pred[i] = pred.p_hat
            v_pred[i] = pred.v_hat
        eps = 1e-12
        p_clip = np.clip(p_pred, eps, 1.0 - eps)
        miss_log_loss = float(-np.mean(miss * np.log(p_clip) + (1.0 - miss) * np.log(1.0 - p_clip)))
        block["miss_brier"] = float(np.mean((p_pred - miss) ** 2))
        block["miss_log_loss"] = miss_log_loss
        if base is not None:
            base_clip = min(max(base, eps), 1.0 - eps)
            block["miss_base_rate_log_loss"] = float(
                -np.mean(miss * np.log(base_clip) + (1.0 - miss) * np.log(1.0 - base_clip))
            )
            block["miss_base_rate_brier"] = float(np.mean((base - miss) ** 2))
        cost_present = rows.get_column("c_t_ticks").is_not_null().to_numpy()
        if n_cost:
            cost = cost[cost_present]
            if not np.isfinite(cost).all():
                raise ValueError("validation costs must be finite when present")
            block["cost_rmse"] = float(np.sqrt(np.mean((v_pred[cost_present] - cost) ** 2)))
            if mean_cost is not None:
                block["cost_constant_mean_rmse"] = float(np.sqrt(np.mean((mean_cost - cost) ** 2)))
    return out


@dataclass(frozen=True, slots=True)
class _ValidationStage:
    theta_primary: float
    theta_secondary: float
    primary_trials: list[dict[str, Any]]
    secondary_trials: list[dict[str, Any]]


def _theta_policy_id(signal: str, theta: float) -> str:
    tag = "B2" if signal == "u_signal" else "B2_100MS"
    return f"{tag}@{theta}"


def _run_validation_selection(
    cfg: StudyConfig, split: _Split, price: _PriceStage, horizon_ns: int
) -> _ValidationStage:
    """Stage 6: choose theta for B2 and B2_100MS from validation rows only (T26)."""
    scorer = PriceSignalScorer(price.primary_model, price.secondary_model)
    primary_rows: list[dict[str, Any]] = []
    secondary_rows: list[dict[str, Any]] = []

    for sid in split.validation:
        config = _experiment_config(cfg, horizon_ns, PRIMARY_LATENCY_ID)
        policies: list[Any] = []
        id_to_theta: dict[str, tuple[str, float]] = {}
        for theta in THETA_MENU:
            pid_p = _theta_policy_id("u_signal", theta)
            pid_s = _theta_policy_id("u_signal_100ms", theta)
            policies.append(B2Policy(theta, "u_signal", pid_p))
            policies.append(B2Policy(theta, "u_signal_100ms", pid_s))
            id_to_theta[pid_p] = ("primary", theta)
            id_to_theta[pid_s] = ("secondary", theta)
        engine = SessionEngine(
            split.session_dirs[sid],
            config,
            policies,
            scenario_id=PRIMARY_LATENCY_ID,
            fork_label_probe=False,
            price_scorer=scorer,
            record_checkpoint_features=False,
        )
        out = engine.run()
        tr = out.task_results
        evaluable = tr.filter(pl.col("status") != "TECHNICALLY_UNEVALUABLE")
        for row in evaluable.iter_rows(named=True):
            pid = row["policy_id"]
            if pid not in id_to_theta:
                continue
            kind, theta = id_to_theta[pid]
            miss = 1 if row["status"] == "DEADLINE_MISS" else 0
            c_t = row["c_t_ticks"]
            if c_t is None or not np.isfinite(float(c_t)):
                raise ValueError(
                    "validation C_T must be defined and finite for every evaluable task: "
                    f"{sid}/{pid}"
                )
            rec = {
                "theta": theta,
                "session_id": sid,
                "miss": miss,
                "c_t": float(c_t),
            }
            (primary_rows if kind == "primary" else secondary_rows).append(rec)

    theta_primary, primary_trials = _select_theta_logged(primary_rows, cfg.epsilon)
    theta_secondary, secondary_trials = _select_theta_logged(secondary_rows, cfg.epsilon)
    return _ValidationStage(
        theta_primary=theta_primary,
        theta_secondary=theta_secondary,
        primary_trials=primary_trials,
        secondary_trials=secondary_trials,
    )


def _select_theta_logged(
    rows: list[dict[str, Any]], epsilon: float
) -> tuple[float, list[dict[str, Any]]]:
    if not rows:
        # No evaluable validation rows: fall back to the smallest menu theta, logged honestly.
        return THETA_MENU[0], [
            {"theta": t, "mean_miss_rate": None, "mean_c_t": None, "n_sessions": 0, "n_tasks": 0}
            for t in THETA_MENU
        ]
    frame = pl.DataFrame(rows)
    selection = select_theta(frame, epsilon)
    trials = [
        {
            "theta": t.theta,
            "mean_miss_rate": None if np.isnan(t.mean_miss_rate) else t.mean_miss_rate,
            "mean_c_t": None if np.isnan(t.mean_c_t) else t.mean_c_t,
            "n_sessions": t.n_sessions,
            "n_tasks": t.n_tasks,
            "within_allowance": t.within_allowance,
        }
        for t in selection.trials
    ]
    return selection.chosen_theta, trials


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _save_support_rule(support: SupportRule, path: Path) -> None:
    state = {
        "feature_names": list(support.schema.names),
        "feature_min": list(support.feature_min),
        "feature_max": list(support.feature_max),
        "feature_margin": list(support.feature_margin),
        "min_training_rows": support.min_training_rows,
        "n_training_rows": support.n_training_rows,
        "n_min_fit_rows": support.n_min_fit_rows,
    }
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


@dataclass(frozen=True, slots=True)
class _FreezeStage:
    manifest_path: Path
    model_paths: dict[str, str]
    support_paths: dict[str, str]
    artifact_hashes: dict[str, str]


def _run_freeze(
    cfg: StudyConfig,
    layout: StudyLayout,
    *,
    split: _Split,
    horizon_ns: int,
    price: _PriceStage,
    action: _ActionStage,
    validation: _ValidationStage,
    dev: _DevelopmentData,
) -> _FreezeStage:
    """Stage 7: save every model with ModelArtifact; write freeze/manifest.json with hashes."""
    # R14: the freeze is write-once across every invocation.
    ensure_freeze_writable(layout)
    primary_train = price.fit_provenance["primary"]["training_sessions"]
    secondary_train = price.fit_provenance["secondary"]["training_sessions"]
    action_train = list(action.training_sessions)
    # Save price models.
    price_primary = layout.model_path("price_primary")
    price_secondary = layout.model_path("price_secondary")
    ModelArtifact.create(
        "price",
        schema=price.primary_schema,
        training_sessions=primary_train,
        preprocessing="standardize",
        hyperparameters={"c_grid": list(price.primary_model.c_grid), "label": "price_label"},
        seed=0,
        state=price.primary_model.to_state(),
    ).save(price_primary)
    ModelArtifact.create(
        "price",
        schema=price.primary_schema,
        training_sessions=secondary_train,
        preprocessing="standardize",
        hyperparameters={
            "c_grid": list(price.secondary_model.c_grid),
            "label": "price_label_100ms",
        },
        seed=0,
        state=price.secondary_model.to_state(),
    ).save(price_secondary)

    # Save action models.
    action_b3 = layout.model_path("action_b3")
    action_b3_nq = layout.model_path("action_b3_no_queue")
    ModelArtifact.create(
        "action",
        schema=action.b3_schema,
        training_sessions=action_train,
        preprocessing="standardize",
        hyperparameters={"policy_id": "B3"},
        seed=0,
        state=action.b3_models.to_state(),
    ).save(action_b3)
    ModelArtifact.create(
        "action",
        schema=action.b3_no_queue_schema,
        training_sessions=action_train,
        preprocessing="standardize",
        hyperparameters={"policy_id": "B3_NO_QUEUE"},
        seed=0,
        state=action.b3_no_queue_models.to_state(),
    ).save(action_b3_nq)

    # Save support rules.
    support_b3 = layout.freeze_dir / "support_b3.json"
    support_b3_nq = layout.freeze_dir / "support_b3_no_queue.json"
    _save_support_rule(action.b3_support, support_b3)
    _save_support_rule(action.b3_no_queue_support, support_b3_nq)

    model_paths = {
        "price_primary": str(price_primary),
        "price_secondary": str(price_secondary),
        "action_b3": str(action_b3),
        "action_b3_no_queue": str(action_b3_nq),
    }
    support_paths = {"b3": str(support_b3), "b3_no_queue": str(support_b3_nq)}

    # Hash the saved artifact files (both .json and .npz sidecars) and support files.
    artifact_hashes: dict[str, str] = {}
    for name, base in model_paths.items():
        for suffix in (".json", ".npz"):
            fp = Path(base).with_suffix(suffix)
            if fp.exists():
                artifact_hashes[f"{name}{suffix}"] = _sha256_file(fp)
    for name, fp_str in support_paths.items():
        artifact_hashes[f"support_{name}.json"] = _sha256_file(Path(fp_str))

    # Session checksums (reproducibility).
    session_checksums: dict[str, str] = {}
    for sid, d in sorted(split.session_dirs.items()):
        cfile = d / "checksums.json"
        if cfile.exists():
            session_checksums[sid] = _sha256_file(cfile)

    manifest = {
        "data_kind": DATA_KIND,
        "config": cfg.to_dict(),
        "config_hash": config_hash(cfg),
        "primary_horizon_ns": horizon_ns,
        "primary_price_tau_ns": dev.primary_tau_ns,
        "theta_primary": validation.theta_primary,
        "theta_secondary": validation.theta_secondary,
        "epsilon": cfg.epsilon,
        "model_artifact_hashes": artifact_hashes,
        "session_checksums": session_checksums,
        "git_commit": _git_commit(Path(__file__).resolve().parents[3]),
        "frozen_at": datetime.now(UTC).isoformat(),
        "splits": {
            "train": list(split.train),
            "validation": list(split.validation),
            "test": list(split.test),
        },
    }
    layout.freeze_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return _FreezeStage(
        manifest_path=layout.freeze_manifest,
        model_paths=model_paths,
        support_paths=support_paths,
        artifact_hashes=artifact_hashes,
    )


@dataclass(frozen=True, slots=True)
class _TestStage:
    task_results: pl.DataFrame
    decisions: pl.DataFrame
    executions: pl.DataFrame
    tasks: pl.DataFrame
    reports: pl.DataFrame
    quality: pl.DataFrame
    markouts: pl.DataFrame
    b3_decision_log: list[dict[str, Any]]
    b3_no_queue_decision_log: list[dict[str, Any]]
    horizon_sensitivity: pl.DataFrame
    loaded_from_disk: bool


def _build_eval_tasks(
    cfg: StudyConfig,
    split: _Split,
    horizon_ns: int,
    freeze: _FreezeStage,
    validation: _ValidationStage,
) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    # Deterministic ordering: sessions in split order, scenarios in config order.
    for sid in split.test:
        tick = _session_tick(split.session_dirs[sid])
        for scenario_id in cfg.scenarios:
            tasks.append(
                EvalTask(
                    session_dir=str(split.session_dirs[sid]),
                    session_id=sid,
                    scenario_id=scenario_id,
                    planned_scenario_ids=cfg.scenarios,
                    horizon_ns=horizon_ns,
                    epsilon=cfg.epsilon,
                    fee_per_contract_fixed=0,
                    theta_primary=validation.theta_primary,
                    theta_secondary=validation.theta_secondary,
                    tick_size_fixed=tick,
                    price_primary_path=freeze.model_paths["price_primary"],
                    price_secondary_path=freeze.model_paths["price_secondary"],
                    action_b3_path=freeze.model_paths["action_b3"],
                    action_b3_no_queue_path=freeze.model_paths["action_b3_no_queue"],
                    support_b3_path=freeze.support_paths["b3"],
                    support_b3_no_queue_path=freeze.support_paths["b3_no_queue"],
                    include_model_policies=True,
                )
            )
    return tasks


def _run_eval_tasks(cfg: StudyConfig, tasks: list[EvalTask]) -> list[EvalResult]:
    """Run eval tasks, parallel when max_workers > 1, else sequential. Deterministic order."""
    if cfg.max_workers <= 1 or len(tasks) <= 1:
        return [run_eval_task(t) for t in tasks]
    results: list[EvalResult] = [None] * len(tasks)  # type: ignore[list-item]
    # Submit with index so we can restore the deterministic input order regardless of completion.
    with ProcessPoolExecutor(max_workers=cfg.max_workers) as pool:
        futures = {pool.submit(run_eval_task, t): i for i, t in enumerate(tasks)}
        for fut, idx in futures.items():
            results[idx] = fut.result()
    return results


def _horizon_sensitivity(cfg: StudyConfig, split: _Split, primary_h: int) -> pl.DataFrame:
    """At L1 only, run B0 and B1 for every other checkpoint-valid ladder horizon (descriptive)."""
    rows: list[dict[str, Any]] = []
    for horizon in _valid_ladder_horizons(cfg):
        if horizon == primary_h:
            continue
        for sid in split.test:
            config = _experiment_config(cfg, horizon, PRIMARY_LATENCY_ID)
            # B0 + B1 both (descriptive): the frozen L1 models are horizon-specific and are not
            # transferred to other ladder horizons (research spec stage 8).
            engine = SessionEngine(
                split.session_dirs[sid],
                config,
                [B0Policy(), B1Policy()],
                scenario_id=PRIMARY_LATENCY_ID,
                fork_label_probe=False,
                price_scorer=None,
                record_checkpoint_features=False,
            )
            out = engine.run()
            for row in out.task_results.iter_rows(named=True):
                rows.append(
                    {
                        "horizon_ns": horizon,
                        "session_id": sid,
                        "policy_id": row["policy_id"],
                        "status": row["status"],
                        "is_ticks_net": row["is_ticks_net"],
                        "c_t_ticks": row["c_t_ticks"],
                        "note": "models are horizon-specific and not transferred",
                    }
                )
    return (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(
            schema={
                "horizon_ns": pl.Int64,
                "session_id": pl.Utf8,
                "policy_id": pl.Utf8,
                "status": pl.Utf8,
                "is_ticks_net": pl.Float64,
                "c_t_ticks": pl.Float64,
                "note": pl.Utf8,
            }
        )
    )


def _run_test_evaluation(
    cfg: StudyConfig,
    layout: StudyLayout,
    *,
    split: _Split,
    horizon_ns: int,
    freeze: _FreezeStage,
    validation: _ValidationStage,
) -> _TestStage:
    """Stage 8: frozen L1 models, one pass per (test session, scenario); never refit."""
    # Record the first test access (unblinding) with the UTC wall time (append-only, R14).
    append_unblinding_access(
        layout,
        f"first_test_access n_test_sessions={len(split.test)} scenarios={list(cfg.scenarios)}",
    )
    tasks = _build_eval_tasks(cfg, split, horizon_ns, freeze, validation)
    results = _run_eval_tasks(cfg, tasks)

    tr_frames: list[pl.DataFrame] = []
    dec_frames: list[pl.DataFrame] = []
    exec_frames: list[pl.DataFrame] = []
    audit_frames: dict[str, list[pl.DataFrame]] = {
        name: [] for name in ("tasks", "reports", "quality", "markouts")
    }
    b3_log: list[dict[str, Any]] = []
    b3_nq_log: list[dict[str, Any]] = []
    for res in results:
        tr_frames.append(
            _stamp_scenario(frame_from_ipc(res.task_results_ipc), res.scenario_id, horizon_ns)
        )
        dec_frames.append(
            _stamp_scenario(frame_from_ipc(res.decisions_ipc), res.scenario_id, horizon_ns)
        )
        exec_frames.append(
            _stamp_scenario(frame_from_ipc(res.executions_ipc), res.scenario_id, horizon_ns)
        )
        for name, data in (
            ("tasks", res.tasks_ipc),
            ("reports", res.reports_ipc),
            ("quality", res.quality_ipc),
            ("markouts", res.markouts_ipc),
        ):
            audit_frames[name].append(
                _stamp_scenario(frame_from_ipc(data), res.scenario_id, horizon_ns).with_columns(
                    pl.lit(DATA_KIND).alias("data_kind")
                )
            )
        b3_log.extend(res.b3_decision_log)
        b3_nq_log.extend(res.b3_no_queue_decision_log)

    task_results = pl.concat(tr_frames, how="diagonal_relaxed") if tr_frames else pl.DataFrame()
    decisions = pl.concat(dec_frames, how="diagonal_relaxed") if dec_frames else pl.DataFrame()
    executions = pl.concat(exec_frames, how="diagonal_relaxed") if exec_frames else pl.DataFrame()

    sensitivity = _horizon_sensitivity(cfg, split, horizon_ns)
    return _TestStage(
        task_results=task_results,
        decisions=decisions,
        executions=executions,
        tasks=pl.concat(audit_frames["tasks"], how="diagonal_relaxed"),
        reports=pl.concat(audit_frames["reports"], how="diagonal_relaxed"),
        quality=pl.concat(audit_frames["quality"], how="diagonal_relaxed"),
        markouts=pl.concat(audit_frames["markouts"], how="diagonal_relaxed"),
        b3_decision_log=b3_log,
        b3_no_queue_decision_log=b3_nq_log,
        horizon_sensitivity=sensitivity,
        loaded_from_disk=True,
    )


def _stamp_scenario(frame: pl.DataFrame, scenario_id: str, horizon_ns: int) -> pl.DataFrame:
    """Ensure a result frame carries ``scenario_id`` and ``horizon_ns`` (R11).

    The engine (plane A) is expected to stamp these columns on every ``SessionOutputs`` frame.
    Until it does, the study plane adds them here at concatenation using the run's known scenario
    and horizon, and never overwrites a value the engine already provided.
    """
    if frame.height == 0:
        return frame
    additions: list[pl.Expr] = []
    if "scenario_id" not in frame.columns:
        additions.append(pl.lit(scenario_id).alias("scenario_id"))
    if "horizon_ns" not in frame.columns:
        additions.append(pl.lit(int(horizon_ns), dtype=pl.Int64).alias("horizon_ns"))
    return frame.with_columns(additions) if additions else frame


def _causal_trace_for_report(
    cfg: StudyConfig,
    split: _Split,
    horizon_ns: int,
    freeze: _FreezeStage,
    validation: _ValidationStage,
) -> list[dict[str, Any]]:
    """Produce one causal trace from a test task under the frozen B3 policy, L1 (R19).

    Runs a single frozen ``(first test session, L1)`` engine pass with the six policies (loading
    the frozen artifacts from disk, never refitting) and returns the ordered causal trace of the
    first task that B3 traced. Returns an empty list if there is no test session or traced task.
    """
    if not split.test:
        return []
    sid = split.test[0]
    append_unblinding_access(
        StudyLayout(freeze.manifest_path.parent.parent),
        f"report_trace session={sid} scenario=L1 horizon_ns={horizon_ns}",
    )
    tick = _session_tick(split.session_dirs[sid])
    primary = _load_price_model(freeze.model_paths["price_primary"])
    secondary = _load_price_model(freeze.model_paths["price_secondary"])
    scorer = PriceSignalScorer(primary, secondary)
    action_b3 = _load_action_models(freeze.model_paths["action_b3"])
    action_b3_nq = _load_action_models(freeze.model_paths["action_b3_no_queue"])
    support_b3 = load_support_rule(freeze.support_paths["b3"])
    support_b3_nq = load_support_rule(freeze.support_paths["b3_no_queue"])
    b3 = B3Policy(action_b3, action_b3.schema, support_b3, cfg.epsilon, "B3", tick_size_fixed=tick)
    b3_nq = B3Policy(
        action_b3_nq,
        action_b3_nq.schema,
        support_b3_nq,
        cfg.epsilon,
        "B3_NO_QUEUE",
        tick_size_fixed=tick,
    )
    policies: list[Policy] = [
        B0Policy(),
        B1Policy(),
        B2Policy(validation.theta_primary, "u_signal", "B2"),
        B2Policy(validation.theta_secondary, "u_signal_100ms", "B2_100MS"),
        b3,
        b3_nq,
    ]
    config = _experiment_config(cfg, horizon_ns, PRIMARY_LATENCY_ID)
    engine = SessionEngine(
        split.session_dirs[sid],
        config,
        policies,
        scenario_id=PRIMARY_LATENCY_ID,
        fork_label_probe=False,
        price_scorer=scorer,
        record_checkpoint_features=False,
    )
    outputs = engine.run()
    task_ids = outputs.tasks.get_column("task_id").to_list() if outputs.tasks.height else []
    for task_id in task_ids:
        trace = outputs.trace(str(task_id), "B3")
        if trace:
            return trace
    # Fall back to a B1 trace so the report always has a concrete world to show.
    for task_id in task_ids:
        trace = outputs.trace(str(task_id), "B1")
        if trace:
            return trace
    return []


def _support_table_frame(gate: SupportGateResult) -> pl.DataFrame:
    rows = [
        {
            "data_kind": DATA_KIND,
            "horizon_ns": h.horizon_ns,
            "passes": h.passes,
            "min_eligible": h.min_eligible,
            "min_queue_depletion": h.min_queue_depletion,
            "n_pilot_sessions": h.n_pilot_sessions,
            "support_min_eligible": gate.support_min_eligible,
            "support_min_queue_depletion": gate.support_min_queue_depletion,
        }
        for h in gate.horizons
    ]
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def _write_support_gate(layout: StudyLayout, gate: SupportGateResult) -> None:
    layout.support_gate_json.write_text(
        json.dumps(
            {
                "data_kind": DATA_KIND,
                "verdict": gate.verdict,
                "chosen_horizon_ns": gate.chosen_horizon_ns,
                "support_min_eligible": gate.support_min_eligible,
                "support_min_queue_depletion": gate.support_min_queue_depletion,
                "horizons": [
                    {
                        "horizon_ns": h.horizon_ns,
                        "passes": h.passes,
                        "min_eligible": h.min_eligible,
                        "min_queue_depletion": h.min_queue_depletion,
                        "per_session_eligible": h.per_session_eligible,
                        "per_session_queue_depletion": h.per_session_queue_depletion,
                    }
                    for h in gate.horizons
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _variant_reason_counts(decision_log: list[dict[str, Any]]) -> dict[str, int]:
    """Count eligible-checkpoint decisions by reason for one B3 variant.

    The decision log holds exactly the eligible checkpoints (the engine calls the B3 policy only
    for a ``MODEL_CHOICE`` eligibility verdict), so its reasons are ``MODEL_CHOICE``,
    ``FALLBACK_UNSUPPORTED``, or ``FALLBACK_NONFINITE``. Ineligible checkpoints never reach the
    policy and are reported separately from the task-result reasons.
    """
    counts = {
        DecisionReason.MODEL_CHOICE.value: 0,
        DecisionReason.FALLBACK_UNSUPPORTED.value: 0,
        DecisionReason.FALLBACK_NONFINITE.value: 0,
    }
    for row in decision_log:
        reason = str(row["reason"])
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _ineligible_reason_counts(task_results: pl.DataFrame, policy_id: str) -> dict[str, int]:
    """Count ineligible-checkpoint reasons for a variant from its persisted task-result rows.

    A checkpoint is ineligible when ``checkpoint_eligible`` is false; its ``decision_reason`` is
    one of the ``INELIGIBLE_*`` / ``REPORTED_COMPLETE`` / ``NOT_APPLICABLE`` codes. Reported for
    transparency so the eligible denominator is never silently inflated or hidden.
    """
    if task_results.height == 0:
        return {}
    rows = task_results.filter(
        (pl.col("policy_id") == policy_id)
        & (pl.col("scenario_id") == PRIMARY_LATENCY_ID)
        & (~pl.col("checkpoint_eligible"))
    )
    if rows.height == 0:
        return {}
    grouped = rows.group_by("decision_reason").agg(pl.len().alias("n"))
    return {str(r["decision_reason"]): int(r["n"]) for r in grouped.iter_rows(named=True)}


def _variant_decisions_by_task(
    decision_log: list[dict[str, Any]],
) -> dict[tuple[str, int, str], str]:
    """Map each eligible checkpoint (task_id, time_ns, scenario_id) -> chosen action.

    The scenario id is part of the key so that decisions from different latency scenarios (e.g.
    L1 and L6) never collide (R11): the same task's checkpoint occurs in every scenario world.
    """
    return {
        (str(r["task_id"]), int(r["time_ns"]), str(r.get("scenario_id", PRIMARY_LATENCY_ID))): str(
            r["choice"]
        )
        for r in decision_log
    }


def _primary_scenario_log(decision_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only primary-scenario (L1) decisions for the primary diagnostics (R11).

    Rows without a ``scenario_id`` are treated as primary (older engine outputs that did not
    stamp the scenario); rows for other scenarios are excluded from the primary comparison so
    appending e.g. L6 decisions cannot change the primary disagreement rate.
    """
    return [
        r
        for r in decision_log
        if str(r.get("scenario_id", PRIMARY_LATENCY_ID)) == PRIMARY_LATENCY_ID
    ]


def _degeneracy_guardrails(
    b3_log: list[dict[str, Any]],
    b3_nq_log: list[dict[str, Any]],
    task_results: pl.DataFrame,
) -> dict[str, Any]:
    """Compute degeneracy guardrails for the primary B3 vs B3_NO_QUEUE comparison (stage 9c).

    Reports, for each variant: eligible-decision reason counts, ineligible reason counts, the
    number of eligible checkpoints, and the fraction decided by the model (MODEL_CHOICE share).
    Also reports the B3 vs B3_NO_QUEUE action-disagreement rate on the eligible checkpoints both
    variants decided (keyed by task + checkpoint time).

    Sets ``degenerate_primary_comparison = True`` with a reason string when either variant's
    model-choice fraction is below 0.5, OR when the two variants agree on 100% of their shared
    eligible checkpoints AND both are dominated by fallbacks (model-choice fraction < 0.5 for
    both). The flag is always present (never hidden), defaulting to ``False``.

    All computations use the **primary** latency scenario (L1) decisions only (R11); appending
    other scenarios' decisions cannot change the primary comparison.
    """
    b3_log = _primary_scenario_log(b3_log)
    b3_nq_log = _primary_scenario_log(b3_nq_log)
    b3_counts = _variant_reason_counts(b3_log)
    nq_counts = _variant_reason_counts(b3_nq_log)
    b3_eligible = len(b3_log)
    nq_eligible = len(b3_nq_log)
    mc = DecisionReason.MODEL_CHOICE.value
    b3_model_frac = (b3_counts[mc] / b3_eligible) if b3_eligible else 0.0
    nq_model_frac = (nq_counts[mc] / nq_eligible) if nq_eligible else 0.0
    # Support rate = fraction of eligible decisions whose feature vector was in the frozen
    # support region (``supported=True``), distinct from the model-choice fraction (a supported
    # decision can still fall back on a nonfinite prediction, and vice versa).
    b3_support_rate = (
        sum(1 for r in b3_log if bool(r["supported"])) / b3_eligible if b3_eligible else 0.0
    )
    nq_support_rate = (
        sum(1 for r in b3_nq_log if bool(r["supported"])) / nq_eligible if nq_eligible else 0.0
    )

    # Action disagreement on the shared eligible checkpoints (same task + checkpoint time).
    b3_map = _variant_decisions_by_task(b3_log)
    nq_map = _variant_decisions_by_task(b3_nq_log)
    shared = sorted(set(b3_map) & set(nq_map))
    n_shared = len(shared)
    n_disagree = sum(1 for key in shared if b3_map[key] != nq_map[key])
    disagreement_rate = (n_disagree / n_shared) if n_shared else 0.0

    reasons: list[str] = []
    if b3_eligible > 0 and b3_model_frac < 0.5:
        reasons.append(
            f"B3 model-choice fraction {b3_model_frac:.3f} < 0.5 (dominated by fallbacks)"
        )
    if nq_eligible > 0 and nq_model_frac < 0.5:
        reasons.append(
            f"B3_NO_QUEUE model-choice fraction {nq_model_frac:.3f} < 0.5 (dominated by fallbacks)"
        )
    identical = n_shared > 0 and n_disagree == 0
    both_fallback_dominated = (
        b3_eligible > 0 and nq_eligible > 0 and b3_model_frac < 0.5 and nq_model_frac < 0.5
    )
    if identical and both_fallback_dominated:
        reasons.append(
            "B3 and B3_NO_QUEUE decisions identical on 100% of shared eligible checkpoints "
            "while both are dominated by fallbacks (comparison degenerate by construction)"
        )
    # A total absence of eligible checkpoints for either variant is also degenerate.
    if b3_eligible == 0 or nq_eligible == 0:
        reasons.append("one or both variants have zero eligible checkpoints on test")

    return {
        "b3": {
            "n_eligible_checkpoints": b3_eligible,
            "reason_counts": b3_counts,
            "ineligible_reason_counts": _ineligible_reason_counts(task_results, "B3"),
            "model_choice_fraction": b3_model_frac,
            "support_rate": b3_support_rate,
        },
        "b3_no_queue": {
            "n_eligible_checkpoints": nq_eligible,
            "reason_counts": nq_counts,
            "ineligible_reason_counts": _ineligible_reason_counts(task_results, "B3_NO_QUEUE"),
            "model_choice_fraction": nq_model_frac,
            "support_rate": nq_support_rate,
        },
        "action_disagreement_rate": disagreement_rate,
        "n_shared_eligible_checkpoints": n_shared,
        "n_action_disagreements": n_disagree,
        "degenerate_primary_comparison": bool(reasons),
        "degeneracy_reason": "; ".join(reasons) if reasons else None,
    }


def _effect_block(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "cost_effect_ticks": record["cost_effect_ticks"],
        "cost_ci": [record["cost_ci_lo"], record["cost_ci_hi"]],
        "cost_n_sessions_defined": record["cost_n_sessions_defined"],
        "cost_n_sessions_undefined": record["cost_n_sessions_undefined"],
        "cost_n_tasks": record["cost_n_tasks"],
        "miss_diff": record["miss_diff"],
        "miss_ci": [record["miss_ci_lo"], record["miss_ci_hi"]],
        "miss_n_sessions_defined": record["miss_n_sessions_defined"],
        "miss_n_sessions_undefined": record["miss_n_sessions_undefined"],
        "miss_n_tasks": record["miss_n_tasks"],
        "zero_event_upper_bound": record["zero_event_upper_bound"],
    }


def _build_metrics(
    cfg: StudyConfig,
    *,
    split: _Split,
    horizon_ns: int,
    validation: _ValidationStage,
    tables: dict[str, pl.DataFrame],
    test: _TestStage,
    freeze: _FreezeStage,
    degeneracy: dict[str, Any],
) -> dict[str, Any]:
    primary = tables["primary_pair"].to_dicts()[0]
    return {
        "data_kind": DATA_KIND,
        "study_id": cfg.study_id,
        "primary_horizon_ns": horizon_ns,
        "theta_primary": validation.theta_primary,
        "theta_secondary": validation.theta_secondary,
        "epsilon": cfg.epsilon,
        "support_margin_frac": cfg.support_margin_frac,
        "degeneracy_guardrails": degeneracy,
        "n_sessions": {
            "train": len(split.train),
            "validation": len(split.validation),
            "test": len(split.test),
        },
        "scenarios": list(cfg.scenarios),
        "primary_pair": {
            "baseline": primary["baseline"],
            "candidate": primary["candidate"],
            **_effect_block(primary),
        },
        "loaded_models_from_disk": test.loaded_from_disk,
        "freeze_manifest": str(freeze.manifest_path),
    }


def _write_tables_csv(layout: StudyLayout, tables: dict[str, pl.DataFrame]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for name, frame in tables.items():
        path = layout.tables_dir / f"{name}.csv"
        if frame.height == 0:
            # Write an empty file with a data_kind marker so every table file exists.
            path.write_text(f"data_kind,{DATA_KIND}\n")
        else:
            frame.write_csv(path)
        paths[name] = str(path)
    return paths


def run_study(out_dir: Path, cfg: StudyConfig) -> StudyResult:
    """Execute the full research study under ``out_dir`` with the run-directory protocol (R13).

    Run-directory protocol:

    * Every run requires a new or empty directory, including retries with identical config.
      Existing files, completed freezes and access history are never deleted or replaced.
    * ``status.json`` records ``RUNNING`` at start and transitions to ``COMPLETE`` on success,
      ``STOPPED_SUPPORT_GATE`` when the G2-S gate fails (an expected non-error stop), or
      ``FAILED`` when any stage raises (the exception is re-raised).
    * COMPLETE status seals the saved configuration, frozen inputs and result artifacts.
    """
    out_dir = Path(out_dir)
    layout = StudyLayout(out_dir)
    cfg.validate()
    cfg_hash = config_hash(cfg)

    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise RunDirectoryError(
            f"run directory {out_dir} is not empty; refusing to overwrite any run history. "
            "Use a fresh --out directory, including retries with the same configuration."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    # Exclusive ownership prevents two processes that both observed an empty directory
    # from beginning a run in the same location. Retain the claim as part of run history.
    try:
        with (out_dir / ".run-claim").open("x", encoding="utf-8") as handle:
            handle.write(cfg_hash + "\n")
    except FileExistsError as exc:
        raise RunDirectoryError("another run claimed this directory; use a fresh --out") from exc
    layout.ensure_dirs()
    write_status(layout, StudyStatus.RUNNING, cfg_hash)
    try:
        result = _run_study_body(out_dir, cfg, layout)
    except Exception as exc:
        write_status(layout, StudyStatus.FAILED, cfg_hash, detail=f"{type(exc).__name__}: {exc}")
        raise
    if result.support_verdict != "PASSED":
        write_status(
            layout,
            StudyStatus.STOPPED_SUPPORT_GATE,
            cfg_hash,
            detail=f"support gate verdict {result.support_verdict}",
        )
    else:
        write_status(layout, StudyStatus.COMPLETE, cfg_hash)
    return result


def _run_study_body(out_dir: Path, cfg: StudyConfig, layout: StudyLayout) -> StudyResult:
    """Execute the full research study, writing all artifacts under ``out_dir``.

    Stages are logged implicitly by their on-disk artifacts. Returns a :class:`StudyResult` with
    the output paths and the headline numbers.
    """
    started = time.perf_counter()
    layout.config_json.write_text(
        json.dumps({"data_kind": DATA_KIND, **cfg.to_dict()}, indent=2, sort_keys=True)
    )

    # Stage 1: data + chronological split.
    session_paths = _generate_or_reuse_sessions(cfg, layout)
    split = _chronological_split(cfg, session_paths)

    # Stage 2: support gate (counts only).
    gate = _run_support_gate(cfg, split)
    _write_support_gate(layout, gate)
    if gate.verdict != "PASSED" or gate.chosen_horizon_ns is None:
        # The study stops with a written FAILED verdict and no further stages.
        return StudyResult(
            out_dir=out_dir,
            support_verdict=gate.verdict,
            primary_horizon_ns=None,
            theta_primary=None,
            theta_secondary=None,
            metrics={"data_kind": DATA_KIND, "support_verdict": gate.verdict},
            table_paths={},
            freeze_manifest_path=None,
            loaded_models_from_disk=False,
        )
    horizon_ns = gate.chosen_horizon_ns

    # Stage 3: development labels.
    dev = _run_development(cfg, split, horizon_ns)

    # Stage 4: price models (forward-chained OOF + final).
    price = _run_price_models(cfg, split, dev)

    # Stage 5: action models.
    action = _run_action_models(cfg, split, dev, price)
    layout.development_json.write_text(
        json.dumps(
            {
                "data_kind": DATA_KIND,
                "primary_horizon_ns": horizon_ns,
                "primary_price_tau_ns": dev.primary_tau_ns,
                "n_price_label_none": dev.n_price_label_none,
                "oof_scored_sessions": list(price.oof_scored_sessions),
                "oof_unscored_sessions": list(price.oof_unscored_sessions),
                "oof_train_index_log": price.train_index_log,
                "action_n_train_rows": action.n_train_rows,
                "action_excluded_sessions_no_oof": list(action.excluded_sessions_no_oof),
                "action_diagnostics": action.diagnostics,
            },
            indent=2,
            sort_keys=True,
        )
    )

    # Stage 6: validation threshold selection.
    validation = _run_validation_selection(cfg, split, price, horizon_ns)
    layout.validation_json.write_text(
        json.dumps(
            {
                "data_kind": DATA_KIND,
                "epsilon": cfg.epsilon,
                "theta_primary": validation.theta_primary,
                "theta_secondary": validation.theta_secondary,
                "primary_trials": validation.primary_trials,
                "secondary_trials": validation.secondary_trials,
            },
            indent=2,
            sort_keys=True,
        )
    )

    # Stage 7: freeze.
    freeze = _run_freeze(
        cfg,
        layout,
        split=split,
        horizon_ns=horizon_ns,
        price=price,
        action=action,
        validation=validation,
        dev=dev,
    )

    # Stage 8: test evaluation (frozen L1 models; parallel/sequential).
    test = _run_test_evaluation(
        cfg,
        layout,
        split=split,
        horizon_ns=horizon_ns,
        freeze=freeze,
        validation=validation,
    )

    # Stage 9: analysis tables.
    support_table = _support_table_frame(gate)
    # Primary diagnostics (epsilon sensitivity) read the primary-scenario (L1) decisions only, so
    # appending other-scenario decisions cannot shift the primary numbers (R11).
    primary_b3_log = _primary_scenario_log(test.b3_decision_log)
    tables = build_all_tables(
        test.task_results,
        scenarios=cfg.scenarios,
        bootstrap_n=cfg.bootstrap_n,
        bootstrap_seed=cfg.bootstrap_seed,
        b3_decision_log=primary_b3_log,
        epsilon_set=cfg.epsilon_sensitivity,
        primary_epsilon=cfg.epsilon,
        support_table=support_table,
    )
    tables["markouts"] = summarize_markouts(test.markouts)

    # Stage 10: outputs.
    test.task_results.write_parquet(layout.results_dir / "task_results.parquet")
    test.decisions.write_parquet(layout.results_dir / "decisions.parquet")
    test.executions.write_parquet(layout.results_dir / "executions.parquet")
    test.tasks.write_parquet(layout.results_dir / "tasks.parquet")
    test.reports.write_parquet(layout.results_dir / "reports.parquet")
    test.quality.write_parquet(layout.results_dir / "quality.parquet")
    test.markouts.write_parquet(layout.results_dir / "markouts.parquet")
    _dev_branch_frame(dev).write_parquet(layout.results_dir / "branch_labels_dev.parquet")
    # Both summaries are computed at L1 only (support gate; horizon sensitivity). Stamp the
    # scenario explicitly so every results table carries scenario identity (remediation R11).
    _with_scenario(support_table, PRIMARY_LATENCY_ID).write_parquet(
        layout.results_dir / "support.parquet"
    )
    _with_scenario(test.horizon_sensitivity, PRIMARY_LATENCY_ID).write_parquet(
        layout.results_dir / "horizon_sensitivity.parquet"
    )

    table_paths = _write_tables_csv(layout, tables)
    degeneracy = _degeneracy_guardrails(
        test.b3_decision_log, test.b3_no_queue_decision_log, test.task_results
    )
    metrics = _build_metrics(
        cfg,
        split=split,
        horizon_ns=horizon_ns,
        validation=validation,
        tables=tables,
        test=test,
        freeze=freeze,
        degeneracy=degeneracy,
    )
    resources = {
        "data_kind": DATA_KIND,
        "runtime_to_analysis_seconds": time.perf_counter() - started,
        "generated_session_bytes": sum(
            p.stat().st_size for p in layout.sessions_dir.rglob("*") if p.is_file()
        ),
        "configured_workers": cfg.max_workers,
        "hardware_threads": os.cpu_count(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "test_task_results": test.task_results.height,
        "peak_memory": "not measured",
    }
    metrics["resources"] = resources
    (layout.results_dir / "resources.json").write_text(
        json.dumps(resources, indent=2, sort_keys=True)
    )
    layout.metrics_json.write_text(json.dumps(metrics, indent=2, sort_keys=True))

    causal_trace = _causal_trace_for_report(cfg, split, horizon_ns, freeze, validation)
    report_inputs = {
        "data_kind": DATA_KIND,
        "config": cfg.to_dict(),
        "metrics": metrics,
        "degeneracy_guardrails": degeneracy,
        "tables": {name: frame.to_dicts() for name, frame in tables.items()},
        "theta_primary": validation.theta_primary,
        "theta_secondary": validation.theta_secondary,
        "primary_horizon_ns": horizon_ns,
        # R19: persist the B3/B3_NO_QUEUE decision logs (p/v/support/choice/reason/scenario) so
        # epsilon sensitivity can be recomputed and the frozen B3 can be traced without replay.
        "b3_decision_log": test.b3_decision_log,
        "b3_no_queue_decision_log": test.b3_no_queue_decision_log,
        # R19: one causal trace from a test task (frozen B3, L1), stored for the report.
        "causal_trace": causal_trace,
        "markout_summary": tables["markouts"].to_dicts(),
        # R20: which features most often trigger FALLBACK_UNSUPPORTED (validation-set counts).
        "unsupported_feature_counts": {
            "b3": action.diagnostics.get("unsupported_feature_counts_b3", {}),
            "b3_no_queue": action.diagnostics.get("unsupported_feature_counts_b3_no_queue", {}),
        },
        # R16: validation calibration/error diagnostics (miss Brier/log loss, cost RMSE) per
        # action and variant, plus the shared-rule action disagreement rate.
        "validation_diagnostics": {
            "action_disagreement_rate": action.diagnostics.get("action_disagreement_rate"),
            "calibration": action.diagnostics.get("calibration", {}),
            "price_validation": action.diagnostics.get("price_validation", {}),
        },
    }
    layout.report_inputs_json.write_text(json.dumps(report_inputs, indent=2, sort_keys=True))

    return StudyResult(
        out_dir=out_dir,
        support_verdict=gate.verdict,
        primary_horizon_ns=horizon_ns,
        theta_primary=validation.theta_primary,
        theta_secondary=validation.theta_secondary,
        metrics=metrics,
        table_paths={k: Path(v) for k, v in table_paths.items()},
        freeze_manifest_path=freeze.manifest_path,
        loaded_models_from_disk=test.loaded_from_disk,
    )


def _dev_branch_frame(dev: _DevelopmentData) -> pl.DataFrame:
    frames = [f for f in dev.branch_labels.values() if f.height > 0]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _print_quick_degeneracy_diagnostics() -> None:
    """R20 diagnostic: run the quick preset in %TEMP% and print the B3 degeneracy evidence.

    Reports the B3 / B3_NO_QUEUE model-choice fractions and the degeneracy flag from a quick
    study. If the comparison is degenerate it is reported honestly (support is never redefined to
    hide it). This is a decision-with-evidence aid for R20, not part of a study run.
    """
    tmp = Path(tempfile.mkdtemp(prefix="qexec-r20-"))
    cfg = StudyConfig.quick_preset()
    result = run_study(tmp, cfg)
    guard = (
        result.metrics.get("degeneracy_guardrails", {}) if isinstance(result.metrics, dict) else {}
    )
    print("R20 quick-study degeneracy diagnostics (SYNTHETIC):")
    print(f"  out_dir: {tmp}")
    for variant in ("b3", "b3_no_queue"):
        block = guard.get(variant, {})
        print(
            f"  {variant}: model_choice_fraction="
            f"{block.get('model_choice_fraction')} support_rate={block.get('support_rate')} "
            f"n_eligible={block.get('n_eligible_checkpoints')} reasons={block.get('reason_counts')}"
        )
    print(f"  action_disagreement_rate: {guard.get('action_disagreement_rate')}")
    print(f"  degenerate_primary_comparison: {guard.get('degenerate_primary_comparison')}")
    print(f"  degeneracy_reason: {guard.get('degeneracy_reason')}")


if __name__ == "__main__":  # pragma: no cover - manual R20 diagnostic
    _print_quick_degeneracy_diagnostics()
