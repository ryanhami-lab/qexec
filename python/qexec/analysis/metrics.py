"""Execution-cost metrics (product sections 7 and 8).

Conventions (binding; see ``docs/engineering_contract.md`` and product section 7):

* Prices are integer fixed-point units of 1e-9 quoted price units (``PriceFixed``).
* A reference midpoint is passed as ``Mid2 = bid + ask`` (twice the midpoint, exact), so
  half-tick midpoints never require rounding.
* Fees ``f_i`` are signed 1e-9 currency units; positive is a charge.
* ``side`` is the sign ``s``: ``+1`` buy, ``-1`` sell.
* ``tick_size_fixed`` is ``delta`` in fixed-point (e.g. 0.25 -> 250_000_000).
* ``multiplier`` is ``M`` (currency per quoted price unit per contract).

All arithmetic is performed exactly with :class:`fractions.Fraction` and converted to
:class:`float` only on return (**positive shortfall means worse execution**, product 7.1).
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction


def _validate_side(side: int) -> int:
    if side not in (1, -1):
        raise ValueError(f"side must be +1 (buy) or -1 (sell), got {side!r}")
    return side


def implementation_shortfall_ticks(  # noqa: PLR0917  (signature fixed by engineering contract WP-METRICS)
    side: int,
    fills: Sequence[tuple[int, int]],
    m0_mid2: int,
    tick_size_fixed: int,
    multiplier: int,
    fees_fixed: int,
    quantity: int = 1,
) -> tuple[float, float]:
    r"""Implementation shortfall in ticks (product section 7.1), gross and fee-adjusted.

    .. math::
        IS_{ticks} = \frac{s \sum_i q_i (p_i - m_0)}{Q \delta}
                     + \frac{\sum_i f_i}{Q \delta M}.

    ``m0`` is supplied as ``Mid2`` (twice the midpoint), so the midpoint in fixed-point is
    ``m0_mid2 / 2``. To keep exact integer arithmetic the price term is formed as
    ``s * sum q_i (2 p_i - m0_mid2) / (2 Q delta)``.

    Parameters
    ----------
    side:
        Sign ``s`` (``+1`` buy, ``-1`` sell).
    fills:
        Sequence of ``(quantity, price_fixed)`` executions.
    m0_mid2:
        Arrival reference midpoint as ``Mid2`` (fixed-point).
    tick_size_fixed:
        Tick size ``delta`` in fixed-point.
    multiplier:
        Currency per quoted price unit per contract (``M``).
    fees_fixed:
        Total signed fee ``sum f_i`` in 1e-9 currency units (positive = charge).
    quantity:
        Target quantity ``Q`` (default 1).

    Returns
    -------
    tuple[float, float]
        ``(gross, net)`` shortfall in ticks. ``net = gross + fee term``.
    """
    s = _validate_side(side)
    if quantity <= 0:
        raise ValueError(f"quantity (Q) must be positive, got {quantity}")
    if tick_size_fixed <= 0:
        raise ValueError(f"tick_size_fixed (delta) must be positive, got {tick_size_fixed}")
    if multiplier <= 0:
        raise ValueError(f"multiplier (M) must be positive, got {multiplier}")

    # Price term: s * sum q_i (2 p_i - m0_mid2) / (2 Q delta), all exact.
    price_numer = 0
    for qty, price_fixed in fills:
        price_numer += qty * (2 * price_fixed - m0_mid2)
    gross = Fraction(s * price_numer, 2 * quantity * tick_size_fixed)

    # Fee term: sum f_i / (Q delta M), all exact. delta and f are both 1e-9 units so the
    # 1e-9 scale cancels exactly between numerator and the delta factor.
    fee_term = Fraction(fees_fixed, quantity * tick_size_fixed * multiplier)
    net = gross + fee_term
    return (float(gross), float(net))


def markout_ticks(
    side: int,
    fill_price_fixed: int,
    future_mid2: int,
    tick_size_fixed: int,
) -> float:
    r"""Post-fill markout in ticks (product section 7.2). Positive is favorable.

    .. math::
        Markout_h = s \frac{m_{t_{fill}+h} - p_{fill}}{\delta}.

    ``future_mid2`` is the future committed-book reference as ``Mid2``; the midpoint is
    ``future_mid2 / 2``. Exact arithmetic uses ``s (future_mid2 - 2 p_fill) / (2 delta)``.
    The caller is responsible for the freshness/status rule and missing-value treatment
    (product 7.2); a missing horizon must not be silently replaced by a later price.
    """
    s = _validate_side(side)
    if tick_size_fixed <= 0:
        raise ValueError(f"tick_size_fixed (delta) must be positive, got {tick_size_fixed}")
    value = Fraction(s * (future_mid2 - 2 * fill_price_fixed), 2 * tick_size_fixed)
    return float(value)


def deadline_value_ticks(  # noqa: PLR0917  (signature fixed by engineering contract WP-METRICS)
    side: int,
    fills_by_deadline: Sequence[tuple[int, int]],
    fees_fixed: int,
    z0_mid2: int,
    mT_mid2: int,  # noqa: N803  (contract name; m_T is a mathematical symbol, not snake_case)
    tick_size_fixed: int,
    multiplier: int,
) -> float:
    r"""Deadline-horizon value ``C_T`` in ticks (product section 8).

    .. math::
        C_{T} = \sum_{i:\,t_i \le T}\left[\frac{s\,q_i (p_i - z_0)}{\delta}
                + \frac{f_i}{\delta M}\right]
                + \frac{s\,r_T (m_T - z_0)}{\delta}.

    where ``z0`` is the fixed initial client midpoint (``Mid2``), ``m_T`` the last valid
    committed reference midpoint at or before ``T`` (``Mid2``; may be stale), ``r_T = 1 - q_T``
    the residual quantity, and target quantity ``Q = 1`` (one-contract tasks). The residual
    mark is a **valuation**, not a fill, and carries **no fee** (product section 8).

    Parameters
    ----------
    side:
        Sign ``s``.
    fills_by_deadline:
        ``(quantity, price_fixed)`` executions with ``t_i <= T`` only.
    fees_fixed:
        Total signed fee for those fills (1e-9 currency units).
    z0_mid2:
        Fixed initial client midpoint as ``Mid2``.
    mT_mid2:
        Last valid committed reference midpoint at or before ``T`` as ``Mid2`` (may be stale).
    tick_size_fixed:
        Tick size ``delta`` in fixed-point.
    multiplier:
        ``M``.
    """
    s = _validate_side(side)
    if tick_size_fixed <= 0:
        raise ValueError(f"tick_size_fixed (delta) must be positive, got {tick_size_fixed}")
    if multiplier <= 0:
        raise ValueError(f"multiplier (M) must be positive, got {multiplier}")

    # Fill term: s * sum q_i (2 p_i - z0_mid2) / (2 delta), exact.
    fill_numer = 0
    q_filled = 0
    for qty, price_fixed in fills_by_deadline:
        fill_numer += qty * (2 * price_fixed - z0_mid2)
        q_filled += qty
    fill_term = Fraction(s * fill_numer, 2 * tick_size_fixed)

    # Fee term on fills only: sum f_i / (delta M), exact (Q = 1).
    fee_term = Fraction(fees_fixed, tick_size_fixed * multiplier)

    # Residual valuation: s * r_T (m_T - z0) / delta with Q = 1, so r_T = 1 - q_filled.
    # Using Mid2: s * r_T (mT_mid2 - z0_mid2) / (2 delta). No fee on the residual.
    residual = 1 - q_filled
    residual_term = Fraction(s * residual * (mT_mid2 - z0_mid2), 2 * tick_size_fixed)

    total = fill_term + fee_term + residual_term
    return float(total)


def benchmark_offset_ticks(
    side: int,
    z0_mid2: int,
    m0_mid2: int,
    tick_size_fixed: int,
) -> float:
    r"""Benchmark offset ``s (z0 - m0) / delta`` in ticks (architecture section 11.2).

    This is the task-specific constant relating the ``z0`` and ``m0`` benchmarks:

    .. math::
        IS(m_0) = IS(z_0) + s\frac{z_0 - m_0}{\delta}.

    Both midpoints are ``Mid2``; the exact form is ``s (z0_mid2 - m0_mid2) / (2 delta)``.
    """
    s = _validate_side(side)
    if tick_size_fixed <= 0:
        raise ValueError(f"tick_size_fixed (delta) must be positive, got {tick_size_fixed}")
    value = Fraction(s * (z0_mid2 - m0_mid2), 2 * tick_size_fixed)
    return float(value)
