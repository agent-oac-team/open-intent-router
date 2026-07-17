from dataclasses import dataclass

from app.application import (
    DelegatedRunApplicationPort,
    EventApplicationPort,
    KnowledgeApplicationPort,
    KnowledgeAssetApplicationPort,
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
