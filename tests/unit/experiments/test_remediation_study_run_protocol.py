"""R13/R14 reproduction: the study run-directory protocol.

R13 (run lifecycle): ``run_study`` writes ``status.json`` with a config hash and a lifecycle
state (RUNNING -> COMPLETE / STOPPED_SUPPORT_GATE / FAILED). It refuses every nonempty out_dir,
including identical configurations, and report generation requires COMPLETE plus integrity.

R14 (write-once / append-only): the freeze directory is write-once (a rerun that would overwrite
a frozen manifest is refused) and the unblinding log is append-only (every test access is
appended; the first access is preserved).

These are commit-free reproductions: the helpers / API they exercise do not exist before the fix.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from qexec.analysis.report import write_report
from qexec.cli.main import EXIT_FAILURE, main
from qexec.experiments.config import StudyConfig
from qexec.experiments.layout import StudyLayout
from qexec.experiments.pipeline import (
    RunDirectoryError,
    StudyStatus,
    append_unblinding_access,
    config_hash,
    ensure_freeze_writable,
    read_status,
    run_study,
    write_status,
)


def _quick_cfg() -> StudyConfig:
    return StudyConfig.quick_preset()


# --- R13: status.json lifecycle --------------------------------------------


def test_config_hash_is_stable_and_sensitive() -> None:
    a = StudyConfig.quick_preset()
    b = StudyConfig.quick_preset()
    assert config_hash(a) == config_hash(b)
    c = dataclasses.replace(a, base_seed=a.base_seed + 1)
    assert config_hash(c) != config_hash(a)


def test_status_round_trip(tmp_path: Path) -> None:
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    write_status(layout, StudyStatus.RUNNING, config_hash(_quick_cfg()))
    st = read_status(layout)
    assert st is not None
    assert st["state"] == StudyStatus.RUNNING.value
    assert "config_hash" in st


def test_fresh_dir_required_mismatched_config_hash_refused(tmp_path: Path) -> None:
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    # Simulate a previous run with a different config hash.
    write_status(layout, StudyStatus.RUNNING, "deadbeef")
    with pytest.raises(RunDirectoryError, match=r"config hash|resume|fresh"):
        run_study(tmp_path, _quick_cfg())


@pytest.mark.slow
def test_successful_run_writes_complete_status(tmp_path: Path) -> None:
    cfg = _quick_cfg()
    run_study(tmp_path, cfg)
    st = read_status(StudyLayout(tmp_path))
    assert st is not None
    assert st["state"] == StudyStatus.COMPLETE.value
    assert st["config_hash"] == config_hash(cfg)


@pytest.mark.slow
def test_same_config_rerun_refused_and_history_preserved(tmp_path: Path) -> None:
    cfg = _quick_cfg()
    run_study(tmp_path, cfg)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(RunDirectoryError, match="fresh --out"):
        run_study(tmp_path, cfg)
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after
    st = read_status(StudyLayout(tmp_path))
    assert st is not None and st["state"] == StudyStatus.COMPLETE.value


# --- R14: write-once freeze + append-only unblinding -----------------------


def test_unblinding_log_is_append_only(tmp_path: Path) -> None:
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    append_unblinding_access(layout, "first access")
    first_text = layout.unblinding_log.read_text()
    append_unblinding_access(layout, "second access")
    second_text = layout.unblinding_log.read_text()
    # First access preserved; second appended (never overwritten).
    assert "first access" in first_text
    assert "first access" in second_text
    assert "second access" in second_text
    assert second_text.count("access=") >= 2


def test_unblinding_detail_cannot_insert_a_fake_log_line(tmp_path: Path) -> None:
    layout = StudyLayout(tmp_path)
    append_unblinding_access(layout, "task=bad\naccess=fake\r\nsecond")
    text = layout.unblinding_log.read_text()
    assert len(text.splitlines()) == 1
    assert "\\naccess=fake\\r\\n" in text


def test_freeze_dir_write_once_refuses_overwrite(tmp_path: Path) -> None:
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    # Simulate a pre-existing frozen manifest.
    layout.freeze_manifest.write_text(json.dumps({"frozen_at": "2026-01-01T00:00:00+00:00"}))
    with pytest.raises(RunDirectoryError, match=r"write-once|freeze|overwrite"):
        ensure_freeze_writable(layout)


# --- R13: support-gate stop lifecycle + report gating ----------------------


@pytest.mark.slow
def test_support_gate_failure_sets_stopped_status_and_cli_exits_nonzero(tmp_path: Path) -> None:
    # Impossible support minima -> the gate FAILS and the study stops.
    cfg = dataclasses.replace(StudyConfig.quick_preset(), support_min_eligible=10_000)
    result = run_study(tmp_path, cfg)
    assert result.support_verdict == "FAILED"
    st = read_status(StudyLayout(tmp_path))
    assert st is not None and st["state"] == StudyStatus.STOPPED_SUPPORT_GATE.value

    # The CLI surfaces this as a nonzero exit with a clear message (R13).
    out2 = tmp_path.parent / "cli_fail"
    cfg_path = tmp_path.parent / "fail_cfg.json"
    cfg_path.write_text(json.dumps(cfg.to_dict()))
    code = main(["experiment", "run", "--out", str(out2), "--config", str(cfg_path)])
    assert code == EXIT_FAILURE


@pytest.mark.slow
def test_report_refuses_non_complete_study(tmp_path: Path) -> None:
    cfg = dataclasses.replace(StudyConfig.quick_preset(), support_min_eligible=10_000)
    run_study(tmp_path, cfg)  # STOPPED_SUPPORT_GATE
    with pytest.raises((ValueError, FileNotFoundError), match=r"COMPLETE|missing"):
        write_report(tmp_path)
