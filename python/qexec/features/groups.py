"""Feature group constants and the policy feature allowlist (product section 6.2).

The research design freezes feature groups so the primary comparison (B3 versus the matched
``B3_NO_QUEUE``) isolates the incremental value of own-order queue estimates while every other
feature group stays identical (engineering contract WP-FEATURES; product 6.2).

Groups
------
* :data:`MARKET_FEATURES` -- shared delivered market information (spread, top depth, imbalance,
  recent delivered flow, volatility, quote age). Computed by
  :class:`qexec.features.market.MarketFeatureState`.
* :data:`MECHANICS_FEATURES` -- own-order mechanics derived from the client decision view
  (side, own limit distance from the client best, order age, time remaining). Computed by
  :func:`mechanics_features`.
* :data:`PRICE_SIGNAL_FEATURES` -- outputs of the shared price-direction model.
* :data:`QUEUE_FEATURES` -- the observable cohort proxy (architecture 7.1). This is the only
  removable group in the primary ablation.

Prohibited features
-------------------
:data:`PROHIBITED_FEATURES` lists simulator-truth / oracle / future columns that may never be
exposed to a policy. :func:`assert_allowed` rejects any column that is prohibited by exact name,
by the ``future_`` prefix, or that contains the substrings ``oracle`` or ``true_`` (so e.g.
``true_queue_ahead`` is rejected even if it is not spelled exactly). It also rejects any column
not in the requested policy's allowlist.
"""

from __future__ import annotations

from collections.abc import Sequence

from qexec.core.types import TaskSide
from qexec.core.views import DecisionView

MARKET_FEATURES: tuple[str, ...] = (
    "spread_ticks",
    "bid_qty_1",
    "ask_qty_1",
    "imbalance_1",
    "depth_bid_3",
    "depth_ask_3",
    "imbalance_3",
    "quote_age_ms",
    "trade_flow_signed_1s",
    "trade_count_1s",
    "cancel_qty_bid_1s",
    "cancel_qty_ask_1s",
    "add_qty_bid_1s",
    "add_qty_ask_1s",
    "mid_vol_5s",
    "mid_change_1s_ticks",
)

MECHANICS_FEATURES: tuple[str, ...] = (
    "side",
    "limit_offset_ticks",
    "order_age_ms",
    "time_remaining_ms",
)

PRICE_SIGNAL_FEATURES: tuple[str, ...] = ("p_down", "p_unch", "p_up", "u_signal")

QUEUE_FEATURES: tuple[str, ...] = (
    "q_ahead_est",
    "q_ahead_upper",
    "q_insertion_uncertain",
    "q_depleted",
    "q_depleted_frac",
)

# Exact-name prohibitions. ``future_*`` is a prefix prohibition handled in ``assert_allowed``.
PROHIBITED_FEATURES: tuple[str, ...] = (
    "true_queue_ahead",
    "unreported_executed",
    "future_*",
    "m0",
)

# Substrings that mark a column as oracle/simulator-truth regardless of its exact spelling.
_PROHIBITED_SUBSTRINGS: tuple[str, ...] = ("oracle", "true_")
_FUTURE_PREFIX = "future_"

_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "B3": MARKET_FEATURES + MECHANICS_FEATURES + PRICE_SIGNAL_FEATURES + QUEUE_FEATURES,
    "B3_NO_QUEUE": MARKET_FEATURES + MECHANICS_FEATURES + PRICE_SIGNAL_FEATURES,
    "PRICE": MARKET_FEATURES,
    "B2": PRICE_SIGNAL_FEATURES + MECHANICS_FEATURES,
}


def allowlist(policy_id: str) -> tuple[str, ...]:
    """Return the ordered tuple of columns a policy variant is allowed to consume.

    * ``B3``          -> market + mechanics + price-signal + queue features.
    * ``B3_NO_QUEUE`` -> the same minus :data:`QUEUE_FEATURES` (the primary ablation).
    * ``PRICE``       -> :data:`MARKET_FEATURES` only (the shared price model's inputs).
    * ``B2``          -> price-signal + mechanics features.

    An unknown ``policy_id`` raises :class:`ValueError`.
    """
    try:
        return _ALLOWLISTS[policy_id]
    except KeyError:
        raise ValueError(f"unknown policy_id {policy_id!r}") from None


def _is_prohibited(column: str) -> bool:
    """A column is prohibited if it is an exact prohibited name, has the ``future_`` prefix,
    or contains an oracle/simulator-truth substring."""
    if column in PROHIBITED_FEATURES:
        return True
    if column.startswith(_FUTURE_PREFIX):
        return True
    return any(sub in column for sub in _PROHIBITED_SUBSTRINGS)


def assert_allowed(columns: Sequence[str], policy_id: str) -> None:
    """Raise :class:`ValueError` if any column may not be fed to ``policy_id``.

    A column is rejected when it is prohibited (exact name, ``future_`` prefix, or an
    ``oracle``/``true_`` substring) or when it is not in the policy's allowlist. The prohibited
    check runs first so an oracle column is always reported as prohibited, never as merely
    out-of-allowlist (T25, T45).
    """
    allowed = set(allowlist(policy_id))
    for column in columns:
        if _is_prohibited(column):
            raise ValueError(f"prohibited feature column {column!r} for policy {policy_id!r}")
        if column not in allowed:
            raise ValueError(f"feature column {column!r} not in allowlist for policy {policy_id!r}")


def mechanics_features(view: DecisionView, tick_size_fixed: int) -> dict[str, float]:
    """Own-order mechanics features derived purely from the client :class:`DecisionView`.

    * ``side`` -- ``+1`` for a buy task, ``-1`` for a sell task.
    * ``limit_offset_ticks`` -- signed distance of the own limit from the same-side client
      best, in ticks, where **positive means less aggressive** (deeper in the book / further
      from the touch). For a buy the own order rests on the bid, so a limit below the best bid
      is less aggressive: ``(best_bid - own_limit) / tick``. For a sell it rests on the ask, so
      a limit above the best ask is less aggressive: ``(own_limit - best_ask) / tick``. ``0.0``
      if the own limit or the relevant client best is unknown.
    * ``order_age_ms`` -- milliseconds since the passive command was created (``0.0`` if none).
    * ``time_remaining_ms`` -- milliseconds remaining until the deadline (clamped at ``0``).
    """
    side_val = 1.0 if view.side is TaskSide.BUY else -1.0

    limit_offset_ticks = 0.0
    own_limit = view.own_limit_price_fixed
    if own_limit is not None:
        if view.side is TaskSide.BUY and view.best_bid is not None:
            limit_offset_ticks = (view.best_bid.price_fixed - own_limit) / tick_size_fixed
        elif view.side is TaskSide.SELL and view.best_ask is not None:
            limit_offset_ticks = (own_limit - view.best_ask.price_fixed) / tick_size_fixed

    order_age_ms = 0.0 if view.order_age_ns is None else view.order_age_ns / 1_000_000.0
    time_remaining_ms = max(view.time_remaining_ns, 0) / 1_000_000.0

    return {
        "side": side_val,
        "limit_offset_ticks": float(limit_offset_ticks),
        "order_age_ms": float(order_age_ms),
        "time_remaining_ms": float(time_remaining_ms),
    }
