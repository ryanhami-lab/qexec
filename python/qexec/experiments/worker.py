"""Top-level, picklable test-evaluation worker (research spec section 2, stage 8).

Each independent ``(session, scenario)`` evaluation is a self-contained unit of work: it loads
the **frozen** price and action model artifacts from disk (proving the artifacts suffice for test
evaluation -- research spec stage 7), builds the six policies (B0, B1, B2, B2_100MS, B3,
B3_NO_QUEUE), runs one :class:`~qexec.sim.engine.SessionEngine` pass, and returns the result
frames as Arrow IPC bytes plus the B3 decision logs.

Everything here is a module-level function and plain dataclass so it is picklable on Windows
``spawn`` start method: no lambdas, no closures, no engine objects crossing the process boundary.
The worker reloads models itself from paths rather than receiving fitted estimators, so only
small picklable values are sent to the pool.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from qexec.analysis.diagnostics import markouts_for_session
from qexec.core.config import ExperimentConfig
from qexec.labels.price import MidSeries
from qexec.models.action import ActionModels
from qexec.models.artifacts import ModelArtifact
from qexec.models.price import PriceModel
from qexec.models.schema import FeatureSchema, SupportRule
from qexec.policies.base import Policy
from qexec.policies.baselines import B0Policy, B1Policy
from qexec.policies.model_policies import B2Policy, B3Policy, PriceSignalScorer
from qexec.sim.engine import SessionEngine

__all__ = ["EvalResult", "EvalTask", "load_support_rule", "run_eval_task"]


@dataclass(frozen=True, slots=True)
class EvalTask:
    """A single picklable ``(session, scenario)`` evaluation request (frozen inputs)."""

    session_dir: str
    session_id: str
    scenario_id: str
    planned_scenario_ids: tuple[str, ...]
    horizon_ns: int
    epsilon: float
    fee_per_contract_fixed: int
    theta_primary: float
    theta_secondary: float
    tick_size_fixed: int
    # Frozen artifact base paths (no suffix); loaded inside the worker from disk.
    price_primary_path: str
    price_secondary_path: str
    action_b3_path: str
    action_b3_no_queue_path: str
    support_b3_path: str
    support_b3_no_queue_path: str
    # Policies to run (deterministic order). Baselines-only passes omit the model policies.
    include_model_policies: bool = True


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Serializable result of one evaluation (Arrow IPC bytes for each frame)."""

    session_id: str
    scenario_id: str
    task_results_ipc: bytes
    decisions_ipc: bytes
    executions_ipc: bytes
    b3_decision_log: list[dict[str, Any]]
    b3_no_queue_decision_log: list[dict[str, Any]]
    tasks_ipc: bytes
    reports_ipc: bytes
    quality_ipc: bytes
    markouts_ipc: bytes


def _frame_to_ipc(frame: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    frame.write_ipc(buf)
    return buf.getvalue()


def frame_from_ipc(data: bytes) -> pl.DataFrame:
    """Inverse of :func:`_frame_to_ipc` (used by the pipeline to reassemble results)."""
    return pl.read_ipc(io.BytesIO(data))


def load_support_rule(path: str) -> SupportRule:
    """Reload a :class:`SupportRule` from its JSON state file."""
    state = json.loads(Path(path).read_text())
    schema = FeatureSchema.of([str(n) for n in state["feature_names"]])
    return SupportRule(
        schema=schema,
        feature_min=tuple(float(v) for v in state["feature_min"]),
        feature_max=tuple(float(v) for v in state["feature_max"]),
        feature_margin=tuple(float(v) for v in state["feature_margin"]),
        min_training_rows=int(state["min_training_rows"]),
        n_training_rows=int(state["n_training_rows"]),
        n_min_fit_rows=state.get("n_min_fit_rows"),
    )


def _load_price_model(path: str) -> PriceModel:
    artifact = ModelArtifact.load(Path(path))
    return PriceModel.from_state(artifact.state)


def _load_action_models(path: str) -> ActionModels:
    artifact = ModelArtifact.load(Path(path))
    return ActionModels.from_state(artifact.state)


def _build_policies(task: EvalTask) -> tuple[list[Policy], B3Policy | None, B3Policy | None]:
    """Build the deterministic policy list and return the two B3 policies for log capture."""
    policies: list[Policy] = [B0Policy(), B1Policy()]
    b3: B3Policy | None = None
    b3_nq: B3Policy | None = None
    if task.include_model_policies:
        policies.append(B2Policy(task.theta_primary, "u_signal", "B2"))
        policies.append(B2Policy(task.theta_secondary, "u_signal_100ms", "B2_100MS"))
        action_b3 = _load_action_models(task.action_b3_path)
        action_b3_nq = _load_action_models(task.action_b3_no_queue_path)
        support_b3 = load_support_rule(task.support_b3_path)
        support_b3_nq = load_support_rule(task.support_b3_no_queue_path)
        b3 = B3Policy(
            action_b3,
            action_b3.schema,
            support_b3,
            task.epsilon,
            "B3",
            tick_size_fixed=task.tick_size_fixed,
        )
        b3_nq = B3Policy(
            action_b3_nq,
            action_b3_nq.schema,
            support_b3_nq,
            task.epsilon,
            "B3_NO_QUEUE",
            tick_size_fixed=task.tick_size_fixed,
        )
        policies.append(b3)
        policies.append(b3_nq)
    return policies, b3, b3_nq


def run_eval_task(task: EvalTask) -> EvalResult:
    """Run one frozen ``(session, scenario)`` evaluation pass and return serialized frames.

    Loads the frozen models from disk, builds the price scorer and policies, runs the engine for
    the configured scenario, and returns Arrow IPC bytes for result and audit frames plus the two
    B3 decision logs. Never refits models.
    """
    config = ExperimentConfig(
        experiment_id=f"{task.session_id}:{task.scenario_id}",
        horizon_ns=task.horizon_ns,
        latency_id=task.scenario_id,
        planned_scenario_ids=task.planned_scenario_ids,
        epsilon=task.epsilon,
        fee_per_contract_fixed=task.fee_per_contract_fixed,
    )
    primary = _load_price_model(task.price_primary_path)
    secondary = _load_price_model(task.price_secondary_path)
    scorer = PriceSignalScorer(primary, secondary)
    policies, b3, b3_nq = _build_policies(task)

    engine = SessionEngine(
        Path(task.session_dir),
        config,
        policies,
        scenario_id=task.scenario_id,
        fork_label_probe=False,
        price_scorer=scorer,
        record_checkpoint_features=False,
    )
    outputs = engine.run()
    b3_log = _annotate_log(b3.decision_log, task) if b3 is not None else []
    b3_nq_log = _annotate_log(b3_nq.decision_log, task) if b3_nq is not None else []
    return EvalResult(
        session_id=task.session_id,
        scenario_id=task.scenario_id,
        task_results_ipc=_frame_to_ipc(outputs.task_results),
        decisions_ipc=_frame_to_ipc(outputs.decisions),
        executions_ipc=_frame_to_ipc(outputs.executions),
        b3_decision_log=b3_log,
        b3_no_queue_decision_log=b3_nq_log,
        tasks_ipc=_frame_to_ipc(outputs.tasks),
        reports_ipc=_frame_to_ipc(outputs.reports),
        quality_ipc=_frame_to_ipc(outputs.quality),
        markouts_ipc=_frame_to_ipc(
            markouts_for_session(
                outputs.task_results,
                MidSeries.from_session(Path(task.session_dir)),
                task.tick_size_fixed,
            )
        ),
    )


def _annotate_log(log: list[dict[str, Any]], task: EvalTask) -> list[dict[str, Any]]:
    """Attach the run's scenario/session identity to each decision-log row (R11/R19).

    The :class:`B3Policy` itself does not know the latency scenario or session; the worker does.
    Annotating here keeps decision keys unique across scenarios and lets the persisted logs carry
    scenario identity for epsilon-sensitivity recomputation and the causal trace.
    """
    out: list[dict[str, Any]] = []
    for row in log:
        annotated = dict(row)
        annotated["scenario_id"] = task.scenario_id
        annotated["session_id"] = task.session_id
        annotated["horizon_ns"] = task.horizon_ns
        out.append(annotated)
    return out
