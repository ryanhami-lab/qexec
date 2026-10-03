"""One counterfactual world: a (task, policy, side) replay with its own exposure (engine spec).

A :class:`World` bundles everything that is *per world* -- the exchange overlay, the client OMS,
the terminal controller, the observable queue-cohort proxy, the policy, and all per-world
bookkeeping (own limit, order-creation time, decision log, trace, outcome scaffolding). The
shared, read-only historical and client books plus the per-scenario market-feature state live on
the engine and are **not** copied by :meth:`clone`.

A world is *active* from its arrival until every command and report is resolved after the
deadline (reconciliation drain). The engine delivers callbacks only to active worlds, so inactive
worlds cost nothing (engine spec section 1).

:meth:`clone` performs the full-state checkpoint/restore used for label-probe forking (engine
spec section 8, T41): deep copies of the overlay, OMS, controller, and proxy; shared books are
*not* copied. Pending scheduler events live on the engine, not the world, so :meth:`clone` cannot
carry them; after cloning, the engine re-schedules each branch's own per-world timers (cutoff and
deadline) keyed to the branch ``world_id`` so the branches evolve independently through the rest
of the single pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from qexec.core.tasks import Task
from qexec.core.types import TaskSide
from qexec.features.queue_proxy import QueueCohortProxy
from qexec.policies.base import Policy
from qexec.sim.controller import TerminalController
from qexec.sim.oms import ClientOMS
from qexec.sim.overlay import ExchangeOverlay


class WorldPhase(Enum):
    """Lifecycle phase of a world (drives activity and callback routing)."""

    PENDING = "PENDING"
    """Created but not yet arrived."""
    LIVE = "LIVE"
    """Arrived and running (receives callbacks)."""
    DRAINING = "DRAINING"
    """Past deadline; outcome frozen, draining pending commands/reports for reconciliation."""
    DONE = "DONE"
    """Fully resolved; receives no further callbacks (inactive)."""


@dataclass(slots=True)
class World:
    """A single (task, policy) counterfactual world."""

    world_id: str
    task: Task
    side: TaskSide
    policy: Policy
    overlay: ExchangeOverlay
    oms: ClientOMS
    controller: TerminalController

    checkpoint_ns: int
    cutoff_ns: int
    deadline_ns: int

    phase: WorldPhase = WorldPhase.PENDING
    proxy: QueueCohortProxy | None = None
    own_limit_price_fixed: int | None = None
    order_created_ns: int | None = None
    passive_command_id: str | None = None
    checkpoint_done: bool = False
    checkpoint_eligible: bool = False
    """R8: persisted at the checkpoint for every world, independent of feature recording. True
    iff ``checkpoint_eligibility`` returned ``MODEL_CHOICE`` at this world's checkpoint."""
    cutoff_entered: bool = False
    command_counter: int = 0
    z0_mid2: int | None = None  # client Mid2 observed at arrival (fixed benchmark for C_T).

    # Decision / trace logs (lists of plain dicts for DataFrame assembly and trace()).
    decisions: list[dict[str, Any]] = field(default_factory=list)
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    # Checkpoint feature snapshot (set when a B1-prefix world reaches an eligible checkpoint,
    # or any checkpoint for B1 recording). Includes eligibility.
    checkpoint_features: dict[str, Any] | None = None

    # Probe/branch metadata (engine spec section 8).
    is_probe: bool = False
    branch_action: str | None = None  # "HOLD" / "SWITCH" for forked branches.
    probe_parent_id: str | None = None
    fork_feats: dict[str, float] | None = None  # checkpoint feature dict frozen at the fork.
    fork_time_ns: int | None = None

    @property
    def active(self) -> bool:
        return self.phase in (WorldPhase.LIVE, WorldPhase.DRAINING)

    def next_command_id(self, kind: str) -> str:
        cid = f"{self.world_id}:cmd:{self.command_counter}:{kind}"
        self.command_counter += 1
        return cid

    def log_trace(self, event: dict[str, Any]) -> None:
        self.trace_events.append(event)

    def clone(self, new_world_id: str) -> World:
        """Deep, independent copy for HOLD/SWITCH branch forking (engine spec section 8, T41).

        Copies the overlay, OMS, controller, and proxy; shared books are not copied. The clone
        shares the frozen ``task``/``policy`` objects (immutable) but has independent mutable
        logs and bookkeeping. Pending scheduler events are not copied here (they live on the
        engine); the engine re-schedules each branch's cutoff/deadline timers after cloning.
        """
        clone = World(
            world_id=new_world_id,
            task=self.task,
            side=self.side,
            policy=self.policy,
            overlay=self.overlay.copy(),
            oms=self.oms.copy(),
            controller=self.controller.copy(),
            checkpoint_ns=self.checkpoint_ns,
            cutoff_ns=self.cutoff_ns,
            deadline_ns=self.deadline_ns,
            phase=self.phase,
            proxy=self.proxy.copy() if self.proxy is not None else None,
            own_limit_price_fixed=self.own_limit_price_fixed,
            order_created_ns=self.order_created_ns,
            passive_command_id=self.passive_command_id,
            checkpoint_done=self.checkpoint_done,
            checkpoint_eligible=self.checkpoint_eligible,
            cutoff_entered=self.cutoff_entered,
            command_counter=self.command_counter,
            z0_mid2=self.z0_mid2,
            decisions=[dict(d) for d in self.decisions],
            trace_events=[dict(e) for e in self.trace_events],
            checkpoint_features=dict(self.checkpoint_features)
            if self.checkpoint_features is not None
            else None,
            is_probe=self.is_probe,
            branch_action=self.branch_action,
            probe_parent_id=self.probe_parent_id,
            fork_feats=dict(self.fork_feats) if self.fork_feats is not None else None,
            fork_time_ns=self.fork_time_ns,
        )
        return clone
