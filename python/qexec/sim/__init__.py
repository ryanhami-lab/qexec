"""Exchange-side simulation (WP2-OVERLAY).

This package owns the hypothetical exchange-side order for one task:

* :mod:`qexec.sim.overlay` — :class:`~qexec.sim.overlay.ExchangeOverlay`, the per-task
  virtual order that observes the immutable historical feed and emits
  :class:`~qexec.core.messages.ExchangeReport` and
  :class:`~qexec.core.tasks.Execution` objects (architecture section 8).
* :mod:`qexec.sim.evidence` — :class:`~qexec.sim.evidence.ExecutionEvidence`, the audit
  ledger with one entry per economic execution group (architecture section 8.2).
"""

from __future__ import annotations

from qexec.sim.evidence import AmbiguityKind, ExecutionEvidence, ExecutionEvidenceLedger
from qexec.sim.overlay import AMBIGUITY_FILL, ExchangeOverlay

__all__ = [
    "AMBIGUITY_FILL",
    "AmbiguityKind",
    "ExchangeOverlay",
    "ExecutionEvidence",
    "ExecutionEvidenceLedger",
]
