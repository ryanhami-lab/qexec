from __future__ import annotations

from qexec.sim.engine import _frame


def test_frame_infers_schema_from_all_rows() -> None:
    # 150 leading rows with a None optional column, then a populated one (default polars
    # inference only scans 100 rows and would raise).
    rows = [{"task_id": f"t{i}", "reason": None} for i in range(150)]
    rows.append({"task_id": "late", "reason": "NO_VALID_HISTORICAL_MID"})
    df = _frame(rows, ("task_id", "reason"))
    assert df.height == 151
    assert df["reason"][150] == "NO_VALID_HISTORICAL_MID"
