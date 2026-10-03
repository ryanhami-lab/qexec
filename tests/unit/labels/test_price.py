"""Tests for :class:`qexec.labels.price.MidSeries` and :func:`price_direction_label` (T43).

Covers hand-built mid-series lookups, direction-label signs including the unchanged->0 case
(T43), None propagation when a mid is missing, and a MidSeries replayed from a small synthetic
session written with :func:`qexec.synthetic.write_synthetic_session`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qexec.labels.price import MidSeries, price_direction_label
from qexec.synthetic import SyntheticParams, write_synthetic_session

TICK = 250_000_000
S = 1_000_000_000


# --------------------------------------------------------------------------- mid_at / bisect


def test_mid_at_returns_last_at_or_before() -> None:
    # mid2 values at times 0, 10, 20 ns.
    series = MidSeries([0, 10, 20], [100, 102, 104])
    assert series.mid_at(-1) is None  # before the first sample
    assert series.mid_at(0) == 100
    assert series.mid_at(5) == 100  # last at or before 5 is the t=0 sample
    assert series.mid_at(10) == 102
    assert series.mid_at(19) == 102
    assert series.mid_at(20) == 104
    assert series.mid_at(99) == 104  # last known


def test_mid_at_returns_none_when_sample_mid_is_none() -> None:
    series = MidSeries([0, 10, 20], [100, None, 104])
    assert series.mid_at(10) is None  # the valid-at-or-before sample is itself None (halted)
    assert series.mid_at(15) is None
    assert series.mid_at(20) == 104


def test_midseries_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="equal length"):
        MidSeries([0, 1], [100])


def test_midseries_rejects_nonmonotone_times() -> None:
    with pytest.raises(ValueError, match="nondecreasing"):
        MidSeries([0, 5, 3], [100, 101, 102])


# --------------------------------------------------------------------------- direction labels


def test_price_direction_up_down() -> None:
    series = MidSeries([0, 100], [100, 110])
    assert price_direction_label(series, 0, 100, TICK) == 1  # 110 > 100
    series_down = MidSeries([0, 100], [110, 100])
    assert price_direction_label(series_down, 0, 100, TICK) == -1


def test_t43_unchanged_mid_is_class_zero_not_none() -> None:
    # Mid is identical at t* and t*+tau -> class 0 (retained), never None.
    series = MidSeries([0, 100, 200], [100, 100, 100])
    label = price_direction_label(series, 0, 100, TICK)
    assert label == 0
    assert label is not None


def test_label_none_when_either_mid_missing() -> None:
    series = MidSeries([0, 100], [None, 110])
    assert price_direction_label(series, 0, 100, TICK) is None  # m(t*) missing
    series2 = MidSeries([0, 100], [100, None])
    assert price_direction_label(series2, 0, 100, TICK) is None  # m(t*+tau) missing


def test_label_none_before_series_start() -> None:
    series = MidSeries([100, 200], [100, 110])
    assert price_direction_label(series, 0, 50, TICK) is None  # both lookups before first sample


# --------------------------------------------------------------------------- synthetic session


def test_midseries_from_small_synthetic_session(tmp_path: Path) -> None:
    params = SyntheticParams(
        seed=7,
        session_id="SYN-LABELS",
        start_ns=1_000_000_000_000,
        duration_s=30,
    )
    session_dir = write_synthetic_session(params, tmp_path)
    series = MidSeries.from_session(session_dir)

    # The session produced committed LIVE batches, so the series is non-empty.
    assert len(series) > 0

    # Times are nondecreasing (the constructor would have rejected otherwise, but assert the
    # replay actually populated a usable range spanning the ~30 s session).
    start = params.start_ns
    end_lookup = series.mid_at(start + 30 * S)
    # A lookup near the session end should resolve to the last committed mid (int or None).
    assert end_lookup is None or isinstance(end_lookup, int)

    # At least one valid (non-None) committed mid exists while TRADING.
    valid_found = any(series.mid_at(start + i * S) is not None for i in range(31))
    assert valid_found

    # A direction label over a sub-horizon is either a valid class or None (never raises).
    label = price_direction_label(series, start + 5 * S, 1 * S, TICK)
    assert label in (-1, 0, 1, None)
