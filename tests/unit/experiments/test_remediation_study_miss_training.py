"""R10 (C half) reproduction: null-cost rows are never dropped from miss training.

The reviewer found that deadline valuation used ``mid_at`` even when the state was invalid, so
null-cost rows were silently dropped from miss training. The study-plane half of the fix: the
cost model trains on non-null-cost rows only (counted), but the miss model trains on **every**
evaluable action row -- a null cost never removes a row from miss training.

Rows flagged ``technically_unevaluable`` (R4) are excluded from both (counted) when the engine
provides that column.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from qexec.core.tasks import CheckpointChoice
from qexec.experiments.pipeline import _evaluable_rows, _fit_action_arm
from qexec.features.groups import allowlist
from qexec.models.schema import FeatureSchema


def _frame_with_null_cost(schema: FeatureSchema, n_null: int, n_full: int) -> pl.DataFrame:
    rng = np.random.default_rng(1)
    rows = []
    for action in ("HOLD", "SWITCH"):
        for i in range(n_full):
            row: dict[str, object] = {"action": action, "miss": float(i % 2), "c_t_ticks": 1.0 + i}
            for name in schema.names:
                row[f"feat_{name}"] = float(rng.uniform(-1, 1))
            for p in ("p_down", "p_unch", "p_up", "u_signal"):
                row[p] = float(rng.uniform(-0.3, 0.3))
            rows.append(row)
        for _j in range(n_null):
            row = {"action": action, "miss": 1.0, "c_t_ticks": None}
            for name in schema.names:
                row[f"feat_{name}"] = float(rng.uniform(-1, 1))
            for p in ("p_down", "p_unch", "p_up", "u_signal"):
                row[p] = float(rng.uniform(-0.3, 0.3))
            rows.append(row)
    return pl.DataFrame(rows)


def test_miss_model_keeps_null_cost_rows() -> None:
    schema = FeatureSchema.of(list(allowlist("B3_NO_QUEUE")))
    train = _frame_with_null_cost(schema, n_null=3, n_full=5)
    models, _support = _fit_action_arm(schema, train, train, min_rows=1, margin_frac=0.5)
    # Per action: 5 non-null + 3 null = 8 rows feed the miss model; cost sees only the 5 non-null.
    for action in (CheckpointChoice.HOLD, CheckpointChoice.SWITCH):
        assert models.miss_n(action) == 8, f"miss model dropped null-cost rows for {action}"


def test_evaluable_rows_excludes_technically_unevaluable_when_present() -> None:
    frame = pl.DataFrame(
        {
            "action": ["HOLD", "HOLD", "SWITCH"],
            "technically_unevaluable": [False, True, None],
            "c_t_ticks": [1.0, 2.0, 3.0],
        }
    )
    out = _evaluable_rows(frame)
    # The True-flagged row is dropped; False and null (treated as evaluable) remain.
    assert out.height == 2
    assert out.get_column("technically_unevaluable").to_list() == [False, None]


def test_evaluable_rows_passthrough_when_column_absent() -> None:
    frame = pl.DataFrame({"action": ["HOLD"], "c_t_ticks": [1.0]})
    assert _evaluable_rows(frame).height == 1
