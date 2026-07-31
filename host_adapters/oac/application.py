from dataclasses import dataclass

from app.application import (
    DelegatedRunApplicationPort,
    EventApplicationPort,
    ExecutionTraceApplicationPort,
    MemoryGovernanceApplicationPort,
    MemoryManagementApplicationPort,
    PlanApplicationPort,
    RegistryApplicationPort,
    RoutingApplicationPort,
    TurnApplicationPort,
)


@dataclass(frozen=True)
class OacAdapterApplicationPorts:
    routing: RoutingApplicationPort
    registry: RegistryApplicationPort
    events: EventApplicationPort
    plans: PlanApplicationPort
    delegated_runs: DelegatedRunApplicationPort
    turns: TurnApplicationPort
    execution_traces: ExecutionTraceApplicationPort | None = None
    memory_management: MemoryManagementApplicationPort | None = None
    memory_governance: MemoryGovernanceApplicationPort | None = None
