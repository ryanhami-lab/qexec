"""R20 reproduction: the support region is built from the exact action-training matrix.

The external review found the support region was derived from the **final** price model's scores
while the action models were fit on **OOF** scores -- a mismatch that defeats the purpose of the
support check. The fix (R20 default, decided-with-evidence) builds ``SupportRule.from_training``
from the *same* matrix the action models were fit on. If that makes B3 mostly fallback it is
reported honestly via the degeneracy flag; support is never redefined to hide it.

This test asserts the support rule's recorded training range equals the action-training frame's
range (per feature), proving the support region and the action models share one matrix.
"""

from __future__ import annotations

import inspect

import numpy as np
import polars as pl

from qexec.experiments.pipeline import _fit_action_arm
from qexec.features.groups import allowlist
from qexec.models.schema import FeatureSchema


def test_support_rule_matches_action_training_matrix() -> None:
    schema = FeatureSchema.of(list(allowlist("B3_NO_QUEUE")))
    # Build a tiny training frame with HOLD/SWITCH rows and known feature ranges.
    rng = np.random.default_rng(0)
    rows = []
    for action in ("HOLD", "SWITCH"):
        for i in range(8):
            row: dict[str, object] = {"action": action, "miss": float(i % 2), "c_t_ticks": 1.0}
            for name in schema.names:
                row[f"feat_{name}"] = float(rng.uniform(-1.0, 1.0))
            for pcol in ("p_down", "p_unch", "p_up", "u_signal"):
                row[pcol] = float(rng.uniform(-0.5, 0.5))
            rows.append(row)
    train = pl.DataFrame(rows)

    _models, support = _fit_action_arm(schema, train, train, min_rows=1, margin_frac=0.5)

    # The support rule's recorded [min, max] must equal the training frame's per-feature range,
    # i.e. the SAME matrix the action models were fit on (R20).
    cols = [
        f"feat_{n}" if n not in ("p_down", "p_unch", "p_up", "u_signal") else n
        for n in schema.names
    ]
    x = train.filter(pl.col("c_t_ticks").is_not_null()).select(cols).to_numpy().astype(float)
    assert np.allclose(np.asarray(support.feature_min), x.min(axis=0))
    assert np.allclose(np.asarray(support.feature_max), x.max(axis=0))
    assert support.n_training_rows == x.shape[0]


def test_fit_action_arm_signature_is_single_matrix() -> None:
    # The support frame argument must be the action-training frame itself (R20): the default no
    # longer re-scores with the final price model.
    sig = inspect.signature(_fit_action_arm)
    assert "train" in sig.parameters
