"""Integrity checks for immutable, portable study outputs.

Hashes detect accidental edits and stale artifacts; they are not signatures or a defense
against an attacker who can rewrite the entire study. The audit log's completed prefix is
sealed separately so later trace access can be appended without replacing earlier history.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from qexec.core.records_io import verify_session
from qexec.experiments.config import StudyConfig
from qexec.experiments.layout import StudyLayout


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def configuration_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def artifact_hashes(layout: StudyLayout) -> dict[str, str]:
    """Seal outputs plus dataset checksum manifests, using only relative paths."""
    paths = [layout.config_json]
    for directory in (layout.stages_dir, layout.freeze_dir, layout.results_dir):
        paths.extend(p for p in directory.rglob("*") if p.is_file() and p != layout.unblinding_log)
    paths.extend(layout.sessions_dir.glob("*/checksums.json"))
    provenance = layout.sessions_dir / "provenance.json"
    if provenance.is_file():
        paths.append(provenance)
    return {p.relative_to(layout.root).as_posix(): sha256_file(p) for p in sorted(paths)}


def _unblinding_prefix_digest(path: Path, byte_length: int) -> str:
    digest = hashlib.sha256()
    remaining = byte_length
    try:
        with path.open("rb") as handle:
            while remaining:
                block = handle.read(min(remaining, 1 << 20))
                if not block:
                    raise ValueError("COMPLETE study unblinding log is truncated")
                digest.update(block)
                remaining -= len(block)
    except OSError as exc:
        raise ValueError(f"COMPLETE study unblinding log is missing or unreadable: {path}") from exc
    return digest.hexdigest()


def seal_unblinding_log(layout: StudyLayout) -> dict[str, int | str]:
    """Bind every audit-log byte present at completion while permitting later appends."""
    try:
        byte_length = layout.unblinding_log.stat().st_size
    except OSError as exc:
        raise ValueError("COMPLETE study requires a nonempty unblinding log") from exc
    if byte_length == 0:
        raise ValueError("COMPLETE study requires a nonempty unblinding log")
    return {
        "byte_length": byte_length,
        "sha256": _unblinding_prefix_digest(layout.unblinding_log, byte_length),
    }


def _verify_unblinding_prefix(layout: StudyLayout, seal: object) -> None:
    if not isinstance(seal, dict):
        raise ValueError("COMPLETE study is missing its unblinding log prefix seal")
    byte_length = seal.get("byte_length")
    if not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length <= 0:
        raise ValueError("COMPLETE study has an invalid unblinding log prefix length")
    digest = _unblinding_prefix_digest(layout.unblinding_log, byte_length)
    if digest != seal.get("sha256"):
        raise ValueError("COMPLETE study unblinding log prefix was replaced or modified")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid or missing COMPLETE study artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid COMPLETE study artifact {path}: expected JSON object")
    return value


def require_complete_study(layout: StudyLayout) -> dict[str, Any]:
    """Fail closed unless status, config, frozen inputs and saved outputs agree."""
    status = _read_object(layout.status_json)
    if status.get("state") != "COMPLETE":
        raise ValueError(f"study status must be COMPLETE, got {status.get('state')!r}")
    hashes = status.get("artifact_hashes")
    required = {
        "config.json",
        "freeze/manifest.json",
        "results/metrics.json",
        "results/report_inputs.json",
    }
    if not isinstance(hashes, dict) or not required.issubset(hashes):
        raise ValueError("COMPLETE study is missing its artifact integrity seal")
    _verify_unblinding_prefix(layout, status.get("unblinding_log_seal"))
    try:
        actual = artifact_hashes(layout)
    except OSError as exc:
        raise ValueError(f"COMPLETE study artifact missing: {exc}") from exc
    if actual != hashes:
        changed = sorted(k for k in set(actual) | set(hashes) if actual.get(k) != hashes.get(k))
        raise ValueError(f"COMPLETE study integrity mismatch: {changed}")
    config = _read_object(layout.config_json)
    config.pop("data_kind", None)
    StudyConfig.from_dict(config)
    if configuration_hash(config) != status.get("config_hash"):
        raise ValueError("COMPLETE study config hash mismatch")
    manifest = _read_object(layout.freeze_manifest)
    if manifest.get("config") != config or manifest.get("config_hash") != status["config_hash"]:
        raise ValueError("COMPLETE study freeze/config mismatch")
    return manifest


def verify_frozen_inputs(layout: StudyLayout, manifest: dict[str, Any]) -> None:
    """Check models and session contents before a new access to frozen test data."""
    names = {
        f"{name}.{suffix}"
        for name in ("price_primary", "price_secondary", "action_b3", "action_b3_no_queue")
        for suffix in ("json", "npz")
    }
    names.update(("support_b3.json", "support_b3_no_queue.json"))
    models = manifest.get("model_artifact_hashes")
    if not isinstance(models, dict) or set(models) != names:
        raise ValueError("frozen model hash manifest is incomplete")
    for name in sorted(names):
        if sha256_file(layout.freeze_dir / name) != models[name]:
            raise ValueError(f"frozen model hash mismatch: {name}")
    checksums = manifest.get("session_checksums")
    splits = manifest.get("splits")
    if not isinstance(checksums, dict) or not isinstance(splits, dict):
        raise ValueError("frozen session manifest is incomplete")
    sessions = [sid for key in ("train", "validation", "test") for sid in splits.get(key, [])]
    if not sessions or len(sessions) != len(set(sessions)) or set(sessions) != set(checksums):
        raise ValueError("frozen session split/checksum mismatch")
    for sid in sessions:
        if not isinstance(sid, str) or Path(sid).name != sid or sid in (".", ".."):
            raise ValueError("invalid frozen session identifier")
        session_dir = layout.sessions_dir / sid
        if sha256_file(session_dir / "checksums.json") != checksums[sid]:
            raise ValueError(f"frozen session checksum manifest mismatch: {sid}")
        verify_session(session_dir)
