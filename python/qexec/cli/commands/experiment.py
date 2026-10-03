"""``qexec experiment run`` -- run the full research study (:func:`run_study`).

Builds a :class:`~qexec.experiments.StudyConfig` from ``--quick`` and/or a ``--config FILE.json``
(with optional ``--sessions`` / ``--workers`` overrides), runs the 10-stage study, and prints the
headline numbers (support verdict, primary horizon, selected thetas, primary pair effect). All
results are SYNTHETIC software validation.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any

from qexec.cli.commands import SYNTHETIC_BANNER, CommandError, CommandUsageError
from qexec.experiments import StudyConfig, run_study
from qexec.experiments.config import DATA_KIND


def build_study_config(args: argparse.Namespace) -> StudyConfig:
    """Resolve a :class:`StudyConfig` from ``--quick`` / ``--config`` plus overrides.

    ``--config`` (a JSON object) and ``--quick`` are mutually usable: ``--quick`` provides the
    base preset and the config file (if given) overrides individual keys. ``--sessions`` and
    ``--workers`` are last-wins overrides on top of either.
    """
    base: StudyConfig
    if getattr(args, "quick", False):
        base = StudyConfig.quick_preset()
    else:
        base = StudyConfig(study_id="study")

    if getattr(args, "config", None):
        config_path = Path(args.config)
        if not config_path.is_file():
            raise CommandUsageError(f"--config file not found: {config_path}")
        try:
            raw: dict[str, Any] = json.loads(config_path.read_text())
        except (OSError, ValueError) as exc:
            raise CommandUsageError(f"--config is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise CommandUsageError("--config must contain a JSON object")
        data_kind = raw.pop("data_kind", DATA_KIND)
        if data_kind != DATA_KIND:
            raise CommandUsageError("this study entry point accepts only SYNTHETIC data_kind")
        # Merge onto the base preset's dict so a partial config is allowed.
        merged = {**base.to_dict(), **raw}
        try:
            base = StudyConfig.from_dict(merged)
        except (ValueError, TypeError) as exc:
            raise CommandUsageError(f"invalid study configuration: {exc}") from exc

    overrides: dict[str, Any] = {}
    if getattr(args, "sessions", None) is not None:
        if args.sessions < 4:
            raise CommandUsageError("--sessions must be >= 4 (two train, one validation, one test)")
        overrides["n_sessions"] = args.sessions
    if getattr(args, "workers", None) is not None:
        if args.workers < 1:
            raise CommandUsageError("--workers must be >= 1")
        overrides["max_workers"] = args.workers
    if overrides:
        try:
            base = dataclasses.replace(base, **overrides)
        except (ValueError, TypeError) as exc:
            raise CommandUsageError(f"invalid override: {exc}") from exc
    return base


def _print_result(result: Any) -> None:
    print(f"  support verdict:   {result.support_verdict}")
    print(f"  primary horizon:   {result.primary_horizon_ns} ns")
    print(f"  theta primary:     {result.theta_primary}")
    print(f"  theta secondary:   {result.theta_secondary}")
    primary = result.metrics.get("primary_pair") if isinstance(result.metrics, dict) else None
    if primary:
        print(
            f"  primary pair {primary.get('baseline')} vs {primary.get('candidate')}: "
            f"cost_effect_ticks={primary.get('cost_effect_ticks')} "
            f"(n_tasks={primary.get('cost_n_tasks')})"
        )
    print(f"  outputs under:     {result.out_dir}")


def run_experiment_run(args: argparse.Namespace) -> int:
    """Run the study and print the headline numbers."""
    out_dir = Path(args.out)
    cfg = build_study_config(args)

    print(SYNTHETIC_BANNER)
    print(
        f"Running SYNTHETIC study {cfg.study_id!r}: {cfg.n_sessions} sessions x {cfg.duration_s}s, "
        f"scenarios {list(cfg.scenarios)}, workers {cfg.max_workers}"
    )
    try:
        result = run_study(out_dir, cfg)
    except Exception as exc:
        raise CommandError(f"study failed: {exc}") from exc

    _print_result(result)
    if result.support_verdict != "PASSED":
        # R13: a support-gate stop is a non-success outcome; the CLI must exit nonzero with a
        # clear message rather than printing "Done" and exiting 0.
        raise CommandError(f"study stopped: support gate {result.support_verdict}")
    print("Done. All results are SYNTHETIC software validation (not a market finding).")
    return 0
