from __future__ import annotations

from qexec.labels.price import MidSeries


def test_last_valid_mid_skips_invalid_samples() -> None:
    # Samples: t=10 valid 200, t=20 invalid (halt), t=30 invalid.
    s = MidSeries([10, 20, 30], [200, None, None])
    assert s.mid_at(35) is None  # state at t is invalid
    assert s.last_valid_mid_at(35) == (200, 10)  # last valid reference, age = 25
    assert s.last_valid_mid_at(15) == (200, 10)
    assert s.last_valid_mid_at(5) is None
    assert MidSeries([10], [None]).last_valid_mid_at(99) is None
