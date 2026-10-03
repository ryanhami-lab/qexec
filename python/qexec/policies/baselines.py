"""Baseline policies B0 and B1, plus the B1-prefix label-probe policy (product section 6.1).

* **B0** -- immediate aggressive submission: ``on_arrival`` returns ``"SWITCH_TO_TAKER"`` so the
  engine enters the terminal controller at arrival. B0 never reaches a discretionary checkpoint
  (``on_checkpoint`` is never consulted because the world is already in the controller), but the
  method is implemented for protocol completeness and returns ``(HOLD, NOT_APPLICABLE)``.
* **B1** -- join at arrival and hold to the common terminal cutoff: ``on_arrival`` returns
  ``"JOIN_BEST"``; ``on_checkpoint`` always returns ``(HOLD, NOT_APPLICABLE)`` (B1 has no
  discretionary checkpoint decision). The engine still evaluates
  :func:`~qexec.policies.base.checkpoint_eligibility` first and only calls ``on_checkpoint`` when
  the verdict is ``MODEL_CHOICE``.
* **B1_PROBE** -- the B1-prefix policy used for label-probe forking (engine spec section 8). It
  behaves exactly like B1 (same arrival join, same ``policy_id`` prefix semantics), but the
  engine recognizes its ``policy_id`` ``"B1_PROBE"`` and, when its checkpoint is eligible, clones
  the world into HOLD and SWITCH branches instead of continuing B1's hold.

All three share B1's arrival join except B0; their trajectories are identical to B1 until the
checkpoint, exactly as required (product 6.1).
"""

from __future__ import annotations

from qexec.core.tasks import CheckpointChoice, DecisionReason
from qexec.core.views import DecisionView
from qexec.policies.base import ArrivalAction

B1_PROBE_POLICY_ID = "B1_PROBE"


class B0Policy:
    """Immediate aggressive completion benchmark (enters the controller at arrival)."""

    policy_id: str = "B0"

    def on_arrival(self, view: DecisionView) -> ArrivalAction:
        del view
        return "SWITCH_TO_TAKER"

    def on_checkpoint(self, view: DecisionView) -> tuple[CheckpointChoice, DecisionReason]:
        # B0 is always already in the controller by the checkpoint; never consulted.
        del view
        return (CheckpointChoice.HOLD, DecisionReason.NOT_APPLICABLE)


class B1Policy:
    """Join at arrival, hold to the common terminal cutoff (transparent waiting benchmark)."""

    policy_id: str = "B1"

    def on_arrival(self, view: DecisionView) -> ArrivalAction:
        del view
        return "JOIN_BEST"

    def on_checkpoint(self, view: DecisionView) -> tuple[CheckpointChoice, DecisionReason]:
        del view
        return (CheckpointChoice.HOLD, DecisionReason.NOT_APPLICABLE)


class B1ProbePolicy(B1Policy):
    """B1-prefix policy whose eligible checkpoint triggers HOLD/SWITCH label-probe forking.

    Identical to :class:`B1Policy` up to the checkpoint; the engine detects the ``B1_PROBE``
    ``policy_id`` and performs the clone/fork (engine spec section 8) rather than calling
    ``on_checkpoint``. The inherited ``on_checkpoint`` therefore only runs if the checkpoint is
    *ineligible*, in which case the engine already holds with the ineligibility reason.
    """

    policy_id: str = B1_PROBE_POLICY_ID
