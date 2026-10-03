"""Metric tests: T19 (shortfall signs/fees/half-ticks), T28 (offset identity), C_T, markout.

All golden values are hand-derived; expected numbers are written in comments, not recorded from
the implementation's own output (engineering contract rule 6).

Fixed-point convention: price p in quoted units -> p * 1e9 fixed. ``Mid2 = bid + ask`` so a
midpoint m in quoted units is ``2 * m * 1e9`` as Mid2.
"""

from __future__ import annotations

from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from qexec.analysis.metrics import (
    benchmark_offset_ticks,
    deadline_value_ticks,
    implementation_shortfall_ticks,
    markout_ticks,
)

SCALE = 1_000_000_000
DELTA = 250_000_000  # 0.25 tick in fixed-point
M = 50  # multiplier


def px(quoted: float) -> int:
    """Quoted price -> fixed-point integer (exact for the values used here)."""
    return round(quoted * SCALE)


def mid2(quoted_mid: float) -> int:
    """Quoted midpoint -> Mid2 (twice the midpoint) in fixed-point."""
    return round(2 * quoted_mid * SCALE)


# --------------------------------------------------------------------------------------
# T19: product 7.1 example and sign/fee/half-tick behaviour.
# --------------------------------------------------------------------------------------


def test_t19_product_example_buy() -> None:
    # delta=0.25, M=50, m0=100, one-contract buy at 100.25, fee 2.50.
    # gross = s*(p-m0)/(Q*delta) = (100.25-100)/0.25 = 1.00
    # fee term = f/(Q*delta*M) = 2.50/(0.25*50) = 2.50/12.5 = 0.20
    # net = 1.20
    gross, net = implementation_shortfall_ticks(
        side=1,
        fills=[(1, px(100.25))],
        m0_mid2=mid2(100.0),
        tick_size_fixed=DELTA,
        multiplier=M,
        fees_fixed=px(2.50),  # 2.50 currency in 1e-9 units
    )
    assert gross == 1.00
    assert net == 1.20


def test_t19_product_example_sell() -> None:
    # Selling at 99.75 with the same parameters also gives 1.20 ticks net.
    # gross = -1*(99.75-100)/0.25 = -1*(-1.0) = 1.00
    # fee term = 0.20 ; net = 1.20
    gross, net = implementation_shortfall_ticks(
        side=-1,
        fills=[(1, px(99.75))],
        m0_mid2=mid2(100.0),
        tick_size_fixed=DELTA,
        multiplier=M,
        fees_fixed=px(2.50),
    )
    assert gross == 1.00
    assert net == 1.20


def test_t19_positive_shortfall_is_worse() -> None:
    # Buy above mid -> positive (worse). Buy below mid -> negative (better).
    gross_above, _ = implementation_shortfall_ticks(1, [(1, px(100.25))], mid2(100.0), DELTA, M, 0)
    gross_below, _ = implementation_shortfall_ticks(1, [(1, px(99.75))], mid2(100.0), DELTA, M, 0)
    assert gross_above == 1.00
    assert gross_below == -1.00


def test_t19_half_tick_midpoint() -> None:
    # Half-tick midpoint: bid=100.00, ask=100.125 -> mid = 100.0625, Mid2 exact.
    # Buy at 100.25: gross = (100.25 - 100.0625)/0.25 = 0.1875/0.25 = 0.75
    bid, ask = px(100.00), px(100.125)
    m0 = bid + ask  # Mid2 exact, no rounding
    gross, net = implementation_shortfall_ticks(
        side=1,
        fills=[(1, px(100.25))],
        m0_mid2=m0,
        tick_size_fixed=DELTA,
        multiplier=M,
        fees_fixed=0,
    )
    assert gross == 0.75
    assert net == 0.75


def test_t19_fee_sign_rebate() -> None:
    # Negative fee (rebate) reduces net shortfall below gross.
    gross, net = implementation_shortfall_ticks(
        side=1,
        fills=[(1, px(100.25))],
        m0_mid2=mid2(100.0),
        tick_size_fixed=DELTA,
        multiplier=M,
        fees_fixed=-px(2.50),  # rebate
    )
    assert gross == 1.00
    assert net == 0.80  # 1.00 - 0.20


# --------------------------------------------------------------------------------------
# T28: offset identity IS(m0) = IS(z0) + s(z0 - m0)/delta for completed tasks.
# Property test over random integers (engineering contract: hypothesis).
# --------------------------------------------------------------------------------------


@given(
    s=st.sampled_from([1, -1]),
    p_ticks=st.integers(min_value=-200, max_value=200),
    m0_half_ticks=st.integers(min_value=-200, max_value=200),
    z0_half_ticks=st.integers(min_value=-200, max_value=200),
    fee_units=st.integers(min_value=-10_000_000_000, max_value=10_000_000_000),
)
def test_t28_offset_identity(
    s: int, p_ticks: int, m0_half_ticks: int, z0_half_ticks: int, fee_units: int
) -> None:
    # Build prices/midpoints on a fine grid. Price on tick grid; midpoints on half-tick grid so
    # Mid2 remains an exact integer. base price 100.00.
    base = px(100.00)
    p_fixed = base + p_ticks * DELTA
    # Mid2 = 2*midpoint. A half-tick step in the midpoint is DELTA in Mid2 units.
    m0_mid2 = 2 * base + m0_half_ticks * DELTA
    z0_mid2 = 2 * base + z0_half_ticks * DELTA

    _, is_m0 = implementation_shortfall_ticks(s, [(1, p_fixed)], m0_mid2, DELTA, M, fee_units)
    _, is_z0 = implementation_shortfall_ticks(s, [(1, p_fixed)], z0_mid2, DELTA, M, fee_units)
    offset = benchmark_offset_ticks(s, z0_mid2, m0_mid2, DELTA)

    # IS(m0) == IS(z0) + s(z0 - m0)/delta. Reconstruct every term as an exact Fraction and
    # compare the identity exactly; the implementation converts to float only at the boundary, so
    # verify the exact rational identity holds and that the float results satisfy it too.
    expected_offset = Fraction(s * (z0_mid2 - m0_mid2), 2 * DELTA)
    assert offset == float(expected_offset)
    # Exact rational IS values (fee term uses delta*M denominator).
    is_m0_exact = Fraction(s * (1 * (2 * p_fixed - m0_mid2)), 2 * DELTA) + Fraction(
        fee_units, DELTA * M
    )
    is_z0_exact = Fraction(s * (1 * (2 * p_fixed - z0_mid2)), 2 * DELTA) + Fraction(
        fee_units, DELTA * M
    )
    assert is_m0_exact == is_z0_exact + expected_offset
    assert is_m0 == float(is_m0_exact)
    assert is_z0 == float(is_z0_exact)


# --------------------------------------------------------------------------------------
# C_T (deadline-horizon value, product section 8).
# --------------------------------------------------------------------------------------


def test_c_t_full_fill_no_residual() -> None:
    # One fill at z0 price exactly -> fill term 0, no residual, no fee -> 0.
    # z0 = 100.00; buy fill at 100.00: s*q*(p - z0)/delta = 0; residual = 0.
    val = deadline_value_ticks(
        side=1,
        fills_by_deadline=[(1, px(100.00))],
        fees_fixed=0,
        z0_mid2=mid2(100.00),
        mT_mid2=mid2(100.50),  # irrelevant since residual = 0
        tick_size_fixed=DELTA,
        multiplier=M,
    )
    assert val == 0.0


def test_c_t_no_fill_residual_valuation_buy() -> None:
    # No fills -> residual = 1. Buy, z0=100.00, m_T=100.50 (stale allowed).
    # residual term = s*r_T*(m_T - z0)/delta = 1*1*(100.50-100.00)/0.25 = 0.5/0.25 = 2.0
    val = deadline_value_ticks(
        side=1,
        fills_by_deadline=[],
        fees_fixed=0,
        z0_mid2=mid2(100.00),
        mT_mid2=mid2(100.50),
        tick_size_fixed=DELTA,
        multiplier=M,
    )
    assert val == 2.0


def test_c_t_no_fill_residual_valuation_sell() -> None:
    # Sell side: s=-1. z0=100.00, m_T=100.50 -> residual = -1*(0.5)/0.25 = -2.0 (favourable).
    val = deadline_value_ticks(
        side=-1,
        fills_by_deadline=[],
        fees_fixed=0,
        z0_mid2=mid2(100.00),
        mT_mid2=mid2(100.50),
        tick_size_fixed=DELTA,
        multiplier=M,
    )
    assert val == -2.0


def test_c_t_partial_is_one_contract_so_fill_or_residual() -> None:
    # One-contract task: a single fill means residual 0; otherwise residual 1. Verify the fill
    # with fee: buy fill at 100.25 with fee 2.50, z0=100.00.
    # fill term = (100.25-100.00)/0.25 = 1.0 ; fee term = 2.50/(0.25*50)=0.20 ; residual 0.
    val = deadline_value_ticks(
        side=1,
        fills_by_deadline=[(1, px(100.25))],
        fees_fixed=px(2.50),
        z0_mid2=mid2(100.00),
        mT_mid2=mid2(100.00),
        tick_size_fixed=DELTA,
        multiplier=M,
    )
    assert val == 1.20


def test_c_t_stale_mt_half_tick() -> None:
    # Stale m_T on a half-tick midpoint. No fill -> residual valuation only.
    # m_T mid = 100.125, z0 = 100.00. residual term = (100.125-100.00)/0.25 = 0.125/0.25 = 0.5
    mt = px(100.00) + px(100.25)  # Mid2 for midpoint 100.125
    val = deadline_value_ticks(
        side=1,
        fills_by_deadline=[],
        fees_fixed=0,
        z0_mid2=mid2(100.00),
        mT_mid2=mt,
        tick_size_fixed=DELTA,
        multiplier=M,
    )
    assert val == 0.5


def test_c_t_residual_carries_no_fee() -> None:
    # Fee argument applies only to actual fills; with no fills the fee term is still computed on
    # fees_fixed (which should be 0 when there are no fills), and the residual has no fee.
    # Here pass fees_fixed=0 (no fills) and confirm only residual valuation remains.
    val = deadline_value_ticks(
        side=1,
        fills_by_deadline=[],
        fees_fixed=0,
        z0_mid2=mid2(100.00),
        mT_mid2=mid2(100.25),
        tick_size_fixed=DELTA,
        multiplier=M,
    )
    # residual = (100.25 - 100.00)/0.25 = 1.0, no fee added.
    assert val == 1.0


# --------------------------------------------------------------------------------------
# Markout sign (product 7.2). Positive is favourable.
# --------------------------------------------------------------------------------------


def test_markout_sign_buy_favourable() -> None:
    # Buy fill at 100.00, future mid 100.25 -> markout = (100.25-100.00)/0.25 = 1.0 (favourable).
    mo = markout_ticks(
        side=1, fill_price_fixed=px(100.00), future_mid2=mid2(100.25), tick_size_fixed=DELTA
    )
    assert mo == 1.0


def test_markout_sign_buy_adverse() -> None:
    # Buy fill at 100.00, future mid 99.75 -> markout = (99.75-100.00)/0.25 = -1.0 (adverse).
    mo = markout_ticks(1, px(100.00), mid2(99.75), DELTA)
    assert mo == -1.0


def test_markout_sign_sell_favourable() -> None:
    # Sell fill at 100.00, future mid 99.75 -> s=-1: -1*(99.75-100.00)/0.25 = 1.0 (favourable).
    mo = markout_ticks(-1, px(100.00), mid2(99.75), DELTA)
    assert mo == 1.0


def test_markout_half_tick() -> None:
    # Future mid at 100.125 (half tick), buy fill at 100.00: (100.125-100.00)/0.25 = 0.5
    fut = px(100.00) + px(100.25)  # Mid2 for 100.125
    mo = markout_ticks(1, px(100.00), fut, DELTA)
    assert mo == 0.5
