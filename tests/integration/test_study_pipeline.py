"""End-to-end study pipeline integration tests (research spec section 3).

The quick study is run once (module-scoped) and many invariants are asserted against its
artifacts: every output file exists, every artifact is tagged ``data_kind = "SYNTHETIC"``, the
primary development sample is labelled, the freeze manifest hashes match the saved model
artifacts, test-time models are loaded from disk, and whole sessions never cross a split
boundary (T23). A separate tiny study checks that parallel and sequential execution produce
identical analysis tables.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

from qexec.core.config import THETA_MENU
from qexec.experiments import StudyConfig, run_study
from qexec.experiments.layout import StudyLayout
from qexec.features.groups import QUEUE_FEATURES
from qexec.models.artifacts import ModelArtifact


@pytest.fixture(scope="module")
def quick_study(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, object]:
    out = tmp_path_factory.mktemp("qexec-quick-study")
    cfg = StudyConfig.quick_preset()
    result = run_study(out, cfg)
    return out, result


@pytest.mark.slow
def test_study_runs_and_passes_support_gate(quick_study: tuple[Path, object]) -> None:
    _out, result = quick_study
    assert result.support_verdict == "PASSED"  # type: ignore[attr-defined]
    assert result.primary_horizon_ns is not None  # type: ignore[attr-defined]


@pytest.mark.slow
def test_quick_study_degeneracy_reported_honestly_not_hidden(
    quick_study: tuple[Path, object],
) -> None:
    """R20: the support region is built from the EXACT action-training (OOF) matrix.

    On the quick preset this makes the OOF price-signal features fall outside the support band at
    test, so B3 is dominated by FALLBACK_UNSUPPORTED. Per R20 this degeneracy is reported
    honestly via the always-present flag and a diagnostic of which features trigger it -- support
    is NEVER redefined to hide it. (This test previously encoded the hidden-degeneracy behaviour
    where the support band was re-scored from the final model to force model_choice_fraction >=
    0.5; that silently masked the exact-matrix mismatch the reviewer flagged, so the assertion is
    inverted to the honest outcome.)
    """
    out, _result = quick_study
    layout = StudyLayout(out)
    metrics = json.loads(layout.metrics_json.read_text())
    guardrails = metrics["degeneracy_guardrails"]
    # Reason counts and support rate are reported for both variants, always present.
    for variant in ("b3", "b3_no_queue"):
        block = guardrails[variant]
        counts = block["reason_counts"]
        assert set(counts) >= {
            "MODEL_CHOICE",
            "FALLBACK_UNSUPPORTED",
            "FALLBACK_NONFINITE",
        }
        assert block["n_eligible_checkpoints"] == sum(counts.values())
        assert 0.0 <= block["support_rate"] <= 1.0
        assert 0.0 <= block["model_choice_fraction"] <= 1.0
    # The flag is present and honestly set (degenerate on quick data), with a human-readable
    # reason -- never hidden.
    assert "degenerate_primary_comparison" in guardrails
    assert guardrails["degenerate_primary_comparison"] is True
    assert guardrails["degeneracy_reason"]
    assert "fallback" in guardrails["degeneracy_reason"].lower()
    # Disagreement rate is reported.
    assert 0.0 <= guardrails["action_disagreement_rate"] <= 1.0
    # report_inputs.json surfaces the same guardrails block and the per-feature diagnostic (R20).
    report = json.loads(layout.report_inputs_json.read_text())
    assert report["degeneracy_guardrails"]["degenerate_primary_comparison"] is True
    counts = report["unsupported_feature_counts"]
    assert isinstance(counts["b3"], dict)
    # The OOF price-signal features are the dominant trigger of unsupported fallback.
    assert any(k in counts["b3"] for k in ("u_signal", "p_up", "p_down", "p_unch"))


@pytest.mark.slow
def test_all_output_files_exist(quick_study: tuple[Path, object]) -> None:
    out, _result = quick_study
    layout = StudyLayout(out)
    required = [
        layout.config_json,
        layout.support_gate_json,
        layout.development_json,
        layout.validation_json,
        layout.freeze_manifest,
        layout.unblinding_log,
        layout.metrics_json,
        layout.report_inputs_json,
        layout.results_dir / "task_results.parquet",
        layout.results_dir / "decisions.parquet",
        layout.results_dir / "executions.parquet",
        layout.results_dir / "branch_labels_dev.parquet",
        layout.results_dir / "support.parquet",
    ]
    for path in required:
        assert path.exists(), f"missing output file {path}"
    # Every results parquet carries scenario identity (remediation R11).
    for parquet in sorted(layout.results_dir.glob("*.parquet")):
        assert "scenario_id" in pl.read_parquet(parquet).columns, parquet.name
    for name in (
        "primary_pair",
        "secondary_pairs",
        "per_policy_distribution",
        "latency_table",
        "epsilon_sensitivity",
        "support_table",
    ):
        assert (layout.tables_dir / f"{name}.csv").exists()


@pytest.mark.slow
def test_complete_audit_frames_and_markouts_are_saved(quick_study: tuple[Path, object]) -> None:
    out, _result = quick_study
    layout = StudyLayout(out)
    frames = {}
    for name in ("tasks", "reports", "quality", "markouts"):
        path = layout.results_dir / f"{name}.parquet"
        assert path.is_file(), f"missing audit artifact: {name}"
        frames[name] = pl.read_parquet(path)
        assert {"scenario_id", "horizon_ns"} <= set(frames[name].columns)
    results = pl.read_parquet(layout.results_dir / "task_results.parquet")
    completed = results.filter(pl.col("status") == "COMPLETED_ON_TIME").height
    assert frames["markouts"].height == 3 * completed
    assert frames["markouts"].get_column("data_kind").unique().to_list() == ["SYNTHETIC"]
    inputs = json.loads(layout.report_inputs_json.read_text())
    summary = inputs["markout_summary"]
    assert sum(row["n_completed"] for row in summary) == 3 * completed
    assert all(row["n_completed"] == row["n_defined"] + row["n_missing"] for row in summary)
    assert (layout.tables_dir / "markouts.csv").is_file()
    seal = json.loads(layout.status_json.read_text())["artifact_hashes"]
    assert "results/resources.json" in seal
    resources = json.loads((layout.results_dir / "resources.json").read_text())
    assert resources["runtime_to_analysis_seconds"] > 0
    assert resources["generated_session_bytes"] > 0
    assert resources["test_task_results"] == results.height
    for name in frames:
        assert f"results/{name}.parquet" in seal


@pytest.mark.slow
def test_artifacts_tagged_synthetic(quick_study: tuple[Path, object]) -> None:
    out, _result = quick_study
    layout = StudyLayout(out)
    metrics = json.loads(layout.metrics_json.read_text())
    assert metrics["data_kind"] == "SYNTHETIC"
    config = json.loads(layout.config_json.read_text())
    assert config["data_kind"] == "SYNTHETIC"
    manifest = json.loads(layout.freeze_manifest.read_text())
    assert manifest["data_kind"] == "SYNTHETIC"


@pytest.mark.slow
def test_primary_development_sample_is_labelled(quick_study: tuple[Path, object]) -> None:
    out, _result = quick_study
    layout = StudyLayout(out)
    branch = pl.read_parquet(layout.results_dir / "branch_labels_dev.parquet")
    assert branch.height > 0
    assert "price_label" in branch.columns
    assert "price_label_100ms" in branch.columns
    # At least some rows carry a real (non-null) price label.
    assert branch.filter(pl.col("price_label").is_not_null()).height > 0
    # Unchanged class 0 is retained (T43): class 0 is a valid label value, never collapsed to null.
    labels = set(branch.get_column("price_label").drop_nulls().unique().to_list())
    assert labels.issubset({-1, 0, 1})


@pytest.mark.slow
def test_freeze_manifest_hashes_match_saved_artifacts(quick_study: tuple[Path, object]) -> None:
    out, _result = quick_study
    layout = StudyLayout(out)
    manifest = json.loads(layout.freeze_manifest.read_text())
    hashes = manifest["model_artifact_hashes"]
    assert hashes, "freeze manifest must record artifact hashes"
    for filename, expected in hashes.items():
        path = layout.freeze_dir / filename
        assert path.exists(), f"hashed artifact {filename} missing on disk"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, f"hash mismatch for {filename}"
    # frozen_at and git fields are present.
    assert "frozen_at" in manifest
    assert "git_commit" in manifest


@pytest.mark.slow
def test_test_models_loaded_from_disk(quick_study: tuple[Path, object]) -> None:
    out, result = quick_study
    assert result.loaded_models_from_disk is True  # type: ignore[attr-defined]
    layout = StudyLayout(out)
    # The unblinding log records the first test access (proves blinding until freeze). It is now
    # append-only (R14): each access line is prefixed ``access=<utc>`` and the first-access
    # marker ``first_test_access`` is preserved.
    log = layout.unblinding_log.read_text()
    assert "first_test_access" in log
    assert "access=" in log


@pytest.mark.slow
def test_sessions_whole_never_cross_split_boundary(quick_study: tuple[Path, object]) -> None:
    # T23: splits are by whole session index; no session id appears in two splits.
    out, _result = quick_study
    layout = StudyLayout(out)
    manifest = json.loads(layout.freeze_manifest.read_text())
    splits = manifest["splits"]
    train, val, test = set(splits["train"]), set(splits["validation"]), set(splits["test"])
    assert train.isdisjoint(val)
    assert train.isdisjoint(test)
    assert val.isdisjoint(test)
    # Test task_results only reference test sessions (a task's rows all in one split).
    tr = pl.read_parquet(layout.results_dir / "task_results.parquet")
    result_sessions = set(tr.get_column("session_id").unique().to_list())
    assert result_sessions.issubset(test)


@pytest.mark.slow
def test_all_six_policies_evaluated_across_scenarios(quick_study: tuple[Path, object]) -> None:
    out, _result = quick_study
    layout = StudyLayout(out)
    tr = pl.read_parquet(layout.results_dir / "task_results.parquet")
    policies = set(tr.get_column("policy_id").unique().to_list())
    assert policies == {"B0", "B1", "B2", "B2_100MS", "B3", "B3_NO_QUEUE"}
    scenarios = set(tr.get_column("scenario_id").unique().to_list())
    assert scenarios == {"L1", "L6"}


@pytest.mark.slow
def test_oof_provenance_is_strictly_prior(quick_study: tuple[Path, object]) -> None:
    # T44: each scored session's forward-chained OOF training set uses only earlier sessions.
    out, _result = quick_study
    layout = StudyLayout(out)
    dev = json.loads(layout.development_json.read_text())
    manifest = json.loads(layout.freeze_manifest.read_text())
    train_order = manifest["splits"]["train"]
    index_of = {sid: i for i, sid in enumerate(train_order)}
    log = dev["oof_train_index_log"]
    for sid, prior in log.items():
        if sid not in index_of:
            continue
        k = index_of[sid]
        # Every prior session used to score ``sid`` must be chronologically earlier.
        for prior_sid in prior:
            assert index_of[prior_sid] < k, f"{prior_sid} not strictly before {sid}"
    # The earliest train session has no prior data and is unscored (reported).
    assert train_order[0] in dev["oof_unscored_sessions"]


@pytest.mark.slow
def test_action_schemas_differ_exactly_by_queue(quick_study: tuple[Path, object]) -> None:
    # T45: the two action schemas differ exactly by QUEUE_FEATURES (recorded in the artifacts).
    out, _result = quick_study
    layout = StudyLayout(out)
    b3 = ModelArtifact.load(layout.model_path("action_b3"))
    b3_nq = ModelArtifact.load(layout.model_path("action_b3_no_queue"))
    b3_set = set(b3.schema.names)
    nq_set = set(b3_nq.schema.names)
    assert b3_set - nq_set == set(QUEUE_FEATURES)
    assert nq_set - b3_set == set()


@pytest.mark.slow
def test_theta_chosen_only_from_validation(quick_study: tuple[Path, object]) -> None:
    # T26: theta trials come from validation sessions only; the chosen theta is in the menu.
    out, result = quick_study
    layout = StudyLayout(out)
    validation = json.loads(layout.validation_json.read_text())
    assert result.theta_primary in THETA_MENU  # type: ignore[attr-defined]
    assert result.theta_secondary in THETA_MENU  # type: ignore[attr-defined]
    # Every menu theta is logged (a complete trial log).
    logged = {t["theta"] for t in validation["primary_trials"]}
    assert logged == set(THETA_MENU)


@pytest.mark.slow
def test_parallel_and_sequential_tables_identical(tmp_path: Path) -> None:
    """Parallel (max_workers=2) and sequential (max_workers=1) runs give identical tables."""
    cfg = StudyConfig.quick_preset()
    cfg_seq = dataclasses.replace(cfg, max_workers=1)
    out_par = tmp_path / "par"
    out_seq = tmp_path / "seq"
    res_par = run_study(out_par, cfg)
    res_seq = run_study(out_seq, cfg_seq)

    par = pl.read_csv(StudyLayout(out_par).tables_dir / "primary_pair.csv")
    seq = pl.read_csv(StudyLayout(out_seq).tables_dir / "primary_pair.csv")
    assert par.equals(seq)
    # Headline metrics identical.
    assert res_par.theta_primary == res_seq.theta_primary  # type: ignore[attr-defined]
    assert res_par.primary_horizon_ns == res_seq.primary_horizon_ns  # type: ignore[attr-defined]
    tr_par = pl.read_parquet(StudyLayout(out_par).results_dir / "task_results.parquet").sort(
        ["task_id", "policy_id", "scenario_id"]
    )
    tr_seq = pl.read_parquet(StudyLayout(out_seq).results_dir / "task_results.parquet").sort(
        ["task_id", "policy_id", "scenario_id"]
    )
    assert tr_par.equals(tr_seq)
