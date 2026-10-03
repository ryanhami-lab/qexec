"""R12 reproduction: synthetic-session reuse must honour a provenance record.

The reviewer found that existing sessions were reused whenever their checksums verified, even if
they were generated with a different seed / duration / session count than the current config
asks for. The fix writes ``sessions/provenance.json`` (generator params + a version hash) and
reuses only on an exact match; a mismatch is a hard error with a clear message.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from qexec.experiments.config import StudyConfig
from qexec.experiments.layout import StudyLayout
from qexec.experiments.pipeline import (
    SessionProvenanceError,
    _generate_or_reuse_sessions,
    _session_provenance,
    _write_session_provenance,
)
from qexec.synthetic import generator


def _cfg(**kw: object) -> StudyConfig:
    base = {
        "study_id": "prov",
        "n_sessions": 4,
        "duration_s": 15,
        "base_seed": 0,
        "n_pilot_sessions": 1,
    }
    base.update(kw)
    return StudyConfig(**base)  # type: ignore[arg-type]


def test_provenance_written_on_first_generation(tmp_path: Path) -> None:
    cfg = _cfg()
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    _generate_or_reuse_sessions(cfg, layout)
    prov_path = layout.sessions_dir / "provenance.json"
    assert prov_path.is_file()
    recorded = _session_provenance(prov_path)
    expected = _session_provenance_expected(cfg)
    assert recorded["base_seed"] == expected["base_seed"]
    assert recorded["duration_s"] == expected["duration_s"]
    assert recorded["n_sessions"] == expected["n_sessions"]
    assert (
        recorded["generator_source_sha256"]
        == hashlib.sha256(Path(generator.__file__).read_bytes()).hexdigest()
    )


def _session_provenance_expected(cfg: StudyConfig) -> dict[str, object]:
    # Mirror of the fields the pipeline records (for the test's own assertion).
    return {
        "base_seed": cfg.base_seed,
        "duration_s": cfg.duration_s,
        "n_sessions": cfg.n_sessions,
    }


def test_reuse_on_exact_match_does_not_regenerate(tmp_path: Path) -> None:
    cfg = _cfg()
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    first = _generate_or_reuse_sessions(cfg, layout)
    first_hashes = [(p / "checksums.json").read_text() for p in first]
    second = _generate_or_reuse_sessions(cfg, layout)
    second_hashes = [(p / "checksums.json").read_text() for p in second]
    assert first_hashes == second_hashes  # reused, byte-identical


def test_seed_change_is_hard_error(tmp_path: Path) -> None:
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    _generate_or_reuse_sessions(_cfg(base_seed=0), layout)
    with pytest.raises(SessionProvenanceError, match=r"seed|provenance|mismatch"):
        _generate_or_reuse_sessions(_cfg(base_seed=99), layout)


def test_duration_change_is_hard_error(tmp_path: Path) -> None:
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    _generate_or_reuse_sessions(_cfg(duration_s=15), layout)
    with pytest.raises(SessionProvenanceError, match=r"duration|provenance|mismatch"):
        _generate_or_reuse_sessions(_cfg(duration_s=30), layout)


def test_write_and_read_provenance_round_trip(tmp_path: Path) -> None:
    cfg = _cfg()
    path = tmp_path / "provenance.json"
    _write_session_provenance(path, cfg)
    recorded = _session_provenance(path)
    assert recorded["base_seed"] == cfg.base_seed
    assert recorded["duration_s"] == cfg.duration_s
    assert recorded["n_sessions"] == cfg.n_sessions


def test_sidecar_cannot_certify_sessions_with_different_actual_parameters(tmp_path: Path) -> None:
    cfg = _cfg()
    layout = StudyLayout(tmp_path)
    layout.ensure_dirs()
    sessions = _generate_or_reuse_sessions(cfg, layout)
    session_json = sessions[0] / "session.json"
    metadata = json.loads(session_json.read_text())
    metadata["params"]["seed"] = 99
    session_json.write_text(json.dumps(metadata))
    checksum_path = sessions[0] / "checksums.json"
    checksums = json.loads(checksum_path.read_text())
    checksums["session.json"] = hashlib.sha256(session_json.read_bytes()).hexdigest()
    checksum_path.write_text(json.dumps(checksums))
    with pytest.raises(SessionProvenanceError, match="metadata"):
        _generate_or_reuse_sessions(cfg, layout)
