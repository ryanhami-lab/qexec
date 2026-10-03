"""Analysis table tests: the primary pair estimate is invariant to adding other policies'
rows (T49), the latency table reports per scenario, and epsilon sensitivity is recomputed from
the stored B3 decision log without replay."""

from __future__ import annotations

import polars as pl

from qexec.analysis.tables import (
    build_all_tables,
    epsilon_sensitivity_table,
    latency_table,
    primary_pair_table,
)


def _result_row(
    task_id: str,
    policy_id: str,
    session_id: str,
    scenario_id: str,
    status: str,
    is_net: float | None,
    c_t: float | None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "session_id": session_id,
        "policy_id": policy_id,
        "scenario_id": scenario_id,
        "side": 1,
        "status": status,
        "is_ticks_net": is_net,
        "c_t_ticks": c_t,
        "passive_filled": status == "COMPLETED_ON_TIME",
    }


def _primary_pair_frame() -> pl.DataFrame:
    # Two sessions, two tasks each; B3 completes at a lower IS than B3_NO_QUEUE on common tasks.
    rows = []
    for session in ("SYN-0001", "SYN-0002"):
        for k in range(2):
            tid = f"{session}:{k:05d}:BUY"
            rows.append(
                _result_row(tid, "B3_NO_QUEUE", session, "L1", "COMPLETED_ON_TIME", 1.0, 1.0)
            )
            rows.append(_result_row(tid, "B3", session, "L1", "COMPLETED_ON_TIME", 0.5, 0.4))
    return pl.DataFrame(rows)


def test_primary_pair_estimate_unchanged_by_adding_policies() -> None:
    # T49: adding a third policy's rows must not change the primary pair estimate.
    base = _primary_pair_frame()
    base_table = primary_pair_table(base, bootstrap_n=200, bootstrap_seed=0)

    extra_rows = []
    for session in ("SYN-0001", "SYN-0002"):
        for k in range(2):
            tid = f"{session}:{k:05d}:BUY"
            extra_rows.append(_result_row(tid, "B2", session, "L1", "COMPLETED_ON_TIME", 9.0, 9.0))
    augmented = pl.concat([base, pl.DataFrame(extra_rows)])
    aug_table = primary_pair_table(augmented, bootstrap_n=200, bootstrap_seed=0)

    assert base_table["cost_effect_ticks"][0] == aug_table["cost_effect_ticks"][0]
    assert base_table["miss_diff"][0] == aug_table["miss_diff"][0]
    assert base_table["cost_ci_lo"][0] == aug_table["cost_ci_lo"][0]
    assert base_table["cost_ci_hi"][0] == aug_table["cost_ci_hi"][0]


def test_primary_pair_positive_effect_favors_candidate() -> None:
    # IS_baseline - IS_candidate = 1.0 - 0.5 = 0.5 per task; positive favors candidate (B3).
    table = primary_pair_table(_primary_pair_frame(), bootstrap_n=200, bootstrap_seed=0)
    assert table["cost_effect_ticks"][0] == 0.5
    assert table["cost_n_sessions_defined"][0] == 2
    assert table["cost_n_tasks"][0] == 4


def test_latency_table_has_one_row_per_scenario() -> None:
    rows = []
    for scenario in ("L1", "L6"):
        for session in ("SYN-0001",):
            tid = f"{session}:00000:BUY"
            rows.append(
                _result_row(tid, "B3_NO_QUEUE", session, scenario, "COMPLETED_ON_TIME", 1.0, 1.0)
            )
            rows.append(_result_row(tid, "B3", session, scenario, "COMPLETED_ON_TIME", 0.5, 0.4))
    table = latency_table(pl.DataFrame(rows), ("L1", "L6"), bootstrap_n=50, bootstrap_seed=0)
    assert table.height == 2
    assert sorted(table["scenario_id"].to_list()) == ["L1", "L6"]


def test_epsilon_sensitivity_from_decision_log() -> None:
    # eps=0 is risk-first: HOLD has lower p -> HOLD. eps=1 admits all, min cost SWITCH chosen.
    log = [
        {"p_hold": 0.1, "p_switch": 0.4, "v_hold": 2.0, "v_switch": 1.0, "supported": True},
        {"p_hold": 0.2, "p_switch": 0.5, "v_hold": 3.0, "v_switch": 1.0, "supported": True},
    ]
    table = epsilon_sensitivity_table(log, [0.0, 1.0], primary_epsilon=0.0)
    row0 = table.filter(pl.col("epsilon") == 0.0).to_dicts()[0]
    row1 = table.filter(pl.col("epsilon") == 1.0).to_dicts()[0]
    # eps=0: risk-first -> HOLD for both.
    assert row0["share_hold"] == 1.0
    assert row0["is_primary_epsilon"] is True
    assert row0["agreement_with_primary"] == 1.0
    # eps=1: all actions allowed, choose min cost -> SWITCH for both.
    assert row1["share_switch"] == 1.0
    assert row1["agreement_with_primary"] == 0.0


def test_build_all_tables_tags_data_kind_synthetic() -> None:
    tables = build_all_tables(
        _primary_pair_frame(),
        scenarios=("L1",),
        bootstrap_n=50,
        bootstrap_seed=0,
        b3_decision_log=[],
        epsilon_set=(0.0, 0.001),
        primary_epsilon=0.001,
        support_table=pl.DataFrame(),
    )
    assert set(tables) == {
        "primary_pair",
        "secondary_pairs",
        "per_policy_distribution",
        "latency_table",
        "epsilon_sensitivity",
        "support_table",
    }
    assert tables["primary_pair"]["data_kind"][0] == "SYNTHETIC"
