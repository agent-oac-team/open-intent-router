from collections.abc import Sequence
from urllib.parse import urlsplit

from app.application import RegistrySnapshotQuarantineInput
from app.schemas.agents import (
    AccessPolicy,
    AgentDefinitionV2,
    ExternalExecutionHandling,
    TriggerSpec,
    UiHandoffHandling,
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
    agent: RegistryAgent,
    *,
    catalog: BundleCatalog = OAC_BUNDLE_CATALOG,
    existing: AgentDefinitionV2 | None = None,
) -> AgentDefinitionV2:
    """Translate frozen OAC wire fields at the Host Adapter boundary only."""

    return registry_agent_to_native_v2(agent, catalog=catalog, existing=existing)


def registry_agent_from_native(
    agent: AgentDefinitionV2, *, catalog: BundleCatalog = OAC_BUNDLE_CATALOG
) -> RegistryAgent:
    """Project a canonical v2 Definition back to the unchanged OAC wire shape."""

    return registry_agent_from_native_v2(agent, catalog=catalog)


def registry_definition_to_native_v2(
    agent: AgentDefinitionV2,
    *,
    catalog: BundleCatalog = OAC_BUNDLE_CATALOG,
) -> AgentDefinitionV2:
    """OAC's source is already canonical after the Native hard cut."""

    del catalog
    return agent


def registry_definitions_to_snapshot_inputs(
    definitions: Sequence[object],
) -> list[AgentDefinitionV2 | RegistrySnapshotQuarantineInput]:
    """Quarantine non-v2 source rows instead of reinterpreting legacy fields."""

    inputs: list[AgentDefinitionV2 | RegistrySnapshotQuarantineInput] = []
    for definition in definitions:
        if not isinstance(definition, AgentDefinitionV2):
            inputs.append(
                RegistrySnapshotQuarantineInput(
                    agent_id=None,
                    reason_code="legacy_definition_unmappable",
                )
            )
            continue
        inputs.append(registry_definition_to_native_v2(definition))
    return inputs


def registry_agent_to_native_v2(
    agent: RegistryAgent,
    *,
    catalog: BundleCatalog = OAC_BUNDLE_CATALOG,
    existing: AgentDefinitionV2 | None = None,
) -> AgentDefinitionV2:
    """Translate frozen OAC Registry wire into one canonical v2 Handling."""

    has_bot = bool(agent.bot_id.strip())
    has_route = bool(agent.route_path.strip())
    if has_bot == has_route:
        raise RegistryValidationError("bot_id and route_path must be set exclusively")
    entitlements = _oac_entitlements(agent, catalog=catalog)
    if has_route:
        validate_route_path(agent.route_path)
    try:
        handling = (
            ExternalExecutionHandling(executor_ref=agent.bot_id)
            if has_bot
            else UiHandoffHandling(route=agent.route_path)
        )
    except ValueError as exc:
        raise RegistryValidationError("legacy execution reference is invalid") from exc
    projected = AgentDefinitionV2(
        schema_version="oir-agent-v2",
        agent_id=agent.agent_id,
        name=agent.name,
        description=agent.description,
        enabled=agent.enabled,
        trigger=TriggerSpec(
            keywords=agent.positive_keywords,
            positive_examples=agent.positive_keywords,
            negative_examples=agent.negative_keywords,
        ),
        access_policy=AccessPolicy(
            allow_tenants=["oac"],
            any_entitlements=entitlements,
        ),
        handling=handling,
        source="database",
    )
    if existing is None:
        return projected
    return existing.model_copy(
        update={
            "name": projected.name,
            "description": projected.description,
            "enabled": projected.enabled,
            "trigger": projected.trigger,
            "access_policy": projected.access_policy,
            "handling": projected.handling,
        }
    )


def registry_agent_from_native_v2(
    agent: AgentDefinitionV2,
    *,
    catalog: BundleCatalog = OAC_BUNDLE_CATALOG,
) -> RegistryAgent:
    """Project canonical v2 Handling back to the unchanged OAC Legacy wire."""

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
    handling = agent.handling
    if isinstance(handling, ExternalExecutionHandling):
        bot_id, route_path = handling.executor_ref, ""
    elif isinstance(handling, UiHandoffHandling):
        bot_id, route_path = "", handling.route
    else:
        raise RegistryPolicyProjectionError(
            "OAC Legacy Registry cannot project Invocation Handling"
        )
    return RegistryAgent(
        agent_id=agent.agent_id,
        name=agent.name,
        description=agent.description,
        bot_id=bot_id,
        route_path=route_path,
        allowed_user_tags=stable_tags,
        positive_keywords=agent.trigger.keywords or agent.trigger.positive_examples,
        negative_keywords=agent.trigger.negative_examples,
        enabled=agent.enabled,
    )


def _oac_entitlements(agent: RegistryAgent, *, catalog: BundleCatalog) -> list[str]:
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
    return entitlements


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
