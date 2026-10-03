"""Source adapters: event-batch assembly and pre-replay diagnostics (WP1-BOOK)."""

from __future__ import annotations

from qexec.adapters.batches import (
    BatchAssembler,
    iter_batches,
    iter_session_batches,
)
from qexec.adapters.diagnostics import (
    Anomaly,
    DuplicateReport,
    detect_duplicate_records,
    detect_overlapping_sessions,
)

__all__ = [
    "Anomaly",
    "BatchAssembler",
    "DuplicateReport",
    "detect_duplicate_records",
    "detect_overlapping_sessions",
    "iter_batches",
    "iter_session_batches",
]
