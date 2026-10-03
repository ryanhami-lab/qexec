"""QExec policy registry (product section 6.1, architecture section 9).

Exposes the policy :class:`~qexec.policies.base.Policy` protocol, the shared
:func:`~qexec.policies.base.checkpoint_eligibility` rule, and the baseline policies B0 and B1
plus the B1-prefix label-probe policy used by the engine's fork path.
"""

from __future__ import annotations

from qexec.policies.base import ArrivalAction, Policy, checkpoint_eligibility
from qexec.policies.baselines import (
    B1_PROBE_POLICY_ID,
    B0Policy,
    B1Policy,
    B1ProbePolicy,
)

__all__ = [
    "B1_PROBE_POLICY_ID",
    "ArrivalAction",
    "B0Policy",
    "B1Policy",
    "B1ProbePolicy",
    "Policy",
    "checkpoint_eligibility",
]
