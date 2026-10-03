"""Markdown research report for a finished QExec study (WP7-RELEASE, product section 11).

:func:`write_report` reads a study's persisted artifacts -- ``results/metrics.json``,
``results/report_inputs.json`` and ``results/tables/*.csv`` -- and renders a single Markdown
research report to ``<study_dir>/report/report.md``.

Design constraints (binding):

* The report is **software validation of a synthetic pipeline, never a market finding**. A
  prominent SYNTHETIC-DATA disclaimer leads the document, and the real-data gate (G0) is
  explicitly declared blocked pending paid data.
* Keys are read **defensively**: a missing optional key renders as ``undefined`` /
  ``not estimable`` rather than raising, and unknown extra keys are tolerated (the parallel
  research agent may add keys). A valid COMPLETE status and all sealed artifacts are required.
* The report never prints a claim stronger than the numbers: null intervals and undefined
  effects are rendered literally as ``not estimable`` / ``undefined``; there are no profitability
  or novelty claims anywhere.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from qexec.experiments.integrity import require_complete_study
from qexec.experiments.layout import StudyLayout

__all__ = ["write_report"]

_SYNTHETIC = "SYNTHETIC"
_NA = "undefined"
_NOT_ESTIMABLE = "not estimable"


# ---------------------------------------------------------------------------
# Loading helpers (defensive)
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"required study artifact missing: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return data


def _load_table_csv(path: Path) -> list[dict[str, str]]:
    """Load a tables/*.csv file into a list of row dicts (empty list if absent/empty)."""
    if not path.is_file():
        return []
    text = path.read_text().strip()
    if not text:
        return []
    rows = list(csv.DictReader(text.splitlines()))
    # The pipeline writes an empty-table sentinel ``data_kind,SYNTHETIC`` (a one-column file with
    # no data columns). Treat that single marker row as "no rows".
    if len(rows) == 1 and set(rows[0].keys()) == {"data_kind"}:
        return []
    return rows


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_num(value: Any, *, none_text: str = _NA, places: int = 4) -> str:
    """Format a numeric value, rendering ``None`` as ``none_text`` (never a fake zero)."""
    if value is None:
        return none_text
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return none_text
        return f"{value:.{places}g}"
    return str(value)


def _fmt_interval(pair: Any) -> str:
    """Render a [lo, hi] interval, printing ``not estimable`` when either bound is null."""
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return _NOT_ESTIMABLE
    lo, hi = pair
    if lo is None or hi is None:
        return _NOT_ESTIMABLE
    if isinstance(lo, float) and math.isnan(lo):
        return _NOT_ESTIMABLE
    if isinstance(hi, float) and math.isnan(hi):
        return _NOT_ESTIMABLE
    return f"[{_fmt_num(lo)}, {_fmt_num(hi)}]"


def _ns_to_ms(ns: Any) -> str:
    if ns is None:
        return _NA
    try:
        return f"{int(ns) / 1_000_000:.0f} ms ({int(ns)} ns)"
    except (TypeError, ValueError):
        return str(ns)


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Render a GitHub-flavoured Markdown table as a list of lines."""

    def cell(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("|", "\\|")
            .replace("\r\n", "<br>")
            .replace("\r", "<br>")
            .replace("\n", "<br>")
        )

    out = [
        "| " + " | ".join(cell(value) for value in headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for row in rows:
        out.append("| " + " | ".join(cell(value) for value in row) + " |")
    return out


def _get(mapping: Any, key: str, default: Any = None) -> Any:
    """Defensive ``mapping.get`` that tolerates non-dict inputs."""
    if isinstance(mapping, dict):
        return mapping.get(key, default)
    return default


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_header(metrics: dict[str, Any]) -> list[str]:
    study_id = _get(metrics, "study_id", "unknown")
    return [
        f"# QExec research report — study `{study_id}`",
        "",
        "> **SYNTHETIC-DATA DISCLAIMER.** Every number in this report is produced from "
        "**synthetic** limit-order-book data generated locally by `qexec.synthetic`. These "
        "results are **software validation of the research pipeline, not a market finding**. "
        "They say nothing about any real instrument, venue, or strategy. No profitability, edge, "
        "or novelty is claimed or implied.",
        "",
        "> **Real-data gate (G0) is BLOCKED pending paid data.** The real-data feasibility "
        "decision, real-data audits, and the final real-data holdout evaluation require a "
        "licensed market-data subscription that has not been purchased, plus vendor decoding "
        "and a real-session study entry point that are not implemented. These remain future "
        "work; this build makes no real-data claim.",
        "",
    ]


def _render_research_question(metrics: dict[str, Any], config: dict[str, Any]) -> list[str]:
    primary = _get(metrics, "primary_pair", {})
    baseline = _get(primary, "baseline", "B3_NO_QUEUE")
    candidate = _get(primary, "candidate", "B3")
    return [
        "## Research question",
        "",
        f"On synthetic data, does the queue-aware policy **{candidate}** reduce paired "
        f"implementation-shortfall cost relative to the otherwise-identical no-queue policy "
        f"**{baseline}**, without worsening completion risk (deadline-miss rate), under the "
        "frozen latency / horizon / epsilon protocol? This is a *software-validation* question: "
        "it checks that the estimation machinery runs end to end and reports honest denominators "
        "and intervals. It is not a claim about real markets.",
        "",
        f"Primary configuration: horizon **{_ns_to_ms(_get(metrics, 'primary_horizon_ns'))}**, "
        f"primary latency scenario, epsilon **{_fmt_num(_get(metrics, 'epsilon'))}**, "
        f"fee scenario 0 (gross) with a net variant.",
        "",
    ]


def _render_coverage(metrics: dict[str, Any], tables: dict[str, Any]) -> list[str]:
    n_sessions = _get(metrics, "n_sessions", {})
    lines = [
        "## Data and coverage",
        "",
        f"Data kind: **{_get(metrics, 'data_kind', _SYNTHETIC)}**.",
        "",
    ]
    rows = [
        ["train", _fmt_num(_get(n_sessions, "train"))],
        ["validation", _fmt_num(_get(n_sessions, "validation"))],
        ["test", _fmt_num(_get(n_sessions, "test"))],
    ]
    lines.extend(_md_table(["split", "sessions"], rows))
    lines.append("")

    dist = tables.get("per_policy_distribution", [])
    if dist:
        lines.append("Per-policy task coverage (primary configuration):")
        lines.append("")
        header = [
            "policy",
            "n_tasks",
            "completed_on_time",
            "deadline_miss",
            "technically_unevaluable",
        ]
        drows = [
            [
                str(_get(r, "policy_id")),
                _fmt_num(_get(r, "n_tasks")),
                _fmt_num(_get(r, "n_completed_on_time")),
                _fmt_num(_get(r, "n_deadline_miss")),
                _fmt_num(_get(r, "n_technically_unevaluable")),
            ]
            for r in dist
        ]
        lines.extend(_md_table(header, drows))
        lines.append("")
    else:
        lines.append("_Per-policy distribution table not available._")
        lines.append("")
    return lines


def _render_support(metrics: dict[str, Any], tables: dict[str, Any]) -> list[str]:
    lines = ["## G2-S support and selected horizon", ""]
    lines.append(
        f"Selected primary horizon: **{_ns_to_ms(_get(metrics, 'primary_horizon_ns'))}**. "
        "The horizon is chosen from support counts only (decision-eligible checkpoints and "
        "post-checkpoint queue-depletion fills per session); cost and markout columns cannot "
        "influence the selection."
    )
    lines.append("")
    support = tables.get("support_table", [])
    if support:
        header = ["horizon_ns", "passes", "min_eligible", "min_queue_depletion"]
        rows = [
            [
                _fmt_num(_get(r, "horizon_ns")),
                str(_get(r, "passes")),
                _fmt_num(_get(r, "min_eligible")),
                _fmt_num(_get(r, "min_queue_depletion")),
            ]
            for r in support
        ]
        lines.extend(_md_table(header, rows))
        lines.append("")
    else:
        lines.append("_Support table not available._")
        lines.append("")
    return lines


def _render_primary_effect(metrics: dict[str, Any]) -> list[str]:
    primary = _get(metrics, "primary_pair", {})
    baseline = _get(primary, "baseline", "B3_NO_QUEUE")
    candidate = _get(primary, "candidate", "B3")
    lines = ["## Primary paired effect (cost) and completion risk (miss)", ""]
    lines.append(
        f"Baseline **{baseline}** vs candidate **{candidate}**. Positive cost effect favours the "
        "candidate (lower implementation shortfall); positive miss difference means the candidate "
        "misses the deadline *more* often (worse). Cost is a per-session paired mean on the "
        "common-completion set; the miss difference is over all technically evaluable tasks. "
        "Null intervals are rendered `not estimable`; an undefined effect is rendered `undefined`."
    )
    lines.append("")
    lines.extend(_render_primary_policy_use(metrics))

    cost_effect = _get(primary, "cost_effect_ticks")
    miss_diff = _get(primary, "miss_diff")
    header = [
        "quantity",
        "estimate",
        "95% interval",
        "n_sessions_defined",
        "n_sessions_undefined",
        "n_tasks (denominator)",
    ]
    rows = [
        [
            "cost effect (is_ticks_net)",
            _fmt_num(cost_effect, none_text=_NA),
            _fmt_interval(_get(primary, "cost_ci")),
            _fmt_num(_get(primary, "cost_n_sessions_defined")),
            _fmt_num(_get(primary, "cost_n_sessions_undefined")),
            _fmt_num(_get(primary, "cost_n_tasks")),
        ],
        [
            "completion-risk (miss diff)",
            _fmt_num(miss_diff, none_text=_NA),
            _fmt_interval(_get(primary, "miss_ci")),
            _fmt_num(_get(primary, "miss_n_sessions_defined")),
            _fmt_num(_get(primary, "miss_n_sessions_undefined")),
            _fmt_num(_get(primary, "miss_n_tasks")),
        ],
    ]
    lines.extend(_md_table(header, rows))
    lines.append("")

    # Degeneracy guardrail: flag an undefined effect or an all-zero-miss zero-event bound.
    degenerate = cost_effect is None or _get(primary, "cost_n_tasks") in (0, None)
    zeb = _get(primary, "zero_event_upper_bound")
    if degenerate:
        lines.append(
            "> **DEGENERACY FLAG.** The primary cost effect is **undefined**: there is no "
            "common-completion task set for this pair (no task completed on time under both "
            "policies). No cost effect is estimable; this is reported as a count, not as zero."
        )
        lines.append("")
    if zeb is not None:
        lines.append(
            f"> **Zero-observed-miss note.** Every evaluable candidate task completed on time, so "
            f"the miss rate point estimate is 0. This is **not** a zero-risk claim: the "
            f"one-sided upper bound on the per-session miss probability is "
            f"**{_fmt_num(zeb)}** (`zero_event_session_upper_bound`)."
        )
        lines.append("")
    return lines


def _render_primary_policy_use(metrics: dict[str, Any]) -> list[str]:
    """Disclose the saved decision-use guardrails without recomputing the verdict."""
    lines = ["### Policy use and comparison guardrails", ""]
    guardrails = metrics.get("degeneracy_guardrails")
    if not isinstance(guardrails, dict):
        return [*lines, "_Primary policy-use diagnostics are unavailable._", ""]
    flag = guardrails.get("degenerate_primary_comparison")
    lines.extend([f"Degenerate primary comparison: **{_fmt_num(flag)}**.", ""])
    if flag is True:
        lines.extend(
            [
                "> **DEGENERATE PRIMARY COMPARISON.** This paired effect does not establish a "
                "queue-model advantage. Inspect model use and fallback counts before "
                "interpreting the numerical effect.",
                "",
            ]
        )
    reason = guardrails.get("degeneracy_reason")
    if reason:
        lines.extend([f"Saved guardrail reason: {reason}", ""])
    rows = []
    for policy, key in (("B3", "b3"), ("B3_NO_QUEUE", "b3_no_queue")):
        arm = _get(guardrails, key, {})
        counts = _get(arm, "reason_counts", {})
        rows.append(
            [
                policy,
                _fmt_num(_get(arm, "n_eligible_checkpoints")),
                _fmt_num(_get(arm, "support_rate")),
                _fmt_num(_get(arm, "model_choice_fraction")),
                _fmt_num(_get(counts, "MODEL_CHOICE")),
                _fmt_num(_get(counts, "FALLBACK_UNSUPPORTED")),
                _fmt_num(_get(counts, "FALLBACK_NONFINITE")),
            ]
        )
    lines.extend(
        _md_table(
            [
                "policy",
                "eligible checkpoints",
                "support rate",
                "model-choice fraction",
                "MODEL_CHOICE",
                "FALLBACK_UNSUPPORTED",
                "FALLBACK_NONFINITE",
            ],
            rows,
        )
    )
    lines.extend(
        [
            "",
            "Rates use each policy's eligible checkpoints as their denominator. "
            "The pilot horizon-support gate and model feature-support rate are distinct checks.",
            "",
            "Shared eligible checkpoints: "
            f"**{_fmt_num(_get(guardrails, 'n_shared_eligible_checkpoints'))}**; "
            f"action disagreements: **{_fmt_num(_get(guardrails, 'n_action_disagreements'))}** "
            f"(rate **{_fmt_num(_get(guardrails, 'action_disagreement_rate'))}**).",
            "",
        ]
    )
    return lines


def _render_secondary(tables: dict[str, Any]) -> list[str]:
    lines = ["## Secondary pairs", ""]
    secondary = tables.get("secondary_pairs", [])
    if not secondary:
        lines.append("_Secondary pairs table not available._")
        lines.append("")
        return lines
    header = [
        "baseline",
        "candidate",
        "cost effect",
        "cost interval",
        "miss diff",
        "miss interval",
        "n_tasks",
    ]
    rows = [
        [
            str(_get(r, "baseline")),
            str(_get(r, "candidate")),
            _fmt_num(_get(r, "cost_effect_ticks")),
            _fmt_interval([_get(r, "cost_ci_lo"), _get(r, "cost_ci_hi")]),
            _fmt_num(_get(r, "miss_diff")),
            _fmt_interval([_get(r, "miss_ci_lo"), _get(r, "miss_ci_hi")]),
            _fmt_num(_get(r, "cost_n_tasks")),
        ]
        for r in secondary
    ]
    lines.extend(_md_table(header, rows))
    lines.append("")
    return lines


def _render_latency(tables: dict[str, Any]) -> list[str]:
    lines = [
        "## Latency sensitivity",
        "",
        "Primary pair effect per latency scenario (frozen policies).",
        "",
    ]
    latency = tables.get("latency_table", [])
    if not latency:
        lines.append("_Latency table not available._")
        lines.append("")
        return lines
    header = ["scenario", "cost effect", "cost interval", "miss diff", "n_tasks"]
    rows = [
        [
            str(_get(r, "scenario_id")),
            _fmt_num(_get(r, "cost_effect_ticks")),
            _fmt_interval([_get(r, "cost_ci_lo"), _get(r, "cost_ci_hi")]),
            _fmt_num(_get(r, "miss_diff")),
            _fmt_num(_get(r, "cost_n_tasks")),
        ]
        for r in latency
    ]
    lines.extend(_md_table(header, rows))
    lines.append("")
    return lines


def _render_epsilon(tables: dict[str, Any]) -> list[str]:
    lines = [
        "## Epsilon sensitivity (decision-only)",
        "",
        "Action shares recomputed from stored decision predictions as the risk-allowance epsilon "
        "varies. **Decision-only:** realized outcomes for non-primary epsilons would require a "
        "replay and are *not* claimed here.",
        "",
    ]
    eps = tables.get("epsilon_sensitivity", [])
    if not eps:
        lines.append("_Epsilon sensitivity table not available._")
        lines.append("")
        return lines
    header = ["epsilon", "share_hold", "share_switch", "agreement_with_primary", "is_primary"]
    rows = [
        [
            _fmt_num(_get(r, "epsilon")),
            _fmt_num(_get(r, "share_hold")),
            _fmt_num(_get(r, "share_switch")),
            _fmt_num(_get(r, "agreement_with_primary")),
            str(_get(r, "is_primary_epsilon")),
        ]
        for r in eps
    ]
    lines.extend(_md_table(header, rows))
    lines.append("")
    return lines


def _render_model_diagnostics(metrics: dict[str, Any]) -> list[str]:
    lines = ["## Model diagnostics", ""]
    diag = _get(metrics, "model_diagnostics")
    if isinstance(diag, dict) and diag:
        header = ["diagnostic", "value"]
        rows = [[str(k), _fmt_num(v)] for k, v in sorted(diag.items())]
        lines.extend(_md_table(header, rows))
        lines.append("")
    else:
        lines.append(
            f"Selected thresholds: theta_primary = **{_fmt_num(_get(metrics, 'theta_primary'))}**, "
            f"theta_secondary = **{_fmt_num(_get(metrics, 'theta_secondary'))}**. "
            "Detailed per-model validation diagnostics are recorded in "
            "`stages/development.json` and `stages/validation.json`."
        )
        lines.append("")
    return lines


def _render_distribution(tables: dict[str, Any]) -> list[str]:
    lines = ["## Per-policy outcome distribution", ""]
    dist = tables.get("per_policy_distribution", [])
    if not dist:
        lines.append("_Per-policy distribution table not available._")
        lines.append("")
        return lines
    header = [
        "policy",
        "passive_fill_fraction",
        "mean_is_ticks_net",
        "median_is_ticks_net",
        "miss_rate",
    ]
    rows = [
        [
            str(_get(r, "policy_id")),
            _fmt_num(_get(r, "passive_fill_fraction")),
            _fmt_num(_get(r, "mean_is_ticks_net")),
            _fmt_num(_get(r, "median_is_ticks_net")),
            _fmt_num(_get(r, "miss_rate")),
        ]
        for r in dist
    ]
    lines.extend(_md_table(header, rows))
    lines.append("")
    return lines


def _render_model_calibration(report_inputs: dict[str, Any]) -> list[str]:
    """Validation calibration/error diagnostics per action and variant (R16)."""
    lines = ["## Model calibration and error (validation)", ""]
    diag = _get(report_inputs, "validation_diagnostics")
    cal = _get(diag, "calibration") if isinstance(diag, dict) else None
    disagree = _get(diag, "action_disagreement_rate") if isinstance(diag, dict) else None
    if not isinstance(cal, dict) or not cal:
        lines.append("_Validation calibration diagnostics were not recorded._")
        lines.append("")
        return lines
    lines.append(
        "Per-action miss calibration (Brier score and log loss vs the base-rate predictor) and "
        "cost error (RMSE vs the constant training-mean predictor), computed on the validation "
        "branch labels. A model that beats the reference has a lower log loss / RMSE than its "
        f"reference column. B3 vs B3_NO_QUEUE action-disagreement rate: **{_fmt_num(disagree)}** "
        "(computed with the frozen risk-allowance rule, exactly as the policy decides)."
    )
    lines.append("")
    header = [
        "variant",
        "action",
        "miss_brier",
        "miss_log_loss",
        "base_rate_log_loss",
        "cost_rmse",
        "constant_mean_rmse",
        "n_rows",
        "n_cost_rows",
        "n_cost_missing",
        "n_technical_excluded",
    ]
    rows: list[list[str]] = []
    for variant in ("b3", "b3_no_queue"):
        variant_cal = cal.get(variant) if isinstance(cal, dict) else None
        if not isinstance(variant_cal, dict):
            continue
        for action in ("HOLD", "SWITCH"):
            block = variant_cal.get(action)
            if not isinstance(block, dict):
                continue
            rows.append(
                [
                    variant.upper(),
                    action,
                    _fmt_num(_get(block, "miss_brier")),
                    _fmt_num(_get(block, "miss_log_loss")),
                    _fmt_num(_get(block, "miss_base_rate_log_loss")),
                    _fmt_num(_get(block, "cost_rmse")),
                    _fmt_num(_get(block, "cost_constant_mean_rmse")),
                    _fmt_num(_get(block, "n_rows")),
                    _fmt_num(_get(block, "n_cost_rows")),
                    _fmt_num(_get(block, "n_cost_missing")),
                    _fmt_num(_get(block, "n_technical_excluded")),
                ]
            )
    if rows:
        lines.extend(_md_table(header, rows))
    else:
        lines.append("_No per-action calibration rows available._")
    lines.append("")
    price = _get(diag, "price_validation")
    if isinstance(price, dict):
        lines += [
            "### Price forecasts",
            "",
            "Class-frequency reference probabilities are estimated on training sessions. "
            "Invalid source windows and missing labels are excluded and counted.",
            "",
        ]
        columns = [
            "label",
            "n_rows",
            "n_checkpoint_rows",
            "n_technical_excluded",
            "n_label_missing",
            "log_loss",
            "base_rate_log_loss",
        ]
        price_rows = [
            [label, *[_fmt_num(block.get(c)) for c in columns[1:]]]
            for label, block in price.items()
            if isinstance(block, dict)
        ]
        lines.extend(_md_table(columns, price_rows))
        lines.append("")
    return lines


def _render_unsupported_features(report_inputs: dict[str, Any]) -> list[str]:
    """Diagnostic table: which features most often trigger FALLBACK_UNSUPPORTED (R20)."""
    lines = ["## Support diagnostics: which features trigger unsupported", ""]
    counts = _get(report_inputs, "unsupported_feature_counts")
    has_any = isinstance(counts, dict) and any(
        isinstance(counts.get(v), dict) and counts.get(v) for v in ("b3", "b3_no_queue")
    )
    if not has_any:
        lines.append(
            "_No feature triggered FALLBACK_UNSUPPORTED on the validation checkpoints "
            "(or the diagnostic was not recorded)._"
        )
        lines.append("")
        return lines
    lines.append(
        "Per-feature count of validation checkpoints falling outside the frozen support band "
        "(a row may be out of range on several features). The support region is built from the "
        "exact action-training matrix (R20); these counts explain any fallback rate honestly."
    )
    lines.append("")
    for variant in ("b3", "b3_no_queue"):
        variant_counts = counts.get(variant) if isinstance(counts, dict) else None
        if not isinstance(variant_counts, dict) or not variant_counts:
            continue
        lines.append(f"**{variant.upper()}**")
        lines.append("")
        rows = [[str(name), _fmt_num(n)] for name, n in variant_counts.items()]
        lines.extend(_md_table(["feature", "unsupported_count"], rows))
        lines.append("")
    return lines


def _render_markouts(report_inputs: dict[str, Any]) -> list[str]:
    """Display descriptive execution markouts with their coverage denominators."""
    lines = ["## Execution markouts", ""]
    summary = report_inputs.get("markout_summary")
    if not isinstance(summary, list) or not summary:
        return [*lines, "_Execution markout diagnostics were not recorded._", ""]
    lines.extend(
        [
            "Descriptive post-execution diagnostics at 10 ms, 100 ms and 1 s. References use "
            "only committed midpoints at or before each target, subject to the recorded freshness "
            "limit. Missing references are counted as missing, never zero. These are selected "
            "execution samples, not matched policy-effect estimates or evidence of market alpha.",
            "",
        ]
    )
    records = [row for row in summary if isinstance(row, dict)]
    columns = list(dict.fromkeys(key for row in records for key in row))
    values = [
        [
            str(row.get(key)) if isinstance(row.get(key), str) else _fmt_num(row.get(key))
            for key in columns
        ]
        for row in records
    ]
    lines.extend(_md_table(columns, values))
    return [*lines, ""]


def _render_causal_trace(report_inputs: dict[str, Any]) -> list[str]:
    lines = ["## Causal order trace (one world)", ""]
    trace = _get(report_inputs, "causal_trace")
    if not isinstance(trace, list) or not trace:
        lines.append(
            "_No causal trace was included in `report_inputs.json`; omitted. "
            "Use `qexec trace task` to print a world's causal trace directly._"
        )
        lines.append("")
        return lines
    header = ["step", "time_ns", "kind", "detail"]
    rows = []
    for i, ev in enumerate(trace):
        detail = (
            ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("kind", "time_ns"))
            if isinstance(ev, dict)
            else str(ev)
        )
        rows.append([str(i), str(_get(ev, "time_ns", "")), str(_get(ev, "kind", "")), detail])
    lines.extend(_md_table(header, rows))
    lines.append("")
    return lines


def _render_limitations() -> list[str]:
    return [
        "## Limitations",
        "",
        "- **Replay counterfactual.** The engine replays an immutable historical tape; the "
        "hypothetical order never affects the tape. Market impact and reactive counterparties are "
        "absent. Omitting impact can understate live costs; the overall bias is not guaranteed "
        "to have one sign because fill and queue assumptions also matter.",
        "- **Synthetic dynamics.** The order book is generated by a parametric model, not drawn "
        "from a real venue. Any effect is a property of that model, never a market fact.",
        "- **Real-data validation remains open.** Licensed data, vendor decoding and a "
        "real-session study entry point are not included in this Python synthetic core.",
        "- **FIFO assumption.** Queue position and fills use a strict price-time (FIFO) priority "
        "model with a frozen ambiguity rule; real venues may differ (pro-rata, hidden liquidity, "
        "self-match prevention).",
        "- **No real fills.** All executions are virtual; there is no real fill, slippage, or "
        "exchange acknowledgement latency beyond the modelled constant latency scenarios.",
        "- **Small session counts.** The study uses few synthetic sessions, so paired intervals "
        "are wide and some effects are undefined or not estimable; these are reported as such, "
        "never as zero.",
        "",
    ]


def _render_resources(metrics: dict[str, Any]) -> list[str]:
    resources = metrics.get("resources")
    if not isinstance(resources, dict):
        return []
    return [
        "## Runtime and resources",
        "",
        "Runtime covers generation through analysis tables; report and later trace rendering "
        "are excluded. These measurements describe this synthetic run, not a native speedup.",
        "",
        *_md_table(["measurement", "value"], [[str(k), str(v)] for k, v in resources.items()]),
        "",
    ]


def _render_reproducibility(
    config: dict[str, Any], freeze: dict[str, Any], study_dir: Path
) -> list[str]:
    lines = ["## Reproducibility", ""]
    study_dir = study_dir.resolve()
    reproduction = study_dir.with_name(study_dir.name + "-reproduction")

    def quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    lines.append("Exact commands (PowerShell; data is SYNTHETIC, zero cost, offline):")
    lines.append("")
    lines.append("```powershell")
    lines.append("# Run from the source checkout after installing the locked environment.")
    lines.append("# Use a fresh output directory; all existing study history is immutable.")
    lines.append(
        f"uv run --locked --offline qexec experiment run --out {quote(reproduction)} "
        f"--config {quote(study_dir / 'config.json')}"
    )
    lines.append(f"uv run --locked --offline qexec analysis report --study {quote(reproduction)}")
    lines.append("```")
    lines.append("")
    lines.append("Frozen study configuration:")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(config, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")

    lines.append(f"Freeze manifest: `{study_dir / 'freeze' / 'manifest.json'}`")
    lines.append("")
    if isinstance(freeze, dict):
        git_commit = _get(freeze, "git_commit")
        frozen_at = _get(freeze, "frozen_at")
        lines.append(
            f"- git commit: `{git_commit}`" if git_commit else "- git commit: _unavailable_"
        )
        lines.append(
            f"- frozen at (UTC): `{frozen_at}`" if frozen_at else "- frozen at: _unavailable_"
        )
        hashes = _get(freeze, "model_artifact_hashes", {})
        if isinstance(hashes, dict) and hashes:
            lines.append("- model artifact SHA-256 hashes:")
            for name, digest in sorted(hashes.items()):
                lines.append(f"  - `{name}`: `{digest}`")
        lines.append("")
    lines.append(
        "Test-time models are reloaded from the frozen artifacts on disk (never refit), proving "
        "the artifacts suffice to reproduce the evaluation."
    )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def write_report(study_dir: Path | str) -> Path:
    """Render ``<study_dir>/report/report.md`` from the study's persisted artifacts.

    Reads ``results/metrics.json`` and ``results/report_inputs.json`` (required) and the
    ``results/tables/*.csv`` files (optional, read defensively). Returns the written report path.

    :raises FileNotFoundError: if the required ``metrics.json`` / ``report_inputs.json`` are
        missing (e.g. the study has not been run, or stopped at the support gate before writing
        results).
    """
    study_dir = Path(study_dir)
    results_dir = study_dir / "results"

    freeze = require_complete_study(StudyLayout(study_dir))

    metrics = _load_json(results_dir / "metrics.json")
    report_inputs = _load_json(results_dir / "report_inputs.json")

    # Prefer tables embedded in report_inputs (exact row dicts); fall back to the CSV files.
    tables: dict[str, Any] = {}
    embedded = _get(report_inputs, "tables")
    if isinstance(embedded, dict):
        tables = {k: v for k, v in embedded.items() if isinstance(v, list)}
    tables_dir = results_dir / "tables"
    for name in (
        "primary_pair",
        "secondary_pairs",
        "per_policy_distribution",
        "latency_table",
        "epsilon_sensitivity",
        "support_table",
    ):
        if not tables.get(name):
            tables[name] = _load_table_csv(tables_dir / f"{name}.csv")

    config = _get(report_inputs, "config", {})
    if not isinstance(config, dict):
        config = {}

    lines: list[str] = []
    lines += _render_header(metrics)
    lines += _render_research_question(metrics, config)
    lines += _render_coverage(metrics, tables)
    lines += _render_support(metrics, tables)
    lines += _render_model_diagnostics(metrics)
    lines += _render_primary_effect(metrics)
    lines += _render_secondary(tables)
    lines += _render_latency(tables)
    lines += _render_epsilon(tables)
    lines += _render_distribution(tables)
    lines += _render_model_calibration(report_inputs)
    lines += _render_unsupported_features(report_inputs)
    lines += _render_markouts(report_inputs)
    lines += _render_causal_trace(report_inputs)
    lines += _render_limitations()
    lines += _render_resources(metrics)
    lines += _render_reproducibility(config, freeze, study_dir)

    report_dir = study_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
