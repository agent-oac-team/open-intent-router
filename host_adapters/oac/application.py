from dataclasses import dataclass

from app.application import (
    DelegatedRunApplicationPort,
    EventApplicationPort,
    ExecutionTraceApplicationPort,
    KnowledgeApplicationPort,
    KnowledgeAssetApplicationPort,
    MemoryManagementApplicationPort,
    PlanApplicationPort,
    RegistryApplicationPort,
    RoutingApplicationPort,
    TurnApplicationPort,
)


@dataclass(frozen=True)
class OacAdapterApplicationPorts:
    routing: RoutingApplicationPort
    knowledge: KnowledgeApplicationPort
    knowledge_assets: KnowledgeAssetApplicationPort
    registry: RegistryApplicationPort
    events: EventApplicationPort
    plans: PlanApplicationPort
    delegated_runs: DelegatedRunApplicationPort
    turns: TurnApplicationPort
    execution_traces: ExecutionTraceApplicationPort | None = None
    memory_management: MemoryManagementApplicationPort | None = None
