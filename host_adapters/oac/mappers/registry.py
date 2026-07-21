from urllib.parse import urlsplit

from app.schemas.agents import (
    AccessPolicy,
    AgentDefinition,
    InvocationSpec,
    TriggerSpec,
    UiHandoffSpec,
)
from host_adapters.oac.authz import OAC_BUNDLE_CATALOG, BundleCatalog
from host_adapters.oac.schemas.registry import RegistryAgent


class InvalidRoutePath(ValueError):
    pass


class RegistryValidationError(ValueError):
    pass


class RegistryPolicyProjectionError(ValueError):
    pass


def registry_agent_to_native(
    agent: RegistryAgent, *, catalog: BundleCatalog = OAC_BUNDLE_CATALOG
) -> AgentDefinition:
    has_bot = bool(agent.bot_id.strip())
    has_route = bool(agent.route_path.strip())
    if has_bot == has_route:
        raise RegistryValidationError("bot_id and route_path must be set exclusively")
    try:
        entitlements = sorted(
            {
                entitlement
                for tag in agent.allowed_user_tags
                if tag.strip()
                for entitlement in catalog.entitlements_for_tag(tag.strip())
            }
        )
    except KeyError as exc:
        raise RegistryValidationError("allowed_user_tags contains an unknown tag") from exc
    if not entitlements or any(not tag.strip() for tag in agent.allowed_user_tags):
        raise RegistryValidationError("allowed_user_tags must contain known non-empty tags")
    if agent.route_path:
        validate_route_path(agent.route_path)
    agent_type = "provider_platform" if has_bot else "ui_handoff"
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
            allow_tenants=["oac"],
            any_entitlements=entitlements,
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


def registry_agent_from_native(
    agent: AgentDefinition, *, catalog: BundleCatalog = OAC_BUNDLE_CATALOG
) -> RegistryAgent:
    if any(group in catalog.by_tag for group in agent.access_policy.allow_groups):
        raise RegistryPolicyProjectionError("OAC legacy groups remain in Core policy")
    try:
        tags = {
            catalog.legacy_tag_for_entitlement(entitlement)
            for entitlement in agent.access_policy.any_entitlements
        }
    except KeyError as exc:
        raise RegistryPolicyProjectionError("policy contains an unknown entitlement") from exc
    if not tags:
        raise RegistryPolicyProjectionError("OAC Agent entitlement policy is empty")
    stable_tags = [bundle.legacy_tag for bundle in catalog.bundles if bundle.legacy_tag in tags]
    bot_id = agent.invocation.provider_config.get("bot_id", "")
    return RegistryAgent(
        agent_id=agent.agent_id,
        name=agent.name,
        description=agent.description,
        bot_id=str(bot_id) if bot_id is not None else "",
        route_path=agent.ui_handoff.route or "",
        allowed_user_tags=stable_tags,
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
