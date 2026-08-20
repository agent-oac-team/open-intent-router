import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import RegistryVersionConflict
from app.repositories.external_execution_acceptances import MemoryExternalExecutionAcceptanceStore
from app.schemas.agents import AccessPolicy, AgentDefinition, InvocationSpec
from app.schemas.external_execution import (
    ExternalExecutionPrincipal,
    ExternalExecutorAcceptanceRequest,
)
from app.services.registry_snapshot import (
    RegistryQuarantineEntry,
    RegistrySnapshotBuilder,
    RegistrySnapshotRuntime,
)
from host_adapters.oac.api.registry import router
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.external_executor import OacExternalExecutor
from host_adapters.oac.identity.models import TrustedHostIdentity
from host_adapters.oac.mappers.registry import (
    InvalidRoutePath,
    registry_agent_from_native,
    registry_agent_from_native_v2,
    registry_agent_to_native,
    registry_agent_to_native_v2,
    registry_definitions_to_snapshot_inputs,
)
from host_adapters.oac.schemas.registry import RegistryAgent
from host_apps.oac.dependencies import (
    get_host_identity_verifier,
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)

FIXTURES = Path("tests/contract/oac_irs/irs-baseline/v1/success")


class RegistryPort:
    def __init__(self) -> None:
        self.agents = {}
        self.force_conflict = False

    async def list_definitions(self, *, enabled_only=False):
        return list(self.agents.values())

    async def get_definition(self, agent_id):
        return self.agents.get(agent_id)

    async def upsert_definition(self, definition, *, expected_revision=None):
        current = self.agents.get(definition.agent_id)
        revision = current.revision if current else 0
        if self.force_conflict and current is not None:
            raise RegistryVersionConflict("revision conflict")
        if expected_revision is not None and revision != expected_revision:
            raise ValueError("revision conflict")
        saved = definition.model_copy(update={"revision": revision + 1})
        self.agents[definition.agent_id] = saved
        return saved

    async def set_enabled(self, agent_id, enabled, *, expected_revision=None):
        current = self.agents.get(agent_id)
        if current is None:
            return None
        if expected_revision is not None and current.revision != expected_revision:
            raise ValueError("revision conflict")
        current = current.model_copy(update={"enabled": enabled, "revision": current.revision + 1})
        self.agents[agent_id] = current
        return current

    async def delete_definition(self, agent_id, *, expected_revision=None):
        current = self.agents.get(agent_id)
        if current and expected_revision is not None and current.revision != expected_revision:
            raise ValueError("revision conflict")
        return self.agents.pop(agent_id, None) is not None

    async def mutate_definition(self, command):
        if command.operation in {"create", "update"}:
            saved = await self.upsert_definition(
                command.definition, expected_revision=command.expected_revision
            )
            return SimpleNamespace(after=saved)
        if command.operation in {"enable", "disable"}:
            saved = await self.set_enabled(
                command.agent_id,
                command.operation == "enable",
                expected_revision=command.expected_revision,
            )
            return SimpleNamespace(after=saved)
        await self.delete_definition(command.agent_id, expected_revision=command.expected_revision)
        return SimpleNamespace(after=None)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class SnapshotRefreshProbe:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.registries: list[RegistryPort] = []

    async def refresh_registry_snapshot(self, registry: RegistryPort) -> bool:
        self.registries.append(registry)
        return self.succeeds


def _client(
    *,
    credential_class="oac_admin",
    force_conflict=False,
    snapshot_refresh: SnapshotRefreshProbe | None = None,
) -> TestClient:
    registry = RegistryPort()
    registry.force_conflict = force_conflict
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        registry=registry,
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=SimpleNamespace(),
        registry_snapshot_refresh=snapshot_refresh,
    )
    identity = TrustedHostIdentity(
        key_id="admin-key",
        audience="test",
        principal_type="user" if credential_class != "coze_workflow" else "service",
        tenant_id="oac",
        user_id="admin-1",
        groups=(),
        credential_class=credential_class,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    app.dependency_overrides[get_trusted_host_identity] = lambda: identity
    app.state.registry = registry
    return TestClient(app)


def test_registry_fixtures_parse_and_mapper_round_trips_legacy_fields() -> None:
    request = RegistryAgent.model_validate(_fixture("registry-create")["request"]["body"])
    native = registry_agent_to_native(request)
    projected = registry_agent_from_native(native)

    assert projected == request
    assert native.invocation.provider_config == {}
    assert native.ui_handoff.route == "/fixture"
    assert native.access_policy.allow_groups == []
    assert native.access_policy.any_entitlements == ["workspace.operations.access"]
    assert native.trigger.negative_examples == ["忽略"]


def test_registry_legacy_wire_maps_to_one_canonical_v2_handling_without_changing_wire() -> None:
    bot = RegistryAgent.model_validate(
        {
            "agent_id": "external_agent",
            "name": "External",
            "description": "delegated externally",
            "bot_id": "host_executor",
            "route_path": "",
            "allowed_user_tags": ["运营版"],
            "positive_keywords": ["delegate"],
            "negative_keywords": [],
            "enabled": True,
        }
    )
    ui = RegistryAgent.model_validate(_fixture("registry-create")["request"]["body"])

    canonical_bot = registry_agent_to_native_v2(bot)
    canonical_ui = registry_agent_to_native_v2(ui)

    assert canonical_bot.handling.model_dump(mode="json", exclude_none=True) == {
        "kind": "external_execution",
        "executor_ref": "host_executor",
        "params": {},
    }
    assert canonical_ui.handling.model_dump(mode="json", exclude_none=True) == {
        "kind": "ui_handoff",
        "route": "/fixture",
        "params": {},
    }
    assert registry_agent_from_native_v2(canonical_bot) == bot
    assert registry_agent_from_native_v2(canonical_ui) == ui
    assert "bot_id" not in canonical_bot.model_dump(mode="json")
    assert "route_path" not in canonical_ui.model_dump(mode="json")


async def test_oac_external_executor_requires_an_explicit_host_capability() -> None:
    store = MemoryExternalExecutionAcceptanceStore()
    executor = OacExternalExecutor(
        supported_executor_refs={"registered_bot"},
        acceptance_store=store,
        acceptance_fingerprint_secret="oac-test-secret",
    )
    unknown = ExternalExecutorAcceptanceRequest(
        acceptance_id="external_acceptance_unknown",
        executor_ref="unknown_bot",
        agent_id="external_agent",
        agent_revision=1,
        principal=ExternalExecutionPrincipal(tenant_id="oac", user_id="user-1"),
    )

    snapshot = RegistrySnapshotBuilder(None, external_executor=executor).build(
        [
            registry_agent_to_native_v2(
                RegistryAgent(
                    agent_id="external_agent",
                    name="Unknown External",
                    description="must not be delegated",
                    bot_id="unknown_bot",
                    route_path="",
                    allowed_user_tags=["运营版"],
                    positive_keywords=["delegate"],
                    negative_keywords=[],
                )
            )
        ],
        source="oac-unknown-executor-test",
    )

    assert executor.supports("unknown_bot") is False
    assert (
        snapshot.entry_for("external_agent").isolation_reason_code
        == "external_executor_unsupported"
    )
    rejected = await executor.accept(unknown)
    assert rejected.accepted is False
    assert rejected.reason_code == "external_executor_unsupported"
    assert store.records == {}


async def test_oac_external_executor_rechecks_health_for_a_prior_acceptance() -> None:
    store = MemoryExternalExecutionAcceptanceStore()
    request = ExternalExecutorAcceptanceRequest(
        acceptance_id="external_acceptance_health",
        executor_ref="registered_bot",
        agent_id="external_agent",
        agent_revision=1,
        principal=ExternalExecutionPrincipal(tenant_id="oac", user_id="user-1"),
    )
    healthy = OacExternalExecutor(
        supported_executor_refs={"registered_bot"},
        acceptance_store=store,
        acceptance_fingerprint_secret="oac-test-secret",
    )

    assert (await healthy.accept(request)).accepted is True
    unhealthy = OacExternalExecutor(
        supported_executor_refs={"registered_bot"},
        acceptance_store=store,
        acceptance_fingerprint_secret="oac-test-secret",
        healthy=False,
    )
    rejected = await unhealthy.accept(request)

    assert rejected.accepted is False
    assert rejected.reason_code == "external_executor_unhealthy"
    assert len(store.records) == 1


async def test_oac_external_acceptance_fingerprint_is_keyed_and_requires_a_secret() -> None:
    request = ExternalExecutorAcceptanceRequest(
        acceptance_id="external_acceptance_keyed",
        executor_ref="registered_bot",
        agent_id="external_agent",
        agent_revision=1,
        principal=ExternalExecutionPrincipal(
            tenant_id="oac",
            user_id="user-1",
            entitlements=["premium"],
        ),
        params={"task": "low-entropy-value"},
    )
    first_store = MemoryExternalExecutionAcceptanceStore()
    second_store = MemoryExternalExecutionAcceptanceStore()
    first = OacExternalExecutor(
        supported_executor_refs={"registered_bot"},
        acceptance_store=first_store,
        acceptance_fingerprint_secret="first-host-secret",
    )
    second = OacExternalExecutor(
        supported_executor_refs={"registered_bot"},
        acceptance_store=second_store,
        acceptance_fingerprint_secret="second-host-secret",
    )

    assert (await first.accept(request)).accepted is True
    assert (await second.accept(request)).accepted is True
    assert (
        first_store.records[request.acceptance_id].request_fingerprint
        != second_store.records[request.acceptance_id].request_fingerprint
    )
    with pytest.raises(ValueError, match="fingerprint secret"):
        OacExternalExecutor(
            supported_executor_refs={"registered_bot"},
            acceptance_store=MemoryExternalExecutionAcceptanceStore(),
        )


@pytest.mark.parametrize("route", ["https://evil.example/x", "//evil", "/a/../b", "/a\\b"])
def test_registry_mapper_rejects_non_internal_route_paths(route) -> None:
    body = _fixture("registry-create")["request"]["body"]
    body["route_path"] = route
    with pytest.raises(InvalidRoutePath):
        registry_agent_to_native(RegistryAgent.model_validate(body))


def test_registry_crud_handlers_match_frozen_status_and_shapes() -> None:
    client = _client()
    create_body = _fixture("registry-create")["request"]["body"]
    created = client.post("/api/v1/admin/agent-registry", json=create_body)
    assert created.status_code == 200
    assert created.json() == _fixture("registry-create")["response"]["body"]

    listed = client.get("/api/v1/admin/agent-registry")
    assert listed.status_code == 200
    assert listed.json() == [created.json()]

    update_body = _fixture("registry-update")["request"]["body"]
    updated = client.put("/api/v1/admin/agent-registry/fixture_agent", json=update_body)
    assert updated.status_code == 200
    assert updated.json()["description"] == "更新后的契约测试智能体"

    disabled = client.patch(
        "/api/v1/admin/agent-registry/fixture_agent/enabled",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    deleted = client.delete("/api/v1/admin/agent-registry/fixture_agent")
    assert deleted.status_code == 204
    assert client.get("/api/v1/admin/agent-registry").json() == []


def test_registry_crud_refreshes_the_process_snapshot_after_each_committed_write() -> None:
    refresh = SnapshotRefreshProbe()
    client = _client(snapshot_refresh=refresh)
    create_body = _fixture("registry-create")["request"]["body"]

    assert client.post("/api/v1/admin/agent-registry", json=create_body).status_code == 200
    assert (
        client.put(
            "/api/v1/admin/agent-registry/fixture_agent",
            json=_fixture("registry-update")["request"]["body"],
        ).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/v1/admin/agent-registry/fixture_agent/enabled",
            json={"enabled": False},
        ).status_code
        == 200
    )
    assert client.delete("/api/v1/admin/agent-registry/fixture_agent").status_code == 204

    assert refresh.registries == [client.app.state.registry] * 4


def test_registry_write_returns_a_safe_error_when_snapshot_refresh_fails() -> None:
    client = _client(snapshot_refresh=SnapshotRefreshProbe(succeeds=False))

    response = client.post(
        "/api/v1/admin/agent-registry",
        json=_fixture("registry-create")["request"]["body"],
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "registry_snapshot_unavailable"}


def test_oac_snapshot_mapper_quarantines_bad_legacy_rows_with_safe_repair_metadata() -> None:
    incompatible = AgentDefinition(
        agent_id="repairable-agent",
        name="Unmappable",
        description="legacy policy cannot be projected",
        type="provider_platform",
        access_policy=AccessPolicy(
            allow_tenants=["oac"],
            any_entitlements=["workspace.foreign.access"],
        ),
        invocation=InvocationSpec(
            type="provider_platform",
            provider_config={"bot_id": "registered_bot"},
        ),
    )

    inputs = registry_definitions_to_snapshot_inputs([incompatible])
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(None))
    snapshot_runtime.load(inputs, source="oac_legacy_registry")

    assert snapshot_runtime.admin_quarantine_inventory() == (
        RegistryQuarantineEntry(
            source_index=0,
            agent_id="repairable-agent",
            reason_code="legacy_definition_unmappable",
        ),
    )


def test_registry_writes_reject_non_admin_credentials() -> None:
    response = _client(credential_class="coze_workflow").post(
        "/api/v1/admin/agent-registry",
        json=_fixture("registry-create")["request"]["body"],
    )
    assert response.status_code == 403


def test_registry_concurrent_update_returns_stable_conflict_response() -> None:
    client = _client(force_conflict=True)
    create_body = _fixture("registry-create")["request"]["body"]
    assert client.post("/api/v1/admin/agent-registry", json=create_body).status_code == 200
    response = client.put(
        "/api/v1/admin/agent-registry/fixture_agent",
        json=_fixture("registry-update")["request"]["body"],
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "agent_revision_conflict"}


def test_registry_legacy_projection_does_not_expose_provider_secrets() -> None:
    native = AgentDefinition(
        agent_id="secure-agent",
        name="Secure",
        description="secure provider",
        type="provider_platform",
        invocation=InvocationSpec(
            type="provider_platform",
            provider_config={"bot_id": "bot-1", "access_token": "secret-token"},
        ),
        access_policy=AccessPolicy(any_entitlements=["workspace.operations.access"]),
    )
    serialized = registry_agent_from_native(native).model_dump_json()
    assert "bot-1" in serialized
    assert "secret-token" not in serialized
    assert "access_token" not in serialized


def test_registry_compat_update_preserves_native_context_configuration() -> None:
    request = RegistryAgent.model_validate(_fixture("registry-update")["request"]["body"])
    current = registry_agent_to_native(
        RegistryAgent.model_validate(_fixture("registry-create")["request"]["body"])
    )
    current = current.model_copy(
        update={
            "context": current.context.model_copy(
                update={
                    "memory": current.context.memory.model_copy(
                        update={
                            "mode": "prefetch",
                            "scopes": ["user_preference", "stable_fact"],
                            "max_items": 5,
                        }
                    )
                }
            ),
            "metadata": {"native_only": True},
        }
    )

    updated = registry_agent_to_native(request, existing=current)

    assert updated.description == request.description
    assert updated.context == current.context
    assert updated.metadata == {"native_only": True}


@pytest.mark.parametrize(
    "updates",
    [
        {"allowed_user_tags": []},
        {"allowed_user_tags": ["未知版"]},
        {"bot_id": "", "route_path": ""},
        {"bot_id": "bot-1", "route_path": "/fixture"},
        {"route_path": "https://evil.example/agent"},
    ],
)
def test_registry_handler_returns_stable_validation_error(updates) -> None:
    body = {**_fixture("registry-create")["request"]["body"], **updates}
    response = _client().post("/api/v1/admin/agent-registry", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "registry_validation_failed"}


def test_registry_list_returns_conflict_for_unprojectable_policy() -> None:
    client = _client()
    client.app.state.registry.agents["foreign-policy"] = AgentDefinition(
        agent_id="foreign-policy",
        name="Foreign",
        description="Cannot project to OAC tags",
        type="provider_platform",
        access_policy=AccessPolicy(
            any_entitlements=["workspace.foreign.access"], allow_tenants=["oac"]
        ),
        invocation=InvocationSpec(type="provider_platform", provider_config={"bot_id": "bot-1"}),
    )
    response = client.get("/api/v1/admin/agent-registry")
    assert response.status_code == 409
    assert response.json() == {"detail": "registry_policy_not_legacy_projectable"}


def test_registry_token_only_request_fails_real_host_verifier() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: _client().app.state
    get_host_identity_verifier.cache_clear()
    try:
        response = TestClient(app).get(
            "/api/v1/admin/agent-registry",
            headers={"X-Admin-Sync-Token": "legacy-token"},
        )
    finally:
        get_host_identity_verifier.cache_clear()

    assert response.status_code == 401
    assert response.json() == {"detail": "host_authentication_failed"}
