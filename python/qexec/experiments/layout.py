"""On-disk layout for a study run (research spec section 2, stage 10).

A single study writes everything under ``out_dir``::

    out_dir/
      config.json                 StudyConfig round-trip (+ data_kind)
      status.json                 lifecycle and immutable completion artifact hashes
      sessions/                   synthetic sessions SYN-0001.. with source/parameter provenance
      stages/
        support_gate.json         G2-S verdict and per-horizon support (counts only)
        development.json          label/model-fit provenance and diagnostics
        validation.json           theta trial logs for B2 and B2_100MS
      freeze/
        manifest.json             config, primary H, theta selections, hashes, git, frozen_at
        unblinding.log            test evaluation and every later trace access (append-only)
        price_primary.json/.npz   frozen model artifacts (ModelArtifact pairs)
        price_secondary.json/.npz
        action_b3.json/.npz
        action_b3_no_queue.json/.npz
        support_b3.json           SupportRule state
        support_b3_no_queue.json
      results/
        task_results.parquet  decisions.parquet  executions.parquet
        branch_labels_dev.parquet  support.parquet
        metrics.json              headline numbers with denominators
        report_inputs.json        everything the report needs
        tables/*.csv              analysis tables
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["StudyLayout"]


@dataclass(frozen=True, slots=True)
class StudyLayout:
    """Resolved directory paths for a study ``out_dir``."""

    root: Path

    @property
    def config_json(self) -> Path:
        return self.root / "config.json"

    @property
    def status_json(self) -> Path:
        return self.root / "status.json"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def stages_dir(self) -> Path:
        return self.root / "stages"

    @property
    def support_gate_json(self) -> Path:
        return self.stages_dir / "support_gate.json"

    @property
    def development_json(self) -> Path:
        return self.stages_dir / "development.json"

    @property
    def validation_json(self) -> Path:
        return self.stages_dir / "validation.json"

    @property
    def freeze_dir(self) -> Path:
        return self.root / "freeze"

    @property
    def freeze_manifest(self) -> Path:
        return self.freeze_dir / "manifest.json"

    @property
    def unblinding_log(self) -> Path:
        return self.freeze_dir / "unblinding.log"

    @property
    def results_dir(self) -> Path:
        return self.root / "results"

    @property
    def tables_dir(self) -> Path:
        return self.results_dir / "tables"

    @property
    def metrics_json(self) -> Path:
        return self.results_dir / "metrics.json"

    @property
    def report_inputs_json(self) -> Path:
        return self.results_dir / "report_inputs.json"

    def model_path(self, name: str) -> Path:
        """Base path (no suffix) for a frozen model artifact under ``freeze/``."""
        return self.freeze_dir / name

    def ensure_dirs(self) -> None:
        for d in (
            self.root,
            self.sessions_dir,
            self.stages_dir,
            self.freeze_dir,
            self.results_dir,
            self.tables_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
