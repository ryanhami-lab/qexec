"""Unit tests for the Markdown report renderer (:mod:`qexec.analysis.report`).

These tests build minimal ``metrics.json`` / ``report_inputs.json`` artifacts by hand (no study
run) and assert on the rendered Markdown. The focus is honest rendering of nulls: a null interval
must render ``not estimable`` and an undefined (``None``) cost effect must render ``undefined``
with an explicit degeneracy flag -- never a fabricated zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from qexec.analysis.report import write_report
from qexec.experiments.config import StudyConfig
from qexec.experiments.layout import StudyLayout
from qexec.experiments.pipeline import StudyStatus, config_hash, write_status

_DISCLAIMER = "SYNTHETIC-DATA DISCLAIMER"


def _write_study(
    study_dir: Path,
    *,
    primary_pair: dict[str, Any],
    tables: dict[str, Any] | None = None,
    report_inputs_extra: dict[str, Any] | None = None,
) -> None:
    """Write a minimal study artifact set under ``study_dir/results``."""
    results = study_dir / "results"
    (results / "tables").mkdir(parents=True, exist_ok=True)
    metrics: dict[str, Any] = {
        "data_kind": "SYNTHETIC",
        "study_id": "unit",
        "primary_horizon_ns": 1_000_000_000,
        "theta_primary": 0.1,
        "theta_secondary": 0.0,
        "epsilon": 0.001,
        "n_sessions": {"train": 2, "validation": 1, "test": 1},
        "scenarios": ["L1", "L6"],
        "primary_pair": primary_pair,
        "loaded_models_from_disk": True,
        "freeze_manifest": str(study_dir / "freeze" / "manifest.json"),
    }
    (results / "metrics.json").write_text(json.dumps(metrics))
    cfg = StudyConfig.quick_preset(study_id="unit")
    report_inputs: dict[str, Any] = {
        "data_kind": "SYNTHETIC",
        "config": cfg.to_dict(),
        "metrics": metrics,
        "tables": tables or {},
        "primary_horizon_ns": 1_000_000_000,
    }
    if report_inputs_extra:
        report_inputs.update(report_inputs_extra)
    (results / "report_inputs.json").write_text(json.dumps(report_inputs))
    layout = StudyLayout(study_dir)
    layout.freeze_dir.mkdir()
    layout.config_json.write_text(json.dumps(cfg.to_dict()))
    layout.freeze_manifest.write_text(
        json.dumps({"config": cfg.to_dict(), "config_hash": config_hash(cfg)})
    )
    layout.unblinding_log.write_text("access=fixture first_test_access\n")
    write_status(layout, StudyStatus.COMPLETE, config_hash(cfg))


def test_report_renders_null_cost_interval_as_not_estimable(tmp_path: Path) -> None:
    _write_study(
        tmp_path,
        primary_pair={
            "baseline": "B3_NO_QUEUE",
            "candidate": "B3",
            "cost_effect_ticks": 0.25,
            "cost_ci": [None, None],
            "cost_n_sessions_defined": 1,
            "cost_n_sessions_undefined": 0,
            "cost_n_tasks": 10,
            "miss_diff": 0.0,
            "miss_ci": [None, None],
            "miss_n_sessions_defined": 1,
            "miss_n_sessions_undefined": 0,
            "miss_n_tasks": 12,
            "zero_event_upper_bound": None,
        },
    )
    report = write_report(tmp_path)
    text = report.read_text(encoding="utf-8")
    assert "not estimable" in text
    # The estimate itself is still shown; only the null interval is 'not estimable'.
    assert "0.25" in text
    assert _DISCLAIMER in text


def test_report_renders_undefined_cost_effect_with_degeneracy_flag(tmp_path: Path) -> None:
    # No common-completion tasks: cost effect is None and n_tasks is 0 (T32/T50 presentation).
    _write_study(
        tmp_path,
        primary_pair={
            "baseline": "B3_NO_QUEUE",
            "candidate": "B3",
            "cost_effect_ticks": None,
            "cost_ci": [None, None],
            "cost_n_sessions_defined": 0,
            "cost_n_sessions_undefined": 2,
            "cost_n_tasks": 0,
            "miss_diff": 0.0,
            "miss_ci": [None, None],
            "miss_n_sessions_defined": 1,
            "miss_n_sessions_undefined": 0,
            "miss_n_tasks": 8,
            "zero_event_upper_bound": None,
        },
    )
    report = write_report(tmp_path)
    text = report.read_text(encoding="utf-8")
    assert "undefined" in text
    assert "DEGENERACY FLAG" in text
    # No fabricated zero cost claim: the degeneracy sentence states no effect is estimable.
    assert (
        "no common-completion task set" in text.lower()
        or "no cost effect is estimable" in text.lower()
    )


def test_report_renders_zero_event_upper_bound_not_as_zero_risk(tmp_path: Path) -> None:
    _write_study(
        tmp_path,
        primary_pair={
            "baseline": "B3_NO_QUEUE",
            "candidate": "B3",
            "cost_effect_ticks": 0.0,
            "cost_ci": [-0.1, 0.1],
            "cost_n_sessions_defined": 2,
            "cost_n_sessions_undefined": 0,
            "cost_n_tasks": 20,
            "miss_diff": 0.0,
            "miss_ci": [None, None],
            "miss_n_sessions_defined": 2,
            "miss_n_sessions_undefined": 0,
            "miss_n_tasks": 20,
            "zero_event_upper_bound": 0.78,
        },
    )
    report = write_report(tmp_path)
    text = report.read_text(encoding="utf-8")
    assert "0.78" in text
    assert "not a zero-risk claim" in text.lower() or "not** a zero-risk claim" in text.lower()


def test_report_contains_required_sections(tmp_path: Path) -> None:
    _write_study(
        tmp_path,
        primary_pair={
            "baseline": "B3_NO_QUEUE",
            "candidate": "B3",
            "cost_effect_ticks": 0.1,
            "cost_ci": [0.0, 0.2],
            "cost_n_sessions_defined": 2,
            "cost_n_sessions_undefined": 0,
            "cost_n_tasks": 20,
            "miss_diff": 0.0,
            "miss_ci": [-0.05, 0.05],
            "miss_n_sessions_defined": 2,
            "miss_n_sessions_undefined": 0,
            "miss_n_tasks": 20,
            "zero_event_upper_bound": None,
        },
    )
    report = write_report(tmp_path)
    text = report.read_text(encoding="utf-8")
    for heading in (
        "## Research question",
        "## Data and coverage",
        "## G2-S support",
        "## Primary paired effect",
        "## Secondary pairs",
        "## Latency sensitivity",
        "## Epsilon sensitivity (decision-only)",
        "## Per-policy outcome distribution",
        "## Causal order trace",
        "## Limitations",
        "## Reproducibility",
    ):
        assert heading in text, f"missing report section {heading!r}"
    # Real-data gate is declared blocked, no profitability claim.
    assert "BLOCKED pending paid data" in text
    assert "profitability" in text.lower()


def test_report_includes_causal_trace_when_present(tmp_path: Path) -> None:
    trace = [
        {"kind": "arrival", "time_ns": 100},
        {"kind": "checkpoint", "time_ns": 200, "choice": "HOLD"},
        {"kind": "outcome", "time_ns": 300, "status": "COMPLETED_ON_TIME"},
    ]
    _write_study(
        tmp_path,
        primary_pair={
            "baseline": "B3_NO_QUEUE",
            "candidate": "B3",
            "cost_effect_ticks": 0.1,
            "cost_ci": [0.0, 0.2],
            "cost_n_sessions_defined": 1,
            "cost_n_sessions_undefined": 0,
            "cost_n_tasks": 5,
            "miss_diff": 0.0,
            "miss_ci": [None, None],
            "miss_n_sessions_defined": 1,
            "miss_n_sessions_undefined": 0,
            "miss_n_tasks": 5,
            "zero_event_upper_bound": None,
        },
        report_inputs_extra={"causal_trace": trace},
    )
    report = write_report(tmp_path)
    text = report.read_text(encoding="utf-8")
    assert "COMPLETED_ON_TIME" in text
    assert "checkpoint" in text


def test_report_missing_artifacts_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="COMPLETE"):
        write_report(tmp_path)


def test_report_tolerates_extra_unknown_keys(tmp_path: Path) -> None:
    # The parallel research agent may add keys; the renderer must tolerate them.
    _write_study(
        tmp_path,
        primary_pair={
            "baseline": "B3_NO_QUEUE",
            "candidate": "B3",
            "cost_effect_ticks": 0.1,
            "cost_ci": [0.0, 0.2],
            "cost_n_sessions_defined": 1,
            "cost_n_sessions_undefined": 0,
            "cost_n_tasks": 5,
            "miss_diff": 0.0,
            "miss_ci": [None, None],
            "miss_n_sessions_defined": 1,
            "miss_n_sessions_undefined": 0,
            "miss_n_tasks": 5,
            "zero_event_upper_bound": None,
            "some_future_key": 123,
        },
        report_inputs_extra={"another_future_block": {"x": 1}},
    )
    report = write_report(tmp_path)
    assert report.is_file()
