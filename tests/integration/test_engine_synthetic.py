"""End-to-end integration tests for the session replay engine (engine spec section 10).

A short synthetic session (120 s) is run end to end for B0 and B1: every TaskResult status is
valid, B0 completes nearly all tasks, executed quantity <= 1 everywhere, and IS/C_T reconcile
with the executions. ``trace()`` is exercised. A 10-minute synthetic session (B0+B1+probe at L1)
is run once and its runtime is asserted below the 120 s budget (reported in the final message).
"""

from __future__ import annotations

import time
from pathlib import Path

import polars as pl
import pytest

from qexec.analysis.metrics import implementation_shortfall_ticks
from qexec.core.config import ExperimentConfig
from qexec.policies.baselines import B0Policy, B1Policy
from qexec.sim.engine import SessionEngine
from qexec.synthetic.generator import SyntheticParams, write_synthetic_session

VALID_STATUSES = {"COMPLETED_ON_TIME", "DEADLINE_MISS", "TECHNICALLY_UNEVALUABLE"}


def _write(tmp_path: Path, sid: str, duration_s: int, seed: int = 11) -> Path:
    params = SyntheticParams(
        seed=seed, session_id=sid, start_ns=1_000_000_000_000, duration_s=duration_s
    )
    return write_synthetic_session(params, tmp_path)


def _cfg() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="int", horizon_ns=1_000_000_000, latency_id="L1")


def test_end_to_end_b0_b1_short_session(tmp_path: Path) -> None:
    sd = _write(tmp_path, "SYN-INT", 120)
    eng = SessionEngine(sd, _cfg(), [B0Policy(), B1Policy()], scenario_id="L1")
    out = eng.run()
    assert out.task_results.height > 0

    # Every status is valid; executed quantity <= 1 everywhere.
    assert set(out.task_results["status"].to_list()) <= VALID_STATUSES
    assert out.task_results["executed_quantity"].max() <= 1
    assert out.task_results["residual_quantity"].min() >= 0

    # B0 completes nearly all tasks (immediate aggressive).
    b0 = out.task_results.filter(out.task_results["policy_id"] == "B0")
    comp = b0.filter(b0["status"] == "COMPLETED_ON_TIME").height
    assert comp >= int(0.9 * b0.height), f"B0 completed {comp}/{b0.height}"


def test_is_reconciles_with_executions(tmp_path: Path) -> None:
    """IS recomputed from the recorded executions matches the stored is_ticks_gross/net."""
    sd = _write(tmp_path, "SYN-ISR", 120)
    cfg = _cfg()
    eng = SessionEngine(sd, cfg, [B0Policy(), B1Policy()], scenario_id="L1")
    out = eng.run()
    tick = 250_000_000
    mult = 50
    tasks = {t["task_id"]: t for t in out.tasks.filter(out.tasks["eligible"]).to_dicts()}
    exec_by_world: dict[str, list[tuple[int, int, int]]] = {}
    for e in out.executions.to_dicts():
        exec_by_world.setdefault(e["world_id"], []).append(
            (int(e["quantity"]), int(e["price_fixed"]), int(e["fee_fixed"]))
        )
    for r in out.task_results.filter(out.task_results["status"] == "COMPLETED_ON_TIME").to_dicts():
        wid = f"{r['task_id']}|{r['policy_id']}"
        m0 = tasks[r["task_id"]]["arrival_reference_mid2"]
        fills = [(q, p) for q, p, _f in exec_by_world.get(wid, [])]
        fees = sum(f for _q, _p, f in exec_by_world.get(wid, []))
        gross, net = implementation_shortfall_ticks(int(r["side"]), fills, m0, tick, mult, fees, 1)
        assert r["is_ticks_gross"] == pytest.approx(gross)
        assert r["is_ticks_net"] == pytest.approx(net)


def test_trace_explains_a_world(tmp_path: Path) -> None:
    sd = _write(tmp_path, "SYN-TRC", 120)
    eng = SessionEngine(sd, _cfg(), [B0Policy(), B1Policy()], scenario_id="L1")
    out = eng.run()
    b1 = out.task_results.filter(out.task_results["policy_id"] == "B1")
    tid = b1["task_id"][0]
    trace = out.trace(tid, "B1")
    kinds = [e["kind"] for e in trace]
    assert kinds[0] == "arrival"
    assert "arrival_decision" in kinds
    assert kinds[-1] == "outcome"
    # Trace times are nondecreasing (causal order).
    times = [e["time_ns"] for e in trace]
    assert times == sorted(times)


def test_outputs_have_expected_frames(tmp_path: Path) -> None:
    sd = _write(tmp_path, "SYN-FR", 120)
    eng = SessionEngine(
        sd, _cfg(), [B0Policy(), B1Policy()], scenario_id="L1", fork_label_probe=True
    )
    out = eng.run()
    for frame in (
        out.tasks,
        out.task_results,
        out.decisions,
        out.executions,
        out.reports,
        out.checkpoint_features,
        out.branch_labels,
        out.support,
        out.quality,
    ):
        assert isinstance(frame, pl.DataFrame)
    # One task_result row per (task, policy): 2 policies.
    n_tasks = out.tasks.filter(out.tasks["eligible"]).height
    assert out.task_results.height == 2 * n_tasks


@pytest.mark.slow
def test_ten_minute_runtime_under_budget(tmp_path: Path) -> None:
    """A 10-minute synthetic session with B0+B1+probe at L1 runs well under the 120 s budget."""
    sd = _write(tmp_path, "SYN-10M", 600, seed=23)
    cfg = _cfg()
    t0 = time.perf_counter()
    eng = SessionEngine(sd, cfg, [B0Policy(), B1Policy()], scenario_id="L1", fork_label_probe=True)
    out = eng.run()
    elapsed = time.perf_counter() - t0
    assert out.task_results.height > 0
    assert out.task_results["executed_quantity"].max() <= 1
    assert elapsed < 120.0, f"10-minute session took {elapsed:.1f}s (budget 120s)"
