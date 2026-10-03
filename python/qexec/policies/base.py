"""Policy protocol and the shared checkpoint-eligibility rule (product section 6.1).

A :class:`Policy` makes exactly two kinds of discretionary decision, both from a client-plane
:class:`~qexec.core.views.DecisionView` that never contains the evaluator benchmark ``m0`` or
any simulator-truth state:

* :meth:`Policy.on_arrival` -- the initial action at task arrival ``t0``. The core admissible
  arrival actions are ``"JOIN_BEST"`` (submit a one-contract same-side limit at the client best)
  and ``"SWITCH_TO_TAKER"`` (enter the terminal controller immediately, i.e. B0).
* :meth:`Policy.on_checkpoint` -- the single discretionary decision at ``t0 + H/2``, returning a
  :class:`~qexec.core.tasks.CheckpointChoice` and the :class:`~qexec.core.tasks.DecisionReason`.

The engine calls :func:`checkpoint_eligibility` *before* ``on_checkpoint`` and only consults the
policy when the result is :data:`~qexec.core.tasks.DecisionReason.MODEL_CHOICE`; otherwise it
holds with the returned ineligibility reason (product 6.1 step 1). This keeps the eligibility
rule identical across every policy (architecture 9.1).
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from qexec.core.tasks import CheckpointChoice, DecisionReason
from qexec.core.views import ClientOrderState, DecisionView

ArrivalAction = Literal["JOIN_BEST", "SWITCH_TO_TAKER", "WAIT"]


@runtime_checkable
class Policy(Protocol):
    """A replay policy. Implementations must be deterministic and read only ``view``."""

    policy_id: str

    def on_arrival(self, view: DecisionView) -> ArrivalAction:
        """Return the initial action at task arrival."""
        ...

    def on_checkpoint(self, view: DecisionView) -> tuple[CheckpointChoice, DecisionReason]:
        """Return the discretionary checkpoint action and its reason.

        Only called by the engine when :func:`checkpoint_eligibility` is ``MODEL_CHOICE``.
        """
        ...


def checkpoint_eligibility(view: DecisionView) -> DecisionReason:
    """Return the shared checkpoint eligibility verdict for ``view`` (product 6.1 step 1).

    A discretionary choice requires a client-reported **working, unfilled** order with no
    pending command and no entry into the controller. The verdict codes (architecture 9.1):

    * ``REPORTED_COMPLETE``          -- the client has received the fill (``FILLED``); no action.
    * ``INELIGIBLE_PENDING_COMMAND`` -- a command is in flight (``pending_command`` or
      ``IN_FLIGHT`` / ``CANCEL_PENDING``); defer to the safe continuation.
    * ``INELIGIBLE_REJECTED``        -- the initial submission was rejected (``REJECTED``).
    * ``INELIGIBLE_NOT_WORKING``     -- not ``WORKING`` for any other reason, or already in the
      controller (e.g. ``CANCELLED`` / ``AGGRESSIVE_UNFILLED`` / ``TECHNICAL`` / ``NONE``).
    * ``INELIGIBLE_PARTIAL``         -- a nonzero reported executed quantity on a one-contract
      task (``reported_executed not in (0,)``), which for ``Q = 1`` means it is already
      reported complete via a partial path; defer rather than act.
    * ``MODEL_CHOICE``               -- eligible; the engine will call ``on_checkpoint``.

    The order of the checks is significant: completion and pending state are tested before the
    working check so that an unreported exchange fill (which does not change the client state)
    can never make an otherwise-pending task look eligible (product 6.1: "An unreported exchange
    fill must not affect this decision").
    """
    if view.order_state is ClientOrderState.FILLED:
        return DecisionReason.REPORTED_COMPLETE
    if view.pending_command or view.order_state in (
        ClientOrderState.IN_FLIGHT,
        ClientOrderState.CANCEL_PENDING,
    ):
        return DecisionReason.INELIGIBLE_PENDING_COMMAND
    if view.order_state is ClientOrderState.REJECTED:
        return DecisionReason.INELIGIBLE_REJECTED
    if view.in_controller or view.order_state is not ClientOrderState.WORKING:
        return DecisionReason.INELIGIBLE_NOT_WORKING
    if view.reported_executed not in (0,):
        return DecisionReason.INELIGIBLE_PARTIAL
    return DecisionReason.MODEL_CHOICE
