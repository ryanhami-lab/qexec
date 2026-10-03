"""Metric and statistical-protocol functions for QExec (product sections 7, 8, 10).

Analysis code consumes persisted ``TaskResult`` rows only; it never reruns strategy logic.
All economic quantities are computed with exact integer/:class:`fractions.Fraction` arithmetic
internally and converted to :class:`float` only at the boundary (product section 7, architecture
section 11.2).
"""

from __future__ import annotations

from qexec.analysis.metrics import (
    benchmark_offset_ticks,
    deadline_value_ticks,
    implementation_shortfall_ticks,
    markout_ticks,
)
from qexec.analysis.stats import (
    PairedEffect,
    common_completion_set,
    paired_miss_difference,
    session_block_bootstrap,
    session_paired_effect,
    zero_event_session_upper_bound,
)

__all__ = [
    "PairedEffect",
    "benchmark_offset_ticks",
    "common_completion_set",
    "deadline_value_ticks",
    "implementation_shortfall_ticks",
    "markout_ticks",
    "paired_miss_difference",
    "session_block_bootstrap",
    "session_paired_effect",
    "zero_event_session_upper_bound",
]
