"""``qexec tasks build`` -- build the policy-independent task manifest for a session.

Runs :func:`qexec.sim.tasks.build_task_manifest` for one session at a chosen horizon and prints
a manifest summary (counts of eligible/ineligible tasks, per-side counts, ineligibility reasons,
corrupting-window count, and the manifest id). All data is SYNTHETIC.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from qexec.cli.commands import SYNTHETIC_BANNER, CommandError, CommandUsageError
from qexec.core.config import (
    LATENCY_SCENARIOS,
    PRIMARY_LATENCY_ID,
    ExperimentConfig,
    checkpoint_valid,
)
from qexec.sim.tasks import build_task_manifest

_MS_NS = 1_000_000
_DEFAULT_HORIZON_NS = 1_000_000_000  # 1 s


def run_tasks_build(args: argparse.Namespace) -> int:
    """Build and summarise the task manifest for ``--session`` at the chosen horizon."""
    session_dir = Path(args.session)
    if not session_dir.is_dir():
        raise CommandUsageError(f"--session must be an existing directory, got {session_dir}")

    horizon_ns = _DEFAULT_HORIZON_NS
    if args.horizon_ms is not None:
        if args.horizon_ms <= 0:
            raise CommandUsageError("--horizon-ms must be positive")
        horizon_ns = args.horizon_ms * _MS_NS
    if horizon_ns % 2:
        raise CommandUsageError(
            "horizon must be even in nanoseconds (choose a different --horizon-ms)"
        )

    guard = LATENCY_SCENARIOS[PRIMARY_LATENCY_ID].guard_ns
    if not checkpoint_valid(horizon_ns, guard):
        raise CommandUsageError(
            f"horizon {horizon_ns} ns violates the checkpoint constraint 0 < H/2 < H - G "
            f"(G_{PRIMARY_LATENCY_ID} = {guard} ns); choose a larger --horizon-ms"
        )

    config = ExperimentConfig(
        experiment_id=f"tasks-build:{session_dir.name}:{horizon_ns}",
        horizon_ns=horizon_ns,
        latency_id=PRIMARY_LATENCY_ID,
    )

    print(SYNTHETIC_BANNER)
    print(f"Building task manifest for SYNTHETIC session {session_dir}")
    print(
        f"  horizon: {horizon_ns} ns ({horizon_ns / _MS_NS:.0f} ms), latency {PRIMARY_LATENCY_ID}"
    )

    try:
        manifest = build_task_manifest(session_dir, config)
    except Exception as exc:
        raise CommandError(f"failed to build manifest: {exc}") from exc

    n_tasks = len(manifest.tasks)
    side_counts = Counter(task.side.name for task in manifest.tasks)
    ineligible_reasons = Counter(ia.reason for ia in manifest.ineligible)
    corrupting = len(manifest.quality.corrupting_times)

    print(f"  manifest id: {manifest.task_manifest_id}")
    print(
        f"  eligible tasks: {n_tasks} (BUY={side_counts.get('BUY', 0)}, "
        f"SELL={side_counts.get('SELL', 0)})"
    )
    print(f"  ineligible arrival/sides: {len(manifest.ineligible)}")
    for reason, count in sorted(ineligible_reasons.items()):
        print(f"    - {reason}: {count}")
    print(f"  corrupting windows (technically unevaluable sources): {corrupting}")
    print("Done. Data is SYNTHETIC.")
    return 0
