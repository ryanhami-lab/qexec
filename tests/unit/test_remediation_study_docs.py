"""R21 reproduction: documentation honesty checks.

The external review found the docs overclaimed: the README gate table said "Validated", the
real-data path implied a CLI input that does not exist, and ``ci.ps1`` set ``QEXEC_OFFLINE``
implying ``uv`` ran offline. These checks pin the corrected wording so a regression is caught.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_readme_gate_table_uses_implemented_and_tested_wording() -> None:
    text = _read("README.md")
    assert "Implemented and tested on synthetic data" in text
    # The old overclaiming "Validated on synthetic data" wording is gone from the gate table.
    assert "**Validated on synthetic data**" not in text
    # The gate table links to the remediation file.
    assert "docs/remediation.md" in text


def test_reproducing_states_real_data_adapter_not_implemented() -> None:
    text = _read("docs/reproducing.md")
    assert "not implemented" in text.lower()
    assert "real-data adapter" in text.lower()


def test_ci_ps1_has_no_qexec_offline_and_documents_first_install() -> None:
    text = _read("scripts/ci.ps1")
    assert "QEXEC_OFFLINE" not in text
    # The first-install PyPI download is documented honestly.
    assert "first install" in text.lower() or "first" in text.lower()
    assert "uv.lock" in text


def test_traceability_marks_t42_and_t57_honestly() -> None:
    text = _read("docs/test_traceability.md")
    # T57 is partial because P1 is not built.
    assert "| T57 |" in text
    t57_line = next(line for line in text.splitlines() if line.startswith("| T57 |"))
    assert "partial" in t57_line.lower()
    # T42 was partial until the R1 remediation added the failed-branch and late-fill cases; it may
    # now claim coverage only by citing those tests, and they must exist.
    t42_line = next(line for line in text.splitlines() if line.startswith("| T42 |"))
    r1_tests = _read("tests/unit/sim/test_remediation_engine_r1.py")
    for name in (
        "test_R1_T42_failed_hold_branch_misses",
        "test_R1_T42_late_fill_counts_as_miss_excluded_from_value",
    ):
        assert name in t42_line
        assert f"def {name}(" in r1_tests


def test_traceability_cites_only_existing_tests() -> None:
    """Every ``tests/...py::test_x`` reference in the traceability doc points at a real test."""
    text = _read("docs/test_traceability.md")
    refs = re.findall(r"(tests/[A-Za-z0-9_/]+\.py)::(test_[A-Za-z0-9_]+)", text)
    assert refs
    for rel, func in refs:
        assert f"def {func}(" in _read(rel), f"{rel}::{func} does not exist"
    for rel in set(re.findall(r"tests/[A-Za-z0-9_/]+\.py", text)):
        assert (_ROOT / rel).is_file(), f"{rel} does not exist"
