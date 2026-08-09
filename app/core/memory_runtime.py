from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from os import environ
from typing import Literal

MemoryMode = Literal["off", "observe", "on"]
MemoryFormationMode = Literal["off", "observe", "enforced"]
MemoryExecutionPlane = Literal["live", "decision_shadow", "state_rehearsal"]

MEMORY_RUNTIME_POLICY_VERSION = "memory-runtime-policy-v1"
MEMORY_RUNTIME_CONFIG_SOURCE = "MEMORY_MODE"
ROUTE_MEMORY_SCOPES = ("user_preference", "stable_fact")
RETIRED_MEMORY_BEHAVIOR_VARIABLES = frozenset(
    {
        "MEMORY_ENABLED",
        "MEMORY_RECALL_ENABLED",
        "MEMORY_FORMATION_MODE",
        "MEMORY_EXECUTION_MODE",
        "MEMORY_TURN_OUTBOX_CONSUMER_ENABLED",
        "MEMORY_FORMATION_WORKER_ENABLED",
        "MEMORY_FORMATION_SWEEPER_ENABLED",
        "MEMORY_INDEX_WORKER_ENABLED",
        "MEMORY_TTL_SWEEPER_ENABLED",
        "MEMORY_CONSOLIDATION_ENABLED",
        "CONTEXT_ROUTE_MEMORY_ENABLED",
        "CONTEXT_ROUTE_MEMORY_SCOPES",
    }
)


@dataclass(frozen=True, slots=True)
class MemoryRuntimePolicy:
    mode: MemoryMode
    execution_plane: MemoryExecutionPlane
    version: str
    config_source: str
    recall_enabled: bool
    formation_mode: MemoryFormationMode
    turn_outbox_consumer_enabled: bool
    formation_worker_enabled: bool
    formation_sweeper_enabled: bool
    index_worker_enabled: bool
    ttl_sweeper_enabled: bool
    consolidation_enabled: bool
    governed_context_memory_enabled: bool
    route_memory_scopes: tuple[str, ...]

    @property
    def memory_enabled(self) -> bool:
        return self.mode == "on" and self.execution_plane != "decision_shadow"

    @property
    def effective_recall_enabled(self) -> bool:
        return self.recall_enabled and self.execution_plane != "decision_shadow"

    @property
    def effective_formation_mode(self) -> MemoryFormationMode:
        if self.execution_plane == "decision_shadow":
            return "off"
        return self.formation_mode

    @property
    def effective_formation_worker_enabled(self) -> bool:
        return self.effective_formation_mode != "off" and self.formation_worker_enabled

    @property
    def effective_formation_sweeper_enabled(self) -> bool:
        return self.effective_formation_mode != "off" and self.formation_sweeper_enabled

    @property
    def effective_governed_context_memory_enabled(self) -> bool:
        return self.governed_context_memory_enabled and self.execution_plane != "decision_shadow"

    @property
    def effective_index_worker_enabled(self) -> bool:
        return self.index_worker_enabled and self.execution_plane != "decision_shadow"

    @property
    def effective_ttl_sweeper_enabled(self) -> bool:
        return self.ttl_sweeper_enabled and self.execution_plane != "decision_shadow"

    def with_execution_plane(self, execution_plane: MemoryExecutionPlane) -> MemoryRuntimePolicy:
        return replace(self, execution_plane=execution_plane, config_source="host_composition")


def build_memory_runtime_policy(
    mode: MemoryMode | str,
    *,
    execution_plane: MemoryExecutionPlane = "live",
    config_source: str = MEMORY_RUNTIME_CONFIG_SOURCE,
) -> MemoryRuntimePolicy:
    if mode not in {"off", "observe", "on"}:
        raise ValueError("MEMORY_MODE must be one of: off, observe, on")
    if execution_plane not in {"live", "decision_shadow", "state_rehearsal"}:
        raise ValueError("Memory execution plane is unsupported")

    formation_mode: MemoryFormationMode = {
        "off": "off",
        "observe": "observe",
        "on": "enforced",
    }[mode]
    active_formation = mode != "off"
    return MemoryRuntimePolicy(
        mode=mode,
        execution_plane=execution_plane,
        version=MEMORY_RUNTIME_POLICY_VERSION,
        config_source=config_source,
        recall_enabled=mode == "on",
        formation_mode=formation_mode,
        turn_outbox_consumer_enabled=True,
        formation_worker_enabled=active_formation,
        formation_sweeper_enabled=active_formation,
        index_worker_enabled=True,
        ttl_sweeper_enabled=True,
        consolidation_enabled=False,
        governed_context_memory_enabled=mode == "on",
        route_memory_scopes=ROUTE_MEMORY_SCOPES if mode == "on" else (),
    )


def reject_retired_memory_behavior_variables(
    environment: Mapping[str, str] | None = None,
) -> None:
    source = environ if environment is None else environment
    retired = sorted(RETIRED_MEMORY_BEHAVIOR_VARIABLES.intersection(source))
    if retired:
        names = ", ".join(retired)
        raise ValueError(
            f"Retired Memory behavior environment variables detected: {names}. "
            "Remove them and configure MEMORY_MODE=off|observe|on."
        )
