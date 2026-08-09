import pytest

from app.schemas.agents import AccessPolicy, AgentDefinition, InvocationSpec
from host_adapters.oac.authz.bundles import (
    OAC_BUNDLE_CATALOG,
    BundleCatalog,
    EntitlementBundle,
)
from host_adapters.oac.mappers.registry import (
    RegistryPolicyProjectionError,
    RegistryValidationError,
    registry_agent_from_native,
    registry_agent_to_native,
)
from host_adapters.oac.schemas.registry import RegistryAgent


def _wire(**updates) -> RegistryAgent:
    payload = {
        "agent_id": "new-agent",
        "name": "New Agent",
        "description": "Configured without code changes",
        "bot_id": "bot-1",
        "route_path": "",
        "allowed_user_tags": ["运营版"],
    }
    payload.update(updates)
    return RegistryAgent.model_validate(payload)


def test_oac_bundle_catalog_is_stable_and_reversible() -> None:
    assert OAC_BUNDLE_CATALOG.policy_version == "oac-authz-v1"
    assert OAC_BUNDLE_CATALOG.entitlements_for_tag("运营版") == ("workspace.operations.access",)
    assert OAC_BUNDLE_CATALOG.entitlements_for_tag("展业版") == (
        "workspace.sales_enablement.access",
    )
    assert OAC_BUNDLE_CATALOG.legacy_tag_for_entitlement("workspace.operations.access") == "运营版"


@pytest.mark.parametrize(
    "bundles",
    [
        (
            EntitlementBundle("duplicate", "A", ("workspace.a.access",)),
            EntitlementBundle("duplicate", "B", ("workspace.b.access",)),
        ),
        (EntitlementBundle("empty", "A", ()),),
        (EntitlementBundle("bad-action", "A", ("workspace.a.invoke",)),),
        (EntitlementBundle("non-ascii", "A", ("工作区.access",)),),
    ],
)
def test_invalid_bundle_catalog_fails_startup_validation(bundles) -> None:
    with pytest.raises(ValueError):
        BundleCatalog(policy_version="test-v1", bundles=bundles)


def test_existing_bundles_configure_provider_and_ui_agents_without_agent_mapping() -> None:
    provider = registry_agent_to_native(_wire(allowed_user_tags=["运营版", "展业版", "运营版"]))
    ui = registry_agent_to_native(
        _wire(bot_id="", route_path="/new-agent", allowed_user_tags=["展业版"])
    )

    assert provider.type == "provider_platform"
    assert provider.access_policy.any_entitlements == [
        "workspace.operations.access",
        "workspace.sales_enablement.access",
    ]
    assert ui.type == "ui_handoff"
    assert ui.access_policy.any_entitlements == ["workspace.sales_enablement.access"]
    assert registry_agent_from_native(provider).allowed_user_tags == ["运营版", "展业版"]


@pytest.mark.parametrize(
    "wire",
    [
        _wire(allowed_user_tags=[]),
        _wire(allowed_user_tags=["未知版"]),
        _wire(bot_id="", route_path=""),
        _wire(bot_id="bot-1", route_path="/also-set"),
    ],
)
def test_registry_mapper_rejects_invalid_legacy_writes(wire: RegistryAgent) -> None:
    with pytest.raises(RegistryValidationError):
        registry_agent_to_native(wire)


@pytest.mark.parametrize(
    "policy",
    [
        AccessPolicy(),
        AccessPolicy(any_entitlements=["workspace.unknown.access"]),
        AccessPolicy(
            allow_groups=["运营版"],
            any_entitlements=["workspace.operations.access"],
        ),
    ],
)
def test_registry_reverse_projection_fails_closed(policy: AccessPolicy) -> None:
    agent = AgentDefinition(
        agent_id="agent",
        name="Agent",
        description="Agent",
        type="provider_platform",
        access_policy=policy,
        invocation=InvocationSpec(type="provider_platform", provider_config={"bot_id": "bot-1"}),
    )
    with pytest.raises(RegistryPolicyProjectionError):
        registry_agent_from_native(agent)
