"""``qexec analysis report`` -- render the Markdown research report for a finished study.

Calls :func:`qexec.analysis.report.write_report`, which reads the study's ``metrics.json`` /
``report_inputs.json`` / tables CSVs and writes ``<study>/report/report.md``. All results are
SYNTHETIC software validation.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from qexec.analysis.report import write_report
from qexec.cli.commands import SYNTHETIC_BANNER, CommandError, CommandUsageError


def run_analysis_report(args: argparse.Namespace) -> int:
    """Render ``<study>/report/report.md`` from the persisted study artifacts."""
    study_dir = Path(args.study)
    if not study_dir.is_dir():
        raise CommandUsageError(f"--study must be an existing directory, got {study_dir}")

    print(SYNTHETIC_BANNER)
    print(f"Rendering research report for SYNTHETIC study {study_dir}")
    try:
        report_path = write_report(study_dir)
    except FileNotFoundError as exc:
        raise CommandError(
            f"study artifacts missing (run 'qexec experiment run' first): {exc}"
        ) from exc
    except Exception as exc:
        raise CommandError(f"report rendering failed: {exc}") from exc

    print(f"  report written: {report_path}")
    print("Done. The report is SYNTHETIC software validation (not a market finding).")
    return 0
