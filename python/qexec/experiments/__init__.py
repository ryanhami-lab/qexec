"""The QExec research study pipeline (research spec section 2).

Exposes the frozen :class:`StudyConfig`, the 10-stage :func:`run_study` entry point, and its
:class:`StudyResult`. All results are synthetic software validation (``data_kind = "SYNTHETIC"``).
"""

from __future__ import annotations

from qexec.experiments.config import DATA_KIND, StudyConfig
from qexec.experiments.layout import StudyLayout
from qexec.experiments.pipeline import StudyResult, run_study

__all__ = [
    "DATA_KIND",
    "StudyConfig",
    "StudyLayout",
    "StudyResult",
    "run_study",
]
