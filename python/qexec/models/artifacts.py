"""Serializable model artifact (architecture 10.3).

A ``ModelArtifact`` records enough metadata and parameters to reproduce a model's
predictions exactly: feature names/order, training session ids, preprocessing summary,
hyperparameters, seed, and sklearn/numpy versions. Serialization is **pickle-free and
joblib-free**: a human-readable JSON sidecar plus a NumPy ``.npz`` for arrays. A reloaded
artifact reproduces predictions bit-for-bit because the prediction path (scale + linear +
softmax/logistic) is reconstructed from the stored coefficients, not from a re-fitted
estimator.

Schema mismatch on load or at predict time is a hard error (``SchemaMismatchError``), never a
silently different prediction (T52).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import sklearn

from qexec.models.schema import FeatureSchema

__all__ = ["ModelArtifact"]

_ARTIFACT_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """Portable, pickle-free record of a fitted model.

    Attributes:
        model_kind: a short tag, e.g. ``"price"`` or ``"action"``.
        schema: the frozen feature schema (names + order).
        training_sessions: chronological session ids the model was trained on.
        preprocessing: short description of preprocessing (e.g. standardization).
        hyperparameters: JSON-serializable hyperparameter record (grids, chosen values).
        seed: the fixed random seed used for fitting.
        state: the model's parameter dict (from ``to_state``); arrays stored in the npz.
        sklearn_version / numpy_version: dependency versions captured at save time.
        format_version: artifact format version.
    """

    model_kind: str
    schema: FeatureSchema
    training_sessions: tuple[str, ...]
    preprocessing: str
    hyperparameters: dict[str, Any]
    seed: int
    state: dict[str, Any]
    sklearn_version: str = field(default_factory=lambda: sklearn.__version__)
    numpy_version: str = field(default_factory=lambda: np.__version__)
    format_version: int = _ARTIFACT_FORMAT_VERSION

    # -- construction ----------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        model_kind: str,
        *,
        schema: FeatureSchema,
        training_sessions: list[str] | tuple[str, ...],
        preprocessing: str,
        hyperparameters: dict[str, Any],
        seed: int,
        state: dict[str, Any],
    ) -> ModelArtifact:
        return cls(
            model_kind=model_kind,
            schema=schema,
            training_sessions=tuple(training_sessions),
            preprocessing=preprocessing,
            hyperparameters=hyperparameters,
            seed=seed,
            state=state,
        )

    # -- serialization ---------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write ``<path>.json`` (metadata + scalar state) and ``<path>.npz`` (arrays).

        The JSON stores every non-array value and, for each array, a reference token; the
        npz stores the arrays themselves. Writing is deterministic (sorted keys).
        """
        path = Path(path)
        arrays: dict[str, np.ndarray] = {}
        json_state = _split_arrays(self.state, arrays, prefix="state")
        meta = {
            "format_version": self.format_version,
            "model_kind": self.model_kind,
            "feature_names": list(self.schema.names),
            "training_sessions": list(self.training_sessions),
            "preprocessing": self.preprocessing,
            "hyperparameters": self.hyperparameters,
            "seed": self.seed,
            "sklearn_version": self.sklearn_version,
            "numpy_version": self.numpy_version,
            "state": json_state,
            "array_keys": sorted(arrays.keys()),
        }
        json_path = path.with_suffix(".json")
        npz_path = path.with_suffix(".npz")
        json_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        # Always write an npz (possibly empty) so the pair is self-consistent.
        _save_npz(npz_path, arrays)

    @classmethod
    def load(cls, path: Path, expected_schema: FeatureSchema | None = None) -> ModelArtifact:
        """Load an artifact. If ``expected_schema`` is given, schema mismatch is a hard error."""
        path = Path(path)
        meta = json.loads(path.with_suffix(".json").read_text())
        if meta.get("format_version") != _ARTIFACT_FORMAT_VERSION:
            raise ValueError(f"unsupported artifact format version {meta.get('format_version')!r}")
        schema = FeatureSchema.of([str(n) for n in meta["feature_names"]])
        if expected_schema is not None:
            # Hard error on any name/order mismatch (T52).
            expected_schema.validate(list(schema.names))
            schema = expected_schema
        with np.load(path.with_suffix(".npz")) as npz:
            arrays = {key: np.asarray(npz[key]) for key in npz.files}
        state = _rejoin_arrays(meta["state"], arrays)
        return cls(
            model_kind=meta["model_kind"],
            schema=schema,
            training_sessions=tuple(meta["training_sessions"]),
            preprocessing=meta["preprocessing"],
            hyperparameters=meta["hyperparameters"],
            seed=int(meta["seed"]),
            state=state,
            sklearn_version=meta["sklearn_version"],
            numpy_version=meta["numpy_version"],
            format_version=int(meta["format_version"]),
        )


_ARRAY_TAG = "__ndarray__"


def _save_npz(npz_path: Path, arrays: dict[str, np.ndarray]) -> None:
    """Write ``arrays`` to an uncompressed ``.npz`` by name.

    Wrapped in a helper because numpy's ``savez`` stub overloads a positional ``allow_pickle``
    with ``**kwds``; passing a ``dict`` mapping directly keeps the call type-checkable.
    """
    with npz_path.open("wb") as fh:
        # numpy's savez stub overloads a positional ``allow_pickle: bool`` ahead of ``**kwds``,
        # so a ``**dict[str, ndarray]`` splat is wrongly matched against it. Our keys are
        # schema-derived ("state...") and never "allow_pickle", so the call is sound.
        np.savez(fh, **arrays)  # type: ignore[arg-type]


def _split_arrays(value: Any, arrays: dict[str, np.ndarray], prefix: str) -> Any:
    """Recursively replace ndarrays with reference tokens, collecting them in ``arrays``.

    Lists that are nested numeric data remain inline in JSON; only actual ``np.ndarray``
    instances are externalized. This keeps small scalar state readable while storing matrices
    efficiently and exactly in the npz.
    """
    if isinstance(value, np.ndarray):
        key = f"{prefix}"
        arrays[key] = value
        return {_ARRAY_TAG: key}
    if isinstance(value, dict):
        return {k: _split_arrays(v, arrays, prefix=f"{prefix}.{k}") for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_split_arrays(v, arrays, prefix=f"{prefix}[{i}]") for i, v in enumerate(value)]
    return value


def _rejoin_arrays(value: Any, arrays: dict[str, np.ndarray]) -> Any:
    """Inverse of :func:`_split_arrays`: replace reference tokens with their arrays."""
    if isinstance(value, dict):
        if set(value.keys()) == {_ARRAY_TAG}:
            return arrays[value[_ARRAY_TAG]]
        return {k: _rejoin_arrays(v, arrays) for k, v in value.items()}
    if isinstance(value, list):
        return [_rejoin_arrays(v, arrays) for v in value]
    return value
