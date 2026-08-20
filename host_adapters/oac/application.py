from dataclasses import dataclass

from app.application import (
    DelegatedRunApplicationPort,
    EventApplicationPort,
    ExecutionTraceApplicationPort,
    ExternalExecutionApplicationPort,
    MemoryGovernanceApplicationPort,
    MemoryManagementApplicationPort,
    PlanApplicationPort,
    PlanPreflightApplicationPort,
    RegistryApplicationPort,
    RegistrySnapshotRefreshApplicationPort,
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
    registry_snapshot_refresh: RegistrySnapshotRefreshApplicationPort | None = None
    plan_preflight: PlanPreflightApplicationPort | None = None
    external_execution: ExternalExecutionApplicationPort | None = None
    execution_traces: ExecutionTraceApplicationPort | None = None
    memory_management: MemoryManagementApplicationPort | None = None
    memory_governance: MemoryGovernanceApplicationPort | None = None
