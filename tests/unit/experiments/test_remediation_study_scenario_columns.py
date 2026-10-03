from __future__ import annotations

import polars as pl

from qexec.experiments.pipeline import _with_scenario


def test_with_scenario_adds_constant_column_when_absent() -> None:
    out = _with_scenario(pl.DataFrame({"session_id": ["S1", "S2"]}), "L1")
    assert out["scenario_id"].to_list() == ["L1", "L1"]


def test_with_scenario_preserves_existing_identity() -> None:
    frame = pl.DataFrame({"scenario_id": ["L6"]})
    assert _with_scenario(frame, "L1")["scenario_id"].to_list() == ["L6"]


def test_with_scenario_on_empty_frame() -> None:
    out = _with_scenario(pl.DataFrame(schema={"session_id": pl.Utf8}), "L1")
    assert out.columns == ["session_id", "scenario_id"]
    assert out.height == 0
