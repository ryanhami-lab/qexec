from __future__ import annotations

import pytest

from qexec.core.config import (
    LATENCY_SCENARIOS,
    ExperimentConfig,
    S,
    arrival_spacing_ns,
    checkpoint_valid,
    guard_ns,
    primary_price_tau_ns,
)
from qexec.core.types import BookLevel, BookSnapshot, Side, TaskSide, TimeNs, TradingStatus


def test_primary_guard_matches_spec() -> None:
    # Product 5.3 / architecture 7.3: G = 1.400001 ms under L1.
    assert LATENCY_SCENARIOS["L1"].guard_ns == 1_400_001
    assert guard_ns(0, 0, 0) == 1


def test_checkpoint_validity_is_exact() -> None:
    g = LATENCY_SCENARIOS["L1"].guard_ns
    assert checkpoint_valid(1 * S, g)
    # H/2 == H - G is invalid (strict inequality).
    assert not checkpoint_valid(2 * g, g)
    assert checkpoint_valid(2 * g + 2, g)
    assert not checkpoint_valid(0, g)


def test_l6_guard_and_horizon() -> None:
    g6 = LATENCY_SCENARIOS["L6"].guard_ns
    assert g6 == 3 * (500_000 + 2_500_000) + 2 * 2_500_000 + 1
    assert checkpoint_valid(1 * S, g6)


def test_primary_tau() -> None:
    assert primary_price_tau_ns(1 * S) == 498_599_999


def test_arrival_spacing() -> None:
    assert arrival_spacing_ns(1 * S) == 10 * S
    assert arrival_spacing_ns(30 * S) == 60 * S


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown configuration keys"):
        ExperimentConfig.from_dict({"experiment_id": "x", "bogus": 1})


def test_config_rejects_invalid_guard() -> None:
    with pytest.raises(ValueError):
        ExperimentConfig.from_dict({"experiment_id": "x", "horizon_ns": 2_000_000})


def test_config_roundtrip_sides() -> None:
    cfg = ExperimentConfig.from_dict({"experiment_id": "x", "sides": ["BUY"]})
    assert cfg.sides == (TaskSide.BUY,)
    assert cfg.to_dict()["guard_ns"] == 1_400_001


def test_side_helpers() -> None:
    assert Side.BID.opposite() is Side.ASK
    assert TaskSide.SELL.book_side is Side.ASK
    assert int(TaskSide.SELL) == -1
    with pytest.raises(ValueError):
        Side.NONE.opposite()


def test_mid2_exact_and_crossed() -> None:
    snap = BookSnapshot(
        1,
        TimeNs(0),
        TimeNs(0),
        (BookLevel(100_000_000_000, 5, 1),),
        (BookLevel(100_250_000_000, 5, 1),),
        TradingStatus.TRADING,
    )
    assert snap.mid2() == 200_250_000_000
    crossed = BookSnapshot(1, TimeNs(0), TimeNs(0), snap.asks, snap.bids, TradingStatus.TRADING)
    assert crossed.mid2() is None
