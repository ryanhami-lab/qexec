"""Pure modeling utilities for QExec (WP-MODELS).

These operate on numpy arrays and polars frames only. They must not depend on the
simulation engine (``qexec.sim``) or any policy runtime. The decision rules, price and
action models, feature schema, support rule, artifact format, and forward-chained
out-of-fold scoring live here and are consumed by the research work package.

Research semantics are defined in the planning documents (product sections 6.1, 6.3, 8;
architecture sections 9.1, 10.3). This package implements those definitions; it never
restates or overrides them.
"""

from __future__ import annotations

from qexec.models.action import ActionModels, ActionPrediction
from qexec.models.artifacts import ModelArtifact
from qexec.models.decision import (
    THETA_MENU,
    ThetaSelection,
    ThetaTrial,
    risk_allowance_choice,
    select_theta,
)
from qexec.models.price import PRICE_CLASSES, PriceModel
from qexec.models.schema import FeatureSchema, SchemaMismatchError, SupportRule
from qexec.models.stacking import ForwardChainedResult, forward_chained_oof_scores

__all__ = [
    "PRICE_CLASSES",
    "THETA_MENU",
    "ActionModels",
    "ActionPrediction",
    "FeatureSchema",
    "ForwardChainedResult",
    "ModelArtifact",
    "PriceModel",
    "SchemaMismatchError",
    "SupportRule",
    "ThetaSelection",
    "ThetaTrial",
    "forward_chained_oof_scores",
    "risk_allowance_choice",
    "select_theta",
]
