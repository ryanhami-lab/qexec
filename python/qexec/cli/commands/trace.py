"""``qexec trace task`` -- print the causal event trace of one (task, policy) world.

Runs a single :class:`~qexec.sim.engine.SessionEngine` pass over ``--session`` with the B0 and B1
baseline policies at the requested scenario/horizon, then prints the ordered causal trace of the
chosen ``--task-id`` under ``--policy`` as a readable table (engine spec section 9). All data is
SYNTHETIC.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from qexec.cli.commands import SYNTHETIC_BANNER, CommandError, CommandUsageError
from qexec.core.config import (
    LATENCY_SCENARIOS,
    ExperimentConfig,
    checkpoint_valid,
)
from qexec.core.records_io import read_instrument
from qexec.experiments.integrity import require_complete_study, verify_frozen_inputs
from qexec.experiments.layout import StudyLayout
from qexec.experiments.pipeline import append_unblinding_access
from qexec.experiments.worker import load_support_rule
from qexec.models.action import ActionModels
from qexec.models.artifacts import ModelArtifact
from qexec.models.price import PriceModel
from qexec.policies.base import Policy
from qexec.policies.baselines import B0Policy, B1Policy
from qexec.policies.model_policies import B2Policy, B3Policy, PriceSignalScorer
from qexec.sim.engine import SessionEngine

_MS_NS = 1_000_000
_DEFAULT_HORIZON_NS = 1_000_000_000  # 1 s
_SESSION_POLICIES = ("B0", "B1")
_STUDY_POLICIES = ("B0", "B1", "B2", "B2_100MS", "B3", "B3_NO_QUEUE")


def _format_trace_table(trace: list[dict[str, Any]]) -> str:
    """Render the ordered trace as a fixed-column text table."""
    if not trace:
        return "(empty trace)"
    # Collect the union of extra keys (everything except kind/time_ns) in first-seen order.
    extra_order: list[str] = []
    for row in trace:
        for key in row:
            if key not in ("kind", "time_ns") and key not in extra_order:
                extra_order.append(key)

    header = ["step", "time_ns", "kind", *extra_order]
    lines: list[list[str]] = [header]
    for i, row in enumerate(trace):
        cells = [str(i), str(row.get("time_ns", "")), str(row.get("kind", ""))]
        cells.extend("" if row.get(k) is None else str(row.get(k)) for k in extra_order)
        lines.append(cells)

    widths = [max(len(line[c]) for line in lines) for c in range(len(header))]
    rendered: list[str] = []
    for r, line in enumerate(lines):
        rendered.append("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(line)))
        if r == 0:
            rendered.append("  ".join("-" * widths[c] for c in range(len(header))))
    return "\n".join(rendered)


def _build_study_policies(
    study_dir: Path,
    *,
    epsilon: float,
    tick_size_fixed: int,
    theta_primary: float,
    theta_secondary: float,
) -> tuple[list[Policy], PriceSignalScorer]:
    """Build the six policies from a finished study's frozen artifacts (R19)."""
    layout = StudyLayout(study_dir)

    def _price(name: str) -> PriceModel:
        return PriceModel.from_state(ModelArtifact.load(layout.model_path(name)).state)

    def _action(name: str) -> ActionModels:
        return ActionModels.from_state(ModelArtifact.load(layout.model_path(name)).state)

    scorer = PriceSignalScorer(_price("price_primary"), _price("price_secondary"))
    action_b3 = _action("action_b3")
    action_b3_nq = _action("action_b3_no_queue")
    support_b3 = load_support_rule(str(layout.freeze_dir / "support_b3.json"))
    support_b3_nq = load_support_rule(str(layout.freeze_dir / "support_b3_no_queue.json"))
    policies: list[Policy] = [
        B0Policy(),
        B1Policy(),
        B2Policy(theta_primary, "u_signal", "B2"),
        B2Policy(theta_secondary, "u_signal_100ms", "B2_100MS"),
        B3Policy(
            action_b3, action_b3.schema, support_b3, epsilon, "B3", tick_size_fixed=tick_size_fixed
        ),
        B3Policy(
            action_b3_nq,
            action_b3_nq.schema,
            support_b3_nq,
            epsilon,
            "B3_NO_QUEUE",
            tick_size_fixed=tick_size_fixed,
        ),
    ]
    return policies, scorer


def _trace_from_study(args: argparse.Namespace, horizon_ns: int, scenario_id: str) -> int:
    """Verify a completed study and replay the requested test session from frozen inputs."""
    study_dir = Path(args.study)
    if not study_dir.is_dir():
        raise CommandUsageError(f"--study must be an existing directory, got {study_dir}")
    policy_id: str = args.policy
    if policy_id not in _STUDY_POLICIES:
        raise CommandUsageError(
            f"--policy must be one of {_STUDY_POLICIES} for a study trace, got {policy_id!r}"
        )
    layout = StudyLayout(study_dir)
    try:
        manifest = require_complete_study(layout)
        verify_frozen_inputs(layout, manifest)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        raise CommandError(f"study integrity verification failed: {exc}") from exc
    frozen_horizon = int(manifest["primary_horizon_ns"])
    if args.horizon_ms is not None and horizon_ns != frozen_horizon:
        raise CommandUsageError("--horizon-ms must equal the study's frozen primary horizon")
    horizon_ns = frozen_horizon
    if scenario_id not in manifest["config"]["scenarios"]:
        raise CommandUsageError(f"scenario {scenario_id!r} was not evaluated in this frozen study")
    epsilon = float(manifest["epsilon"])
    theta_primary = float(manifest["theta_primary"])
    theta_secondary = float(manifest["theta_secondary"])
    test_sessions = manifest["splits"]["test"]
    session_id = args.task_id.split(":", 1)[0]
    if session_id not in test_sessions:
        raise CommandUsageError(f"task-id must belong to a frozen test session: {test_sessions}")
    session_dir = layout.sessions_dir / session_id
    if not session_dir.is_dir():
        raise CommandError(f"test session directory missing: {session_dir}")
    tick = int(read_instrument(session_dir).tick_size_fixed)
    policies, scorer = _build_study_policies(
        study_dir,
        epsilon=epsilon,
        tick_size_fixed=tick,
        theta_primary=theta_primary,
        theta_secondary=theta_secondary,
    )
    config = ExperimentConfig(
        experiment_id=f"{session_id}:{scenario_id}",
        planned_scenario_ids=tuple(manifest["config"]["scenarios"]),
        horizon_ns=horizon_ns,
        latency_id=scenario_id,
        epsilon=epsilon,
        fee_per_contract_fixed=0,
    )
    print(SYNTHETIC_BANNER)
    print(f"Tracing SYNTHETIC study {study_dir} (session {session_id})")
    print(f"  task-id: {args.task_id}, policy: {policy_id}, scenario: {scenario_id}")
    try:
        append_unblinding_access(
            layout,
            f"cli_trace task={args.task_id} policy={policy_id} scenario={scenario_id} "
            f"horizon_ns={horizon_ns}",
        )
        engine = SessionEngine(
            session_dir, config, policies, scenario_id=scenario_id, price_scorer=scorer
        )
        outputs = engine.run()
    except Exception as exc:
        raise CommandError(f"engine replay failed: {exc}") from exc
    trace = outputs.trace(args.task_id, policy_id)
    if not trace:
        known_tasks = sorted(outputs.tasks.get_column("task_id").unique().to_list())
        sample = ", ".join(known_tasks[:5])
        raise CommandUsageError(
            f"no trace for task-id {args.task_id!r} under policy {policy_id!r}. "
            f"The session has {len(known_tasks)} eligible task(s); e.g. {sample}"
        )
    print()
    print(_format_trace_table(trace))
    print()
    print(f"{len(trace)} causal event(s) for one world. Data is SYNTHETIC.")
    return 0


def run_trace_task(args: argparse.Namespace) -> int:
    """Replay the session (or frozen study) and print the causal trace of one world."""
    if getattr(args, "study", None) and getattr(args, "session", None):
        raise CommandUsageError("pass only one of --session or --study, not both")
    if not getattr(args, "study", None) and not getattr(args, "session", None):
        raise CommandUsageError("one of --session or --study is required")

    scenario_id: str = args.scenario
    if scenario_id not in LATENCY_SCENARIOS:
        raise CommandUsageError(
            f"--scenario must be a known latency scenario {sorted(LATENCY_SCENARIOS)}, "
            f"got {scenario_id!r}"
        )

    horizon_ns = _DEFAULT_HORIZON_NS
    if args.horizon_ms is not None:
        if args.horizon_ms <= 0:
            raise CommandUsageError("--horizon-ms must be positive")
        horizon_ns = args.horizon_ms * _MS_NS
    if horizon_ns % 2:
        raise CommandUsageError(
            "horizon must be even in nanoseconds (choose a different --horizon-ms)"
        )

    if getattr(args, "study", None):
        return _trace_from_study(args, horizon_ns, scenario_id)

    session_dir = Path(args.session)
    if not session_dir.is_dir():
        raise CommandUsageError(f"--session must be an existing directory, got {session_dir}")

    policy_id = args.policy
    if policy_id not in _SESSION_POLICIES:
        raise CommandUsageError(
            f"--policy must be one of {_SESSION_POLICIES} for a session trace (the baselines "
            f"this mode replays); use --study to trace a frozen B2/B3, got {policy_id!r}"
        )

    guard = LATENCY_SCENARIOS[scenario_id].guard_ns
    if not checkpoint_valid(horizon_ns, guard):
        raise CommandUsageError(
            f"horizon {horizon_ns} ns violates 0 < H/2 < H - G for scenario {scenario_id} "
            f"(G = {guard} ns); choose a larger --horizon-ms"
        )

    config = ExperimentConfig(
        experiment_id=f"trace:{session_dir.name}:{scenario_id}:{horizon_ns}",
        horizon_ns=horizon_ns,
        latency_id=scenario_id,
    )
    policies: list[Policy] = [B0Policy(), B1Policy()]

    print(SYNTHETIC_BANNER)
    print(f"Tracing SYNTHETIC session {session_dir}")
    print(
        f"  task-id: {args.task_id}, policy: {policy_id}, scenario: {scenario_id}, "
        f"horizon: {horizon_ns} ns"
    )

    try:
        engine = SessionEngine(session_dir, config, policies, scenario_id=scenario_id)
        outputs = engine.run()
    except Exception as exc:
        raise CommandError(f"engine replay failed: {exc}") from exc

    trace = outputs.trace(args.task_id, policy_id)
    if not trace:
        known_tasks = sorted(outputs.tasks.get_column("task_id").unique().to_list())
        sample = ", ".join(known_tasks[:5])
        raise CommandUsageError(
            f"no trace for task-id {args.task_id!r} under policy {policy_id!r}. "
            f"The session has {len(known_tasks)} eligible task(s); e.g. {sample}"
        )

    print()
    print(_format_trace_table(trace))
    print()
    print(f"{len(trace)} causal event(s) for one world. Data is SYNTHETIC.")
    return 0
