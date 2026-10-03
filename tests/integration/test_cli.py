"""Integration tests for the ``qexec`` CLI (WP7-RELEASE).

Fast tests call :func:`qexec.cli.main.main` in-process with an ``argv`` list and assert on exit
codes and captured stdout. A small synthetic session is generated once per module (module-scoped
fixture) and shared across the fast tests. The end-to-end ``demo --quick`` and the
``analysis report`` on a quick study are marked ``@pytest.mark.slow`` because they run the full
study pipeline.

All data is SYNTHETIC. No test performs any network access.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from qexec.cli.main import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, main
from qexec.synthetic.generator import SyntheticParams, write_synthetic_session


@pytest.fixture(scope="module")
def tiny_session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A single short synthetic session for the fast CLI tests."""
    root = tmp_path_factory.mktemp("qexec-cli-session")
    params = SyntheticParams(
        seed=7,
        session_id="SYN-0001",
        start_ns=1_767_623_400_000_000_000,
        duration_s=20,
    )
    return write_synthetic_session(params, root)


# ---------------------------------------------------------------------------
# synth
# ---------------------------------------------------------------------------


def test_synth_generates_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "synth_out"
    code = main(["synth", "--out", str(out), "--sessions", "2", "--duration", "15", "--seed", "3"])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "SYNTHETIC" in captured.out
    assert (out / "SYN-0001").is_dir()
    assert (out / "SYN-0002").is_dir()
    assert (out / "SYN-0001" / "checksums.json").is_file()


def test_synth_bad_sessions_is_usage_error(tmp_path: Path) -> None:
    out = tmp_path / "synth_bad"
    code = main(["synth", "--out", str(out), "--sessions", "0", "--duration", "15", "--seed", "0"])
    assert code == EXIT_USAGE


def test_synth_is_deterministic(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    assert (
        main(["synth", "--out", str(out_a), "--sessions", "1", "--duration", "15", "--seed", "11"])
        == 0
    )
    assert (
        main(["synth", "--out", str(out_b), "--sessions", "1", "--duration", "15", "--seed", "11"])
        == 0
    )
    cks_a = json.loads((out_a / "SYN-0001" / "checksums.json").read_text())
    cks_b = json.loads((out_b / "SYN-0001" / "checksums.json").read_text())
    assert cks_a == cks_b


# ---------------------------------------------------------------------------
# replay validate
# ---------------------------------------------------------------------------


def test_replay_validate_good_session(
    tiny_session: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["replay", "validate", "--session", str(tiny_session)])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "checksums: OK" in captured.out
    assert "RESULT: OK" in captured.out
    assert "SYNTHETIC" in captured.out


def test_replay_validate_tampered_checksum_nonzero(tiny_session: Path, tmp_path: Path) -> None:
    # Copy the session and corrupt a hashed file so verification must fail.
    tampered = tmp_path / "tampered"
    shutil.copytree(tiny_session, tampered)
    with (tampered / "records.parquet").open("ab") as fh:
        fh.write(b"corruption")
    code = main(["replay", "validate", "--session", str(tampered)])
    assert code == EXIT_FAILURE


def test_replay_validate_missing_dir_is_usage_error(tmp_path: Path) -> None:
    code = main(["replay", "validate", "--session", str(tmp_path / "nope")])
    assert code == EXIT_USAGE


# ---------------------------------------------------------------------------
# tasks build
# ---------------------------------------------------------------------------


def test_tasks_build_prints_manifest_summary(
    tiny_session: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["tasks", "build", "--session", str(tiny_session)])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "manifest id:" in captured.out
    assert "eligible tasks:" in captured.out
    assert "SYNTHETIC" in captured.out


def test_tasks_build_rejects_too_short_horizon(tiny_session: Path) -> None:
    # 1 ms horizon violates the checkpoint constraint 0 < H/2 < H - G.
    code = main(["tasks", "build", "--session", str(tiny_session), "--horizon-ms", "1"])
    assert code == EXIT_USAGE


# ---------------------------------------------------------------------------
# trace task
# ---------------------------------------------------------------------------


def test_trace_task_prints_causal_table(
    tiny_session: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "trace",
            "task",
            "--session",
            str(tiny_session),
            "--task-id",
            "SYN-0001:00000:BUY",
            "--policy",
            "B1",
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK
    # The ordered trace table has a header and at least the arrival event.
    assert "step" in captured.out
    assert "kind" in captured.out
    assert "arrival" in captured.out
    assert "SYNTHETIC" in captured.out


def test_trace_task_unknown_task_is_usage_error(tiny_session: Path) -> None:
    code = main(
        [
            "trace",
            "task",
            "--session",
            str(tiny_session),
            "--task-id",
            "SYN-0001:99999:BUY",
            "--policy",
            "B1",
        ]
    )
    assert code == EXIT_USAGE


def test_trace_task_bad_policy_is_usage_error(tiny_session: Path) -> None:
    code = main(
        [
            "trace",
            "task",
            "--session",
            str(tiny_session),
            "--task-id",
            "SYN-0001:00000:BUY",
            "--policy",
            "B9",
        ]
    )
    assert code == EXIT_USAGE


# ---------------------------------------------------------------------------
# usage / dispatch
# ---------------------------------------------------------------------------


def test_no_command_is_usage_error() -> None:
    assert main([]) == EXIT_USAGE


def test_unknown_command_is_usage_error() -> None:
    assert main(["frobnicate"]) == EXIT_USAGE


# ---------------------------------------------------------------------------
# demo + analysis report (slow: full study pipeline)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_demo_quick_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "demo"
    code = main(["demo", "--out", str(out), "--sessions", "2", "--quick"])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    # Reports the minimum-session override explicitly (never silent).
    assert "below the minimum" in captured.out
    report = out / "report" / "report.md"
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "SYNTHETIC-DATA DISCLAIMER" in text


@pytest.mark.slow
def test_analysis_report_on_quick_study(tmp_path: Path) -> None:
    out = tmp_path / "study"
    assert main(["experiment", "run", "--out", str(out), "--quick"]) == EXIT_OK
    # Report is already written by nothing yet; render it explicitly.
    assert main(["analysis", "report", "--study", str(out)]) == EXIT_OK
    report = (out / "report" / "report.md").read_text(encoding="utf-8")
    assert "SYNTHETIC-DATA DISCLAIMER" in report
    assert "Primary paired effect" in report
    assert "## Limitations" in report


def test_analysis_report_missing_study_is_failure(tmp_path: Path) -> None:
    # A directory with no results/ artifacts -> runtime failure (not usage).
    empty = tmp_path / "empty_study"
    empty.mkdir()
    assert main(["analysis", "report", "--study", str(empty)]) == EXIT_FAILURE
