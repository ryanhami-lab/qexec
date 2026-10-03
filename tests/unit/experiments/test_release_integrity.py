"""Regression tests for immutable study history and fail-closed consumers."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest

from qexec.analysis.report import write_report
from qexec.cli.main import main
from qexec.experiments import pipeline
from qexec.experiments.config import StudyConfig
from qexec.experiments.integrity import require_complete_study
from qexec.experiments.layout import StudyLayout


@pytest.mark.parametrize("state", [None, "RUNNING", "FAILED", "COMPLETE"])
def test_existing_run_is_never_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    cfg = StudyConfig.quick_preset()
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "history.txt").write_text("preserve me")
    if state:
        (tmp_path / "status.json").write_text(
            json.dumps({"state": state, "config_hash": pipeline.config_hash(cfg)})
        )
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def forbidden(*args: object) -> None:
        pytest.fail("an existing output directory reached study execution")

    monkeypatch.setattr(pipeline, "_run_study_body", forbidden)
    with pytest.raises(pipeline.RunDirectoryError, match="fresh --out"):
        pipeline.run_study(tmp_path, cfg)
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize(
    "status", [None, "{", "[]", "{}", '{"state":"RUNNING"}', '{"state":"COMPLETE"}']
)
def test_report_requires_valid_complete_status(tmp_path: Path, status: str | None) -> None:
    (tmp_path / "results").mkdir()
    for name in ("metrics.json", "report_inputs.json"):
        (tmp_path / "results" / name).write_text("{}")
    if status is not None:
        (tmp_path / "status.json").write_text(status)
    with pytest.raises(ValueError, match="COMPLETE"):
        write_report(tmp_path)


@pytest.fixture(scope="module")
def completed_study(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("study with apostrophe's")
    cfg = replace(
        StudyConfig.quick_preset(),
        n_sessions=6,
        duration_s=15,
        n_pilot_sessions=1,
        support_min_eligible=0,
        support_min_queue_depletion=0,
        support_margin_frac=0,
        bootstrap_n=10,
        max_workers=1,
        horizon_ladder_ns=(1_000_000_000,),
    )
    result = pipeline.run_study(path, cfg)
    assert result.support_verdict == "PASSED"
    return path


def test_report_commands_use_actual_path_and_fresh_output(completed_study: Path) -> None:
    report = write_report(completed_study).read_text(encoding="utf-8")
    escaped = str(completed_study.resolve()).replace("'", "''")
    assert f"--config '{escaped}\\config.json'" in report
    assert f"--out '{escaped}-reproduction'" in report
    assert "--locked --offline" in report


@pytest.mark.parametrize("target", ["config.json", "results/metrics.json", "freeze/action_b3.json"])
def test_report_rejects_altered_completed_artifact(
    completed_study: Path, tmp_path: Path, target: str
) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(completed_study, copy)
    with (copy / target).open("a") as handle:
        handle.write(" ")
    with pytest.raises(ValueError, match="integrity mismatch"):
        write_report(copy)


def test_trace_selects_later_test_session_and_appends_every_access(
    completed_study: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = StudyLayout(completed_study)
    manifest = require_complete_study(layout)
    sid = manifest["splits"]["test"][-1]
    rows = pl.read_parquet(layout.results_dir / "task_results.parquet")
    task_id = rows.filter(pl.col("session_id") == sid)["task_id"][0]
    before = layout.unblinding_log.read_text()
    args = [
        "trace",
        "task",
        "--study",
        str(completed_study),
        "--task-id",
        task_id,
        "--policy",
        "B3",
        "--scenario",
        "L6",
    ]
    assert main(args) == 0
    assert f"session {sid}" in capsys.readouterr().out
    after = layout.unblinding_log.read_text()
    assert after.startswith(before)
    assert after.count("cli_trace") == before.count("cli_trace") + 1
    assert "scenario=L6 horizon_ns=1000000000" in after
    assert main(args) == 0
    assert layout.unblinding_log.read_text().startswith(after)
    require_complete_study(layout)  # Appended access does not invalidate the immutable outputs.


@pytest.mark.parametrize("flag,value", [("--horizon-ms", "500"), ("--scenario", "L2")])
def test_trace_refuses_unfrozen_configuration(completed_study: Path, flag: str, value: str) -> None:
    manifest = require_complete_study(StudyLayout(completed_study))
    sid = manifest["splits"]["test"][0]
    assert (
        main(
            [
                "trace",
                "task",
                "--study",
                str(completed_study),
                "--task-id",
                f"{sid}:00000:BUY",
                "--policy",
                "B3",
                flag,
                value,
            ]
        )
        != 0
    )


def test_trace_rejects_tampered_test_data(completed_study: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(completed_study, copy)
    manifest = require_complete_study(StudyLayout(copy))
    sid = manifest["splits"]["test"][0]
    with (copy / "sessions" / sid / "records.parquet").open("ab") as handle:
        handle.write(b"tampered")
    assert (
        main(
            [
                "trace",
                "task",
                "--study",
                str(copy),
                "--task-id",
                f"{sid}:00000:BUY",
                "--policy",
                "B3",
            ]
        )
        != 0
    )


def test_moved_study_is_still_verifiable(completed_study: Path, tmp_path: Path) -> None:
    copy = tmp_path / "moved"
    shutil.copytree(completed_study, copy)
    require_complete_study(StudyLayout(copy))
    assert write_report(copy).is_file()


def test_config_and_freeze_must_match_even_if_completion_seal_is_rebuilt(
    completed_study: Path, tmp_path: Path
) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(completed_study, copy)
    layout = StudyLayout(copy)
    config = json.loads(layout.config_json.read_text())
    config.pop("data_kind", None)
    cfg = replace(StudyConfig.from_dict(config), base_seed=999)
    layout.config_json.write_text(json.dumps(cfg.to_dict()))
    pipeline.write_status(layout, pipeline.StudyStatus.COMPLETE, pipeline.config_hash(cfg))
    with pytest.raises(ValueError, match="freeze/config mismatch"):
        require_complete_study(layout)


@pytest.mark.parametrize("damage", ["delete", "truncate", "replace"])
@pytest.mark.parametrize("consumer", ["report", "trace"])
def test_completed_audit_history_cannot_be_removed_or_replaced(
    completed_study: Path, tmp_path: Path, damage: str, consumer: str
) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(completed_study, copy)
    layout = StudyLayout(copy)
    original = layout.unblinding_log.read_bytes()
    if damage == "delete":
        layout.unblinding_log.unlink()
    elif damage == "truncate":
        layout.unblinding_log.write_bytes(original[:1])
    else:
        layout.unblinding_log.write_bytes(b"x" * len(original))
    if consumer == "report":
        with pytest.raises(ValueError, match="unblinding"):
            write_report(copy)
    else:
        manifest = json.loads(layout.freeze_manifest.read_text())
        sid = manifest["splits"]["test"][0]
        task_id = pl.read_parquet(layout.results_dir / "task_results.parquet").filter(
            pl.col("session_id") == sid
        )["task_id"][0]
        assert (
            main(["trace", "task", "--study", str(copy), "--task-id", task_id, "--policy", "B3"])
            == 1
        )


@pytest.mark.parametrize("missing", [False, True])
def test_completion_requires_nonempty_audit_history(
    completed_study: Path, tmp_path: Path, missing: bool
) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(completed_study, copy)
    layout = StudyLayout(copy)
    status_before = layout.status_json.read_bytes()
    if missing:
        layout.unblinding_log.unlink()
    else:
        layout.unblinding_log.write_bytes(b"")
    with pytest.raises(ValueError, match="unblinding"):
        pipeline.write_status(layout, pipeline.StudyStatus.COMPLETE, "unused")
    assert layout.status_json.read_bytes() == status_before


def test_audit_seal_binds_prefix_but_allows_later_appends(completed_study: Path) -> None:
    layout = StudyLayout(completed_study)
    status_before = layout.status_json.read_bytes()
    seal = json.loads(status_before)["unblinding_log_seal"]
    before = layout.unblinding_log.read_bytes()
    assert 0 < seal["byte_length"] <= len(before)
    assert hashlib.sha256(before[: seal["byte_length"]]).hexdigest() == seal["sha256"]
    pipeline.append_unblinding_access(layout, "later permitted access")
    assert layout.unblinding_log.read_bytes().startswith(before)
    require_complete_study(layout)
    assert layout.status_json.read_bytes() == status_before
