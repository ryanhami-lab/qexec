"""R16 reproduction: validation diagnostics use the frozen decision rule + calibration/errors.

The external review found the validation diagnostics used a superseded strict-lexicographic rule
(``min(pred, key=(p_hat, v_hat))``) instead of :func:`risk_allowance_choice` with the config
epsilon and the frozen ``SupportRule`` -- so the reported disagreement rate did not match what
B3Policy actually decides. Calibration (miss Brier / log loss vs base rate) and error (cost RMSE
vs constant mean) diagnostics per action were also missing.

The fix routes the diagnostics through ``risk_allowance_choice`` exactly like B3Policy, and adds
the calibration/error diagnostics. This file asserts the diagnostic keys exist against a quick
study (slow) and that the disagreement computation uses the shared rule (unit).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from qexec.experiments import pipeline
from qexec.experiments.config import StudyConfig
from qexec.experiments.layout import StudyLayout
from qexec.experiments.pipeline import run_study


@pytest.mark.slow
def test_validation_diagnostics_report_calibration_and_errors(tmp_path: Path) -> None:
    out = tmp_path / "study"
    run_study(out, StudyConfig.quick_preset())
    dev = json.loads(StudyLayout(out).development_json.read_text())
    diag = dev["action_diagnostics"]
    # Calibration + error diagnostics per action, per variant (R16).
    assert "calibration" in diag
    cal = diag["calibration"]
    for variant in ("b3", "b3_no_queue"):
        assert variant in cal
        for action in ("HOLD", "SWITCH"):
            block = cal[variant][action]
            # miss Brier + log loss vs base rate, cost RMSE vs constant mean.
            for key in (
                "miss_brier",
                "miss_log_loss",
                "miss_base_rate_log_loss",
                "cost_rmse",
                "cost_constant_mean_rmse",
                "n_rows",
            ):
                assert key in block, f"missing {key} in {variant}/{action}"


def test_diagnostics_use_risk_allowance_choice() -> None:
    # The pipeline must import and use the shared decision rule, not a private lexicographic sort.
    src = inspect.getsource(pipeline._action_diagnostics)
    assert "risk_allowance_choice" in src, "diagnostics must route through risk_allowance_choice"
