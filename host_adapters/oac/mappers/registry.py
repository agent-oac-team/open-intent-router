from urllib.parse import urlsplit

from app.schemas.agents import (
    AccessPolicy,
    AgentDefinition,
    InvocationSpec,
    TriggerSpec,
    UiHandoffSpec,
)
from host_adapters.oac.schemas.registry import RegistryAgent


class InvalidRoutePath(ValueError):
    pass


def registry_agent_to_native(agent: RegistryAgent) -> AgentDefinition:
    if agent.route_path:
        validate_route_path(agent.route_path)
    agent_type = "provider_platform" if agent.bot_id else "ui_handoff"
    return AgentDefinition(
        agent_id=agent.agent_id,
        name=agent.name,
        description=agent.description,
        enabled=agent.enabled,
        type=agent_type,
        trigger=TriggerSpec(
            keywords=agent.positive_keywords,
            positive_examples=agent.positive_keywords,
            negative_examples=agent.negative_keywords,
        ),
        access_policy=AccessPolicy(
            allow_groups=agent.allowed_user_tags,
            allow_tenants=["oac"],
        ),
        invocation=InvocationSpec(
            type=agent_type,
            provider_config={"bot_id": agent.bot_id} if agent.bot_id else {},
        ),
        ui_handoff=UiHandoffSpec(
            mode="route" if agent.route_path else "none",
            route=agent.route_path or None,
        ),
        metadata={"legacy_contract": "irs-agent-registry-v1"},
        source="database",
    )


def registry_agent_from_native(agent: AgentDefinition) -> RegistryAgent:
    bot_id = agent.invocation.provider_config.get("bot_id", "")
    return RegistryAgent(
        agent_id=agent.agent_id,
        name=agent.name,
        description=agent.description,
        bot_id=str(bot_id) if bot_id is not None else "",
        route_path=agent.ui_handoff.route or "",
        allowed_user_tags=agent.access_policy.allow_groups,
        positive_keywords=agent.trigger.keywords or agent.trigger.positive_examples,
        negative_keywords=agent.trigger.negative_examples,
        enabled=agent.enabled,
    )


def validate_route_path(route_path: str) -> None:
    parsed = urlsplit(route_path)
    if (
        not route_path.startswith("/")
        or route_path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or any(part == ".." for part in parsed.path.split("/"))
        or "\\" in route_path
    ):
        raise InvalidRoutePath("route_path must be an OAC internal path")
