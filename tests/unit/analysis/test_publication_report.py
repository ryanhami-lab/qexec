"""Hand-derived publication regressions for honest, readable report output."""

from qexec.analysis.report import _md_table, _render_limitations, _render_primary_effect


def test_primary_report_discloses_all_fallback_comparison() -> None:
    # Twenty-one eligible checkpoints per arm, all unsupported: a numeric zero cost
    # effect does not establish that queue-informed model decisions were compared.
    arm = {
        "n_eligible_checkpoints": 21,
        "support_rate": 0.0,
        "model_choice_fraction": 0.0,
        "reason_counts": {
            "MODEL_CHOICE": 0,
            "FALLBACK_UNSUPPORTED": 21,
            "FALLBACK_NONFINITE": 0,
        },
    }
    text = "\n".join(
        _render_primary_effect(
            {
                "primary_pair": {"cost_effect_ticks": 0.0, "cost_n_tasks": 24},
                "degeneracy_guardrails": {
                    "degenerate_primary_comparison": True,
                    "degeneracy_reason": "Both policies used only fallback decisions.",
                    "b3": arm,
                    "b3_no_queue": arm,
                    "n_shared_eligible_checkpoints": 21,
                    "n_action_disagreements": 0,
                    "action_disagreement_rate": 0.0,
                },
            }
        )
    )
    assert "DEGENERATE PRIMARY COMPARISON" in text
    assert "Both policies used only fallback decisions." in text
    assert "| B3 | 21 | 0 | 0 | 0 | 21 | 0 |" in text
    assert "| B3_NO_QUEUE | 21 | 0 | 0 | 0 | 21 | 0 |" in text
    assert "Shared eligible checkpoints: **21**" in text
    assert "action disagreements: **0** (rate **0**)" in text
    assert "does not establish a queue-model advantage" in text


def test_primary_report_does_not_invent_missing_policy_use_diagnostics() -> None:
    text = "\n".join(
        _render_primary_effect({"primary_pair": {"cost_effect_ticks": 0.0, "cost_n_tasks": 24}})
    )
    assert "Primary policy-use diagnostics are unavailable" in text
    assert "DEGENERATE PRIMARY COMPARISON" not in text


def test_trace_table_escapes_command_ids_and_multiline_cells() -> None:
    rows = _md_table(
        ["command_id", "detail"],
        [["SYN-0004:00000:BUY|B3:cmd:0:PASSIVE", "first\nsecond"]],
    )
    assert rows[2] == "| SYN-0004:00000:BUY\\|B3:cmd:0:PASSIVE | first<br>second |"


def test_report_does_not_assign_a_guaranteed_replay_bias_sign() -> None:
    text = "\n".join(_render_limitations())
    assert "costs are optimistic" not in text
    assert "overall bias is not guaranteed to have one sign" in text
