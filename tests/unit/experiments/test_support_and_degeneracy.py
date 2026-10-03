"""Unit tests for the support-margin construction and degeneracy guardrails (defect fix).

Covers:
* ``_support_margins`` -> per-feature margin ``frac * (max - min)`` (0 for constant features),
  and the resulting ``SupportRule`` supports a value slightly outside the training range but
  inside the margin, and rejects a value far outside.
* ``_degeneracy_guardrails`` -> reason counts, model-choice fraction, support rate, and the
  ``degenerate_primary_comparison`` flag (false in the healthy case; true when all decisions are
  fallbacks and the two variants are identical, and when a variant has zero eligible decisions).
"""

from __future__ import annotations

import numpy as np
import polars as pl

from qexec.core.tasks import DecisionReason
from qexec.experiments.pipeline import (
    _degeneracy_guardrails,
    _support_margins,
    _variant_reason_counts,
)
from qexec.models.schema import FeatureSchema, SupportRule

# ---------------------------------------------------------------------------
# Support margin construction
# ---------------------------------------------------------------------------


def test_support_margins_are_half_spread_and_zero_for_constant() -> None:
    # Feature 0 spans [0, 4] (spread 4), feature 1 is constant at 7 (spread 0).
    x = np.array([[0.0, 7.0], [2.0, 7.0], [4.0, 7.0]])
    margins = _support_margins(x, n_features=2, margin_frac=0.5)
    # 0.5 * 4 = 2.0 ; 0.5 * 0 = 0.0 (constant feature keeps a finite zero margin).
    assert margins == (2.0, 0.0)


def test_support_margins_empty_matrix_is_all_zero() -> None:
    assert _support_margins(np.empty((0, 3)), n_features=3, margin_frac=0.5) == (0.0, 0.0, 0.0)


def test_support_rule_with_margin_admits_near_and_rejects_far() -> None:
    schema = FeatureSchema.of(["x", "y"])
    # x in [0, 4] -> margin 2.0 ; y in [10, 30] -> margin 10.0 (margin_frac 0.5).
    x = np.array([[0.0, 10.0], [2.0, 20.0], [4.0, 30.0]])
    margins = _support_margins(x, n_features=2, margin_frac=0.5)
    rule = SupportRule.from_training(schema, x, min_training_rows=1, margin=margins)
    # Slightly outside the training range of x (max 4) but inside the margin (<= 6): supported.
    assert rule.is_supported({"x": 5.0, "y": 20.0})
    assert rule.is_supported({"x": 4.0 + 2.0, "y": 10.0 - 10.0})  # exactly on both margins (<=)
    # Far outside the margin on x (> 6): unsupported.
    assert not rule.is_supported({"x": 6.1, "y": 20.0})
    # Far outside the margin on y (< 0): unsupported.
    assert not rule.is_supported({"x": 2.0, "y": -0.1})


def test_constant_feature_margin_still_checks_equality() -> None:
    schema = FeatureSchema.of(["c"])
    x = np.array([[7.0], [7.0], [7.0]])  # constant -> spread 0 -> margin 0
    margins = _support_margins(x, n_features=1, margin_frac=0.5)
    rule = SupportRule.from_training(schema, x, min_training_rows=1, margin=margins)
    assert rule.is_supported({"c": 7.0})
    assert not rule.is_supported({"c": 7.1})  # even a tiny deviation is unsupported


# ---------------------------------------------------------------------------
# Degeneracy guardrails
# ---------------------------------------------------------------------------

_MC = DecisionReason.MODEL_CHOICE.value
_UNSUP = DecisionReason.FALLBACK_UNSUPPORTED.value


def _log_row(task_id: str, time_ns: int, choice: str, reason: str, supported: bool) -> dict:
    return {
        "task_id": task_id,
        "time_ns": time_ns,
        "p_hold": 0.1,
        "p_switch": 0.2,
        "v_hold": 1.0,
        "v_switch": 2.0,
        "supported": supported,
        "choice": choice,
        "reason": reason,
    }


def test_variant_reason_counts_covers_all_known_reasons() -> None:
    log = [
        _log_row("t1", 1, "HOLD", _MC, True),
        _log_row("t2", 2, "SWITCH", _UNSUP, False),
    ]
    counts = _variant_reason_counts(log)
    assert counts[_MC] == 1
    assert counts[_UNSUP] == 1
    assert counts[DecisionReason.FALLBACK_NONFINITE.value] == 0


def test_guardrails_healthy_case_flag_false() -> None:
    # Both variants decide the majority by model choice and disagree on some checkpoints.
    b3 = [
        _log_row("t1", 1, "HOLD", _MC, True),
        _log_row("t2", 2, "SWITCH", _MC, True),
        _log_row("t3", 3, "HOLD", _MC, True),
        _log_row("t4", 4, "SWITCH", _UNSUP, False),
    ]
    b3_nq = [
        _log_row("t1", 1, "SWITCH", _MC, True),  # disagrees with B3 on t1
        _log_row("t2", 2, "SWITCH", _MC, True),
        _log_row("t3", 3, "HOLD", _MC, True),
        _log_row("t4", 4, "HOLD", _MC, True),  # disagrees with B3 on t4
    ]
    out = _degeneracy_guardrails(b3, b3_nq, pl.DataFrame())
    assert out["b3"]["model_choice_fraction"] == 0.75
    assert out["b3_no_queue"]["model_choice_fraction"] == 1.0
    assert out["b3"]["support_rate"] == 0.75
    assert out["n_shared_eligible_checkpoints"] == 4
    assert out["n_action_disagreements"] == 2
    assert out["action_disagreement_rate"] == 0.5
    assert out["degenerate_primary_comparison"] is False
    assert out["degeneracy_reason"] is None


def test_guardrails_all_fallback_identical_flag_true() -> None:
    # Reproduces the original defect: every eligible decision is FALLBACK_UNSUPPORTED (SWITCH),
    # so the two variants are identical on 100% of shared checkpoints and both are fallback-
    # dominated -> degenerate.
    b3 = [_log_row(f"t{i}", i, "SWITCH", _UNSUP, False) for i in range(5)]
    b3_nq = [_log_row(f"t{i}", i, "SWITCH", _UNSUP, False) for i in range(5)]
    out = _degeneracy_guardrails(b3, b3_nq, pl.DataFrame())
    assert out["b3"]["model_choice_fraction"] == 0.0
    assert out["b3_no_queue"]["model_choice_fraction"] == 0.0
    assert out["action_disagreement_rate"] == 0.0
    assert out["degenerate_primary_comparison"] is True
    assert out["degeneracy_reason"] is not None
    assert "fallback" in out["degeneracy_reason"].lower()


def test_guardrails_low_model_choice_fraction_flag_true() -> None:
    # B3 decides < 50% by model choice -> degenerate even if the variants disagree sometimes.
    b3 = [
        _log_row("t1", 1, "SWITCH", _UNSUP, False),
        _log_row("t2", 2, "SWITCH", _UNSUP, False),
        _log_row("t3", 3, "HOLD", _MC, True),
    ]
    b3_nq = [
        _log_row("t1", 1, "HOLD", _MC, True),
        _log_row("t2", 2, "HOLD", _MC, True),
        _log_row("t3", 3, "HOLD", _MC, True),
    ]
    out = _degeneracy_guardrails(b3, b3_nq, pl.DataFrame())
    assert out["b3"]["model_choice_fraction"] < 0.5
    assert out["degenerate_primary_comparison"] is True
    assert "B3 model-choice fraction" in out["degeneracy_reason"]


def test_guardrails_zero_eligible_variant_flag_true() -> None:
    b3: list[dict] = []
    b3_nq = [_log_row("t1", 1, "HOLD", _MC, True)]
    out = _degeneracy_guardrails(b3, b3_nq, pl.DataFrame())
    assert out["b3"]["n_eligible_checkpoints"] == 0
    assert out["degenerate_primary_comparison"] is True
    assert "zero eligible checkpoints" in out["degeneracy_reason"]
