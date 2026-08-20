from dataclasses import dataclass

import pytest

from app.core.config import Settings
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalog,
    build_default_runtime_descriptors,
)
from app.schemas.agents import AgentDefinitionV2, InvocationHandling
from app.schemas.common import UserContext
from app.services.registry_snapshot import (
    RegistryDefinitionValidationError,
    RegistryQuarantineEntry,
    RegistrySnapshotBuilder,
    RegistrySnapshotRuntime,
)


@dataclass
class _Adapter:
    key: str


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


async def _catalog(*descriptors: RuntimeAdapterDescriptor) -> RuntimeCatalog:
    return await RuntimeCatalog.activate(
        descriptors,
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )


def _descriptor(
    key: str,
    *,
    config_schema: dict[str, object] | None = None,
    invocation: bool = True,
    v2_invocation: bool = True,
) -> RuntimeAdapterDescriptor:
    return RuntimeAdapterDescriptor(
        key=key,
        contract_version="oir-runtime-adapter-v1",
        implementation_version="test-v1",
        config_schema=config_schema or {"type": "object"},
        capability=RuntimeAdapterCapability(
            invocation=invocation,
            v2_invocation=v2_invocation,
        ),
        factory=lambda _context: _Adapter(key),
        health_check=_healthy,
        lifecycle=RuntimeAdapterLifecycle(activate=_noop, dispose=_noop),
    )


def _invocation_definition(**updates: object) -> AgentDefinitionV2:
    payload: dict[str, object] = {
        "schema_version": "oir-agent-v2",
        "agent_id": "summary-agent",
        "name": "Summary Agent",
        "description": "Summarizes approved documents.",
        "revision": 7,
        "access_policy": {"allow_roles": ["operator"]},
        "handling": {
            "kind": "invocation",
            "adapter_key": "local_function",
            "connector_ref": "tenant-tools",
            "config": {"function": "side_effect", "max_tokens": 256},
        },
    }
    payload.update(updates)
    return AgentDefinitionV2.model_validate(payload)


def _definition(
    agent_id: str,
    handling: dict[str, object],
    **updates: object,
) -> AgentDefinitionV2:
    payload: dict[str, object] = {
        "schema_version": "oir-agent-v2",
        "agent_id": agent_id,
        "name": agent_id.replace("-", " ").title(),
        "description": "A test Agent.",
        "access_policy": {"allow_roles": ["operator"]},
        "handling": handling,
    }
    payload.update(updates)
    return AgentDefinitionV2.model_validate(payload)


@pytest.mark.asyncio
async def test_snapshot_compiles_an_immutable_bindable_definition_with_stable_identity() -> None:
    catalog = await _catalog(
        _descriptor(
            "local_function",
            config_schema={
                "type": "object",
                "required": ["function"],
                "properties": {
                    "function": {"const": "side_effect"},
                    "max_tokens": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        )
    )
    builder = RegistrySnapshotBuilder(catalog)
    definition = _invocation_definition()

    first = builder.build([definition], source="test")
    second = builder.build([definition], source="test")
    revised = builder.build([definition.model_copy(update={"revision": 8})], source="test")
    selected = first.select_for_user(
        "summary-agent",
        UserContext(id="operator-1", roles=["operator"]),
    )

    assert first.snapshot_id == second.snapshot_id
    assert revised.snapshot_id != first.snapshot_id
    assert selected is not None
    assert selected.snapshot_id == first.snapshot_id
    assert selected.definition.revision == 7
    assert selected.definition.handling.kind == "invocation"
    assert selected.binding_requirement.to_payload() == {
        "kind": "invocation",
        "adapter_key": "local_function",
        "connector_ref": "tenant-tools",
        "config": {"function": "side_effect", "max_tokens": 256},
    }
    with pytest.raises(TypeError):
        first.entries["other-agent"] = selected.entry
    with pytest.raises(TypeError):
        selected.binding_requirement.config["function"] = "other"

    selected.definition.name = "Changed outside the snapshot"

    selected_again = first.select_for_user(
        "summary-agent",
        UserContext(id="operator-1", roles=["operator"]),
    )
    assert selected_again is not None
    assert selected_again.definition.name == "Summary Agent"

    await catalog.aclose()


@pytest.mark.asyncio
async def test_snapshot_runtime_swaps_only_complete_loads_and_preserves_selected_entries() -> None:
    catalog = await _catalog(_descriptor("local_function"))
    runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    user = UserContext(id="operator-1", roles=["operator"])
    first = runtime.load([_invocation_definition()], source="test")
    selected = runtime.select_for_user("summary-agent", user)

    assert selected is not None
    assert selected.snapshot_id == first.snapshot_id
    assert selected.definition.revision == 7

    second = runtime.load(
        [_invocation_definition(revision=8)],
        source="test",
    )

    assert runtime.snapshot is second
    assert second.snapshot_id != first.snapshot_id
    assert runtime.select_for_user("summary-agent", user).definition.revision == 8
    assert selected.definition.revision == 7

    with pytest.raises(ValueError):
        runtime.load([_invocation_definition()], source="")

    assert runtime.snapshot is second

    await catalog.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_key", ["http", "local_function"])
async def test_snapshot_isolates_an_installed_legacy_only_adapter_from_v2_candidates(
    adapter_key: str,
) -> None:
    descriptors = {item.key: item for item in build_default_runtime_descriptors()}
    catalog = await _catalog(descriptors[adapter_key])
    builder = RegistrySnapshotBuilder(catalog)
    definition = _definition(
        f"legacy-{adapter_key}-agent",
        {
            "kind": "invocation",
            "adapter_key": adapter_key,
            "config": {"function": "side_effect"},
        },
    )

    snapshot = builder.build([definition], source="test")
    user = UserContext(id="operator-1", roles=["operator"])

    assert snapshot.candidates_for_user(user) == ()
    assert snapshot.select_for_user(f"legacy-{adapter_key}-agent", user) is None
    entry = snapshot.entry_for(f"legacy-{adapter_key}-agent")
    assert entry is not None
    assert entry.isolation_reason_code == "invocation_adapter_incompatible"

    await catalog.aclose()


@pytest.mark.asyncio
async def test_snapshot_quarantines_invalid_rows_and_excludes_unbindable_definitions() -> None:
    catalog = await _catalog(
        _descriptor(
            "local_function",
            config_schema={
                "type": "object",
                "required": ["function"],
                "properties": {
                    "function": {"const": "side_effect"},
                    "max_tokens": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        )
    )
    builder = RegistrySnapshotBuilder(catalog, supported_executor_refs={"supported-executor"})
    bindable = _invocation_definition()
    missing_adapter = _definition(
        "missing-adapter",
        {
            "kind": "invocation",
            "adapter_key": "missing",
            "config": {"function": "side_effect"},
        },
    )
    invalid_config = _definition(
        "invalid-config",
        {
            "kind": "invocation",
            "adapter_key": "local_function",
            "config": {"function": "other"},
        },
    )
    unsupported_executor = _definition(
        "unsupported-executor",
        {
            "kind": "external_execution",
            "executor_ref": "unavailable-executor",
            "params": {"task": "perform_work"},
        },
    )
    disabled = _definition(
        "disabled-agent",
        {"kind": "ui_handoff", "route": "/agents/disabled"},
        enabled=False,
    )
    invalid_raw = {
        "schema_version": "oir-agent-v2",
        "agent_id": "invalid-schema",
        "name": "Invalid Schema",
        "description": "Contains a forbidden deployment field.",
        "handling": {
            "kind": "invocation",
            "adapter_key": "local_function",
            "config": {"headers": {"Authorization": "Bearer secret"}},
        },
    }

    snapshot = builder.build(
        [
            bindable,
            missing_adapter,
            invalid_config,
            unsupported_executor,
            disabled,
            invalid_raw,
        ],
        source="test",
    )
    user = UserContext(id="operator-1", roles=["operator"])

    assert [agent.agent_id for agent in snapshot.public_definitions(user)] == ["summary-agent"]
    assert [agent.agent_id for agent in snapshot.candidates_for_user(user)] == ["summary-agent"]
    assert snapshot.select_for_user("missing-adapter", user) is None
    assert snapshot.select_for_user("invalid-config", user) is None
    assert snapshot.select_for_user("unsupported-executor", user) is None
    assert snapshot.select_for_user("disabled-agent", user) is None
    assert (
        snapshot.entry_for("missing-adapter").isolation_reason_code == "invocation_adapter_missing"
    )
    assert snapshot.entry_for("invalid-config").isolation_reason_code == "invocation_config_invalid"
    assert (
        snapshot.entry_for("unsupported-executor").isolation_reason_code
        == "external_executor_unsupported"
    )
    assert snapshot.entry_for("disabled-agent").binding_status == "disabled"
    assert [(entry.agent_id, entry.reason_code) for entry in snapshot.quarantined] == [
        ("invalid-schema", "definition_schema_invalid")
    ]

    await catalog.aclose()


@pytest.mark.parametrize(
    "unsafe_agent_id",
    [
        "https://private.example/agent?token=secret-marker",
        "sk_live_0123456789abcdef",
    ],
)
def test_snapshot_quarantines_an_unsafe_definition_identifier_without_exposing_it(
    unsafe_agent_id: str,
) -> None:
    runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(None))
    definition = _definition(
        unsafe_agent_id,
        {"kind": "ui_handoff", "route": "/safe"},
    )

    runtime.load([definition], source="test")

    assert runtime.admin_inventory() == ()
    assert runtime.admin_quarantine_inventory() == (
        RegistryQuarantineEntry(
            source_index=0,
            agent_id=None,
            reason_code="definition_schema_invalid",
        ),
    )
    assert unsafe_agent_id not in str(runtime.admin_quarantine_inventory())


@pytest.mark.asyncio
async def test_snapshot_uses_one_visibility_rule_for_public_candidates_and_exact_selection() -> (
    None
):
    catalog = await _catalog(_descriptor("local_function"))
    builder = RegistrySnapshotBuilder(catalog)
    operator_only = _invocation_definition()
    raw_definitions: list[object] = [operator_only]
    snapshot = builder.build(raw_definitions, source="test")
    raw_definitions[0] = _invocation_definition(name="Changed source after load")
    operator = UserContext(id="operator-1", roles=["operator"])
    guest = UserContext(id="guest-1", roles=["guest"])

    assert [agent.agent_id for agent in snapshot.public_definitions(operator)] == ["summary-agent"]
    assert [agent.agent_id for agent in snapshot.candidates_for_user(operator)] == ["summary-agent"]
    assert snapshot.select_for_user("summary-agent", operator) is not None
    assert snapshot.public_definitions(guest) == ()
    assert snapshot.candidates_for_user(guest) == ()
    assert snapshot.select_for_user("summary-agent", guest) is None
    selected = snapshot.select_for_user("summary-agent", operator)
    assert selected is not None
    assert selected.definition.name == "Summary Agent"

    await catalog.aclose()


@pytest.mark.asyncio
async def test_snapshot_rejects_a_write_that_cannot_bind_in_the_current_deployment() -> None:
    catalog = await _catalog(_descriptor("local_function"))
    builder = RegistrySnapshotBuilder(catalog)
    unsupported = _definition(
        "missing-adapter",
        {
            "kind": "invocation",
            "adapter_key": "missing",
            "config": {"function": "side_effect"},
        },
    )

    with pytest.raises(RegistryDefinitionValidationError) as exc_info:
        builder.validate_definition_for_write(unsupported)

    assert exc_info.value.reason_code == "invocation_adapter_missing"
    assert exc_info.value.details == {"reason_code": "invocation_adapter_missing"}

    await catalog.aclose()


@pytest.mark.asyncio
async def test_snapshot_compiles_each_supported_handling_without_retaining_runtime_instances() -> (
    None
):
    catalog = await _catalog(
        _descriptor("invocable"),
        _descriptor("not-invocable", invocation=False),
    )
    builder = RegistrySnapshotBuilder(catalog, supported_executor_refs={"host-executor"})
    invocation = _definition(
        "invocation-agent",
        {
            "kind": "invocation",
            "adapter_key": "invocable",
            "config": {"function": "side_effect"},
        },
    )
    unsupported_invocation = _definition(
        "unsupported-invocation-agent",
        {
            "kind": "invocation",
            "adapter_key": "not-invocable",
            "config": {"function": "side_effect"},
        },
    )
    external = _definition(
        "external-agent",
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"task": "create_ticket", "priority": 10},
        },
    )
    ui_handoff = _definition(
        "ui-agent",
        {
            "kind": "ui_handoff",
            "route": "/agents/history",
            "params": {"tab": "history", "include_history": True},
        },
    )

    snapshot = builder.build(
        [invocation, unsupported_invocation, external, ui_handoff],
        source="test",
    )
    user = UserContext(id="operator-1", roles=["operator"])
    external_selection = snapshot.select_for_user("external-agent", user)
    ui_selection = snapshot.select_for_user("ui-agent", user)

    assert [agent.agent_id for agent in snapshot.candidates_for_user(user)] == [
        "external-agent",
        "invocation-agent",
        "ui-agent",
    ]
    assert snapshot.select_for_user("unsupported-invocation-agent", user) is None
    assert (
        snapshot.entry_for("unsupported-invocation-agent").isolation_reason_code
        == "invocation_adapter_unsupported"
    )
    assert external_selection is not None
    assert external_selection.binding_requirement.to_payload() == {
        "kind": "external_execution",
        "executor_ref": "host-executor",
        "params": {"task": "create_ticket", "priority": 10},
    }
    assert ui_selection is not None
    assert ui_selection.binding_requirement.to_payload() == {
        "kind": "ui_handoff",
        "route": "/agents/history",
        "params": {"tab": "history", "include_history": True},
    }
    assert not hasattr(external_selection.binding_requirement, "adapter")
    assert not hasattr(ui_selection.binding_requirement, "adapter")

    await catalog.aclose()


@pytest.mark.asyncio
async def test_snapshot_revalidates_an_internally_constructed_definition_before_it_can_bind() -> (
    None
):
    catalog = await _catalog(_descriptor("local_function"))
    builder = RegistrySnapshotBuilder(catalog)
    bypassed = _invocation_definition()
    bypassed.handling = InvocationHandling.model_construct(
        kind="invocation",
        adapter_key="local_function",
        config={"function": "Bearer deployment-secret"},
    )

    snapshot = builder.build([bypassed], source="test")

    assert snapshot.entries == {}
    assert [(entry.agent_id, entry.reason_code) for entry in snapshot.quarantined] == [
        ("summary-agent", "definition_schema_invalid")
    ]

    await catalog.aclose()
