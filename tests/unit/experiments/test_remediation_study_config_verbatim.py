"""R19 reproduction: saved config loads verbatim; decision logs persisted; report causal trace.

The external review found:

* the saved ``config.json`` could not be fed back through ``qexec experiment run --config`` to
  reproduce the exact study (the ``data_kind`` marker and the merge-onto-preset changed it);
* B3/B3_NO_QUEUE prediction logs were discarded, so the trace CLI could not show a frozen B3 and
  the report had no causal trace.

This file reproduces the config-verbatim requirement directly; the decision-log persistence and
report trace are asserted against a quick study (slow).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from qexec.analysis.report import write_report
from qexec.cli.commands import CommandUsageError
from qexec.cli.commands.experiment import build_study_config
from qexec.experiments.config import DATA_KIND, StudyConfig
from qexec.experiments.layout import StudyLayout
from qexec.experiments.pipeline import run_study


def _args(**kw: object) -> argparse.Namespace:
    base = {"quick": False, "config": None, "sessions": None, "workers": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_saved_config_json_loads_verbatim(tmp_path: Path) -> None:
    # Simulate the exact file the pipeline writes next to a study (config + data_kind marker).
    cfg = StudyConfig.quick_preset()
    saved = {"data_kind": DATA_KIND, **cfg.to_dict()}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(saved, indent=2, sort_keys=True))

    loaded = build_study_config(_args(config=str(cfg_path)))
    assert loaded == cfg, f"saved config did not round-trip verbatim: {loaded} != {cfg}"


def test_saved_full_config_loads_verbatim(tmp_path: Path) -> None:
    cfg = StudyConfig(study_id="study", n_sessions=7, duration_s=90, base_seed=3)
    saved = {"data_kind": DATA_KIND, **cfg.to_dict()}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(saved))
    loaded = build_study_config(_args(config=str(cfg_path)))
    assert loaded == cfg


def test_data_kind_key_is_tolerated_not_required(tmp_path: Path) -> None:
    cfg = StudyConfig.quick_preset()
    cfg_path = tmp_path / "config_no_marker.json"
    cfg_path.write_text(json.dumps(cfg.to_dict()))  # no data_kind key
    loaded = build_study_config(_args(config=str(cfg_path)))
    assert loaded == cfg


def test_non_synthetic_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "real.json"
    path.write_text(json.dumps({"data_kind": "REAL"}))
    with pytest.raises(CommandUsageError, match="only SYNTHETIC"):
        build_study_config(_args(config=str(path)))


@pytest.mark.slow
def test_quick_study_persists_decision_logs_and_report_trace(tmp_path: Path) -> None:
    out = tmp_path / "study"
    run_study(out, StudyConfig.quick_preset())
    layout = StudyLayout(out)

    # Decision logs persisted to results for B3 and B3_NO_QUEUE.
    report_inputs = json.loads(layout.report_inputs_json.read_text())
    assert "b3_decision_log" in report_inputs
    assert "b3_no_queue_decision_log" in report_inputs
    # Each logged decision carries the prediction fields + scenario for epsilon recomputation.
    if report_inputs["b3_decision_log"]:
        row = report_inputs["b3_decision_log"][0]
        for key in ("task_id", "p_hold", "p_switch", "v_hold", "v_switch", "supported", "choice"):
            assert key in row
        assert "scenario_id" in row

    # The report embeds one causal trace stored in report_inputs.
    assert "causal_trace" in report_inputs
    assert isinstance(report_inputs["causal_trace"], list)
    report_path = write_report(out)
    text = report_path.read_text(encoding="utf-8")
    assert "Causal order trace" in text
    # The trace is present (not the "omitted" placeholder) because report_inputs carried it.
    assert "No causal trace was included" not in text
