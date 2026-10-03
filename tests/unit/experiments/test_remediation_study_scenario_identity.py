"""R11 reproduction: scenario identity on frames + primary diagnostics isolate L1.

The reviewer found decisions/executions lacked scenario identity, so primary diagnostics pooled
scenarios and decision keys collided across latency worlds. The fix: every result frame carries
``scenario_id`` and ``horizon_ns`` (added at concatenation when the engine does not), decision
keys include ``scenario_id``, and the primary degeneracy comparison selects the primary scenario
(L1) only -- so appending e.g. L6 decisions cannot change the primary disagreement.
"""

from __future__ import annotations

import polars as pl

from qexec.experiments.pipeline import (
    _degeneracy_guardrails,
    _primary_scenario_log,
    _stamp_scenario,
)


def _row(task_id: str, time_ns: int, choice: str, scenario_id: str) -> dict:
    return {
        "task_id": task_id,
        "time_ns": time_ns,
        "p_hold": 0.1,
        "p_switch": 0.2,
        "v_hold": 1.0,
        "v_switch": 2.0,
        "supported": True,
        "choice": choice,
        "reason": "MODEL_CHOICE",
        "scenario_id": scenario_id,
    }


def test_appending_l6_decisions_does_not_change_primary_disagreement() -> None:
    # L1-only logs: B3 and B3_NO_QUEUE disagree on exactly one of two shared checkpoints.
    b3_l1 = [_row("t1", 1, "HOLD", "L1"), _row("t2", 2, "SWITCH", "L1")]
    nq_l1 = [_row("t1", 1, "SWITCH", "L1"), _row("t2", 2, "SWITCH", "L1")]
    base = _degeneracy_guardrails(b3_l1, nq_l1, pl.DataFrame())

    # Now append L6 decisions that disagree on everything; the primary (L1) comparison must be
    # unchanged because the primary diagnostics select L1 only.
    b3_all = [*b3_l1, _row("t1", 1, "HOLD", "L6"), _row("t2", 2, "HOLD", "L6")]
    nq_all = [*nq_l1, _row("t1", 1, "SWITCH", "L6"), _row("t2", 2, "SWITCH", "L6")]
    appended = _degeneracy_guardrails(b3_all, nq_all, pl.DataFrame())

    assert appended["action_disagreement_rate"] == base["action_disagreement_rate"]
    assert appended["n_shared_eligible_checkpoints"] == base["n_shared_eligible_checkpoints"]
    assert appended["b3"]["n_eligible_checkpoints"] == base["b3"]["n_eligible_checkpoints"]


def test_primary_scenario_log_keeps_only_l1() -> None:
    log = [_row("t1", 1, "HOLD", "L1"), _row("t2", 2, "SWITCH", "L6")]
    kept = _primary_scenario_log(log)
    assert len(kept) == 1
    assert kept[0]["scenario_id"] == "L1"


def test_primary_scenario_log_treats_missing_scenario_as_primary() -> None:
    log = [
        {
            "task_id": "t1",
            "time_ns": 1,
            "choice": "HOLD",
            "reason": "MODEL_CHOICE",
            "supported": True,
        }
    ]
    assert len(_primary_scenario_log(log)) == 1


def test_stamp_scenario_adds_columns_when_absent_and_preserves_present() -> None:
    frame = pl.DataFrame({"task_id": ["t1"], "choice": ["HOLD"]})
    stamped = _stamp_scenario(frame, "L6", 5_000_000_000)
    assert stamped.get_column("scenario_id").to_list() == ["L6"]
    assert stamped.get_column("horizon_ns").to_list() == [5_000_000_000]

    # If the engine already stamped scenario_id/horizon_ns, the study plane never overwrites them.
    pre = pl.DataFrame({"task_id": ["t1"], "scenario_id": ["L1"], "horizon_ns": [1_000_000_000]})
    kept = _stamp_scenario(pre, "L6", 5_000_000_000)
    assert kept.get_column("scenario_id").to_list() == ["L1"]
    assert kept.get_column("horizon_ns").to_list() == [1_000_000_000]
