"""``qexec demo`` -- end-to-end synthetic study + Markdown report (CI smoke stage).

This is the exact command the CI smoke stage runs::

    qexec demo --out <dir> --sessions 2 --quick

Because the quick study preset requires at least ``MIN_QUICK_SESSIONS`` sessions (min one per
chronological split), a smaller ``--sessions`` is raised to the required minimum and the override
is announced in the output (never silently). The command runs :func:`run_study` then renders the
Markdown research report via :func:`qexec.analysis.report.write_report`. All data is SYNTHETIC.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from qexec.analysis.report import write_report
from qexec.cli.commands import SYNTHETIC_BANNER, CommandError, CommandUsageError
from qexec.experiments import StudyConfig, run_study

# Honest forward-chained OOF fitting needs at least two training sessions.
MIN_QUICK_SESSIONS = 4
MIN_FULL_SESSIONS = 4


def run_demo(args: argparse.Namespace) -> int:
    """Run an end-to-end synthetic study and render its Markdown report under ``--out``."""
    out_dir = Path(args.out)
    requested_sessions: int = args.sessions
    quick: bool = getattr(args, "quick", False)

    if requested_sessions < 1:
        raise CommandUsageError("--sessions must be >= 1")

    if quick:
        cfg = StudyConfig.quick_preset(study_id="demo")
        required = max(MIN_QUICK_SESSIONS, cfg.n_sessions)
    else:
        cfg = StudyConfig(study_id="demo")
        required = MIN_FULL_SESSIONS

    effective_sessions = max(requested_sessions, required)
    preset_pilots = cfg.n_pilot_sessions
    cfg = dataclasses.replace(cfg, n_sessions=effective_sessions, n_pilot_sessions=1)
    cfg = dataclasses.replace(cfg, n_pilot_sessions=min(preset_pilots, cfg.split_counts[0]))

    print(SYNTHETIC_BANNER)
    print(f"QExec demo: end-to-end SYNTHETIC study + report into {out_dir}")
    if effective_sessions != requested_sessions:
        print(
            f"  NOTE: requested --sessions {requested_sessions} is below the minimum "
            f"{required} required by the {'quick' if quick else 'full'} preset "
            f"(two train, one validation and one test); using {effective_sessions} sessions."
        )
    if cfg.n_pilot_sessions != preset_pilots:
        print(
            f"  NOTE: using {cfg.n_pilot_sessions} pilot training sessions instead of preset "
            f"{preset_pilots}, so pilots stay entirely within the training split."
        )
    print(
        f"  config: {cfg.n_sessions} sessions x {cfg.duration_s}s, "
        f"scenarios {list(cfg.scenarios)}, quick={cfg.quick}"
    )

    try:
        result = run_study(out_dir, cfg)
    except Exception as exc:
        raise CommandError(f"demo study failed: {exc}") from exc

    print(f"  support verdict: {result.support_verdict}")
    print(f"  primary horizon: {result.primary_horizon_ns} ns")

    try:
        report_path = write_report(out_dir)
    except Exception as exc:
        raise CommandError(f"report rendering failed: {exc}") from exc

    print(f"  report written:  {report_path}")
    print("Demo complete. All outputs are SYNTHETIC software validation (not a market finding).")
    return 0
