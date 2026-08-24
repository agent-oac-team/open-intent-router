import asyncio
import logging
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.application import RegistrySnapshotQuarantineInput, RegistrySnapshotSourceInput
from app.core.config import Settings
from app.core.errors import ApplicationStartupError, RegistryUnavailableError
from app.db.managed import ManagedDatabase
from app.main import create_app
from app.runtime.application import ApplicationComposition, ApplicationContainer
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalogRuntime,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.common import UserContext
from app.services.registry_service import RegistryState
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime
from app.services.runtime_readiness import RuntimeReadinessRuntime
from app.services.snapshot_routing_service import SnapshotRoutingService


@dataclass
class HealthProbe:
    healthy: bool = True
    calls: int = 0


def _descriptor(key: str, probe: HealthProbe) -> RuntimeAdapterDescriptor:
    async def activate(_adapter: HealthProbe) -> None:
        return None

    async def dispose(_adapter: HealthProbe) -> None:
        return None

    async def health(adapter: HealthProbe) -> bool:
        adapter.calls += 1
        return adapter.healthy

    return RuntimeAdapterDescriptor(
        key=key,
        contract_version="oir-runtime-adapter-v1",
        implementation_version="test-v1",
        config_schema={"type": "object"},
        capability=RuntimeAdapterCapability(invocation=True, v2_invocation=True),
        factory=lambda _context: probe,
        health_check=health,
        lifecycle=RuntimeAdapterLifecycle(activate=activate, dispose=dispose),
    )


def _definition(*, revision: int = 1, connector_ref: str | None = None) -> AgentDefinitionV2:
    handling: dict[str, object] = {
        "kind": "invocation",
        "adapter_key": "optional_adapter",
        "config": {"function": "summarize", "max_tokens": 128},
    }
    if connector_ref is not None:
        handling["connector_ref"] = connector_ref
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "summary-agent",
            "name": "Summary Agent",
            "description": "Creates a short summary.",
            "revision": revision,
            "handling": handling,
        }
    )


def _ui_definition(*, revision: int = 1) -> AgentDefinitionV2:
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "summary-agent",
            "name": "Summary Agent",
            "description": "Creates a short summary.",
            "revision": revision,
            "handling": {"kind": "ui_handoff", "route": "/summary"},
        }
    )


def _container_composition_factory(registry):
    def build(catalog, _databases) -> ApplicationContainer:
        return ApplicationContainer(
            registry=registry,
            runtime_catalog=catalog,
            registry_snapshot_runtime=RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog)),
        )

    return lambda _settings: ApplicationComposition(
        container_builder=build,
        required_database_targets=frozenset(),
    )


async def _runtime(
    probe: HealthProbe,
    *,
    required_adapter_keys: set[str] | None = None,
) -> RuntimeCatalogRuntime:
    runtime = RuntimeCatalogRuntime(
        [_descriptor("optional_adapter", probe)],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
        required_adapter_keys=required_adapter_keys or set(),
    )
    await runtime.start()
    return runtime


@pytest.mark.asyncio
async def test_optional_adapter_health_degrades_only_dependent_snapshot_entries() -> None:
    probe = HealthProbe()
    catalog_runtime = await _runtime(probe)
    catalog = catalog_runtime.catalog
    assert catalog is not None
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="primary")
    readiness = RuntimeReadinessRuntime(catalog_runtime)
    readiness.attach_snapshot_runtime(snapshot_runtime)
    readiness.record_primary_registry_state(RegistryState(status="ok", active_source="file"))

    initial = await readiness.refresh()
    assert initial.status == "ok"
    assert snapshot_runtime.snapshot is not None
    assert snapshot_runtime.snapshot.entry_for("summary-agent").binding_status == "ready"

    probe.healthy = False
    degraded = await readiness.refresh()

    assert degraded.status == "degraded"
    assert degraded.runtime_status == "degraded"
    assert degraded.reason_code == "runtime_adapter_unhealthy"
    assert degraded.impacted_definition_count == 1
    assert snapshot_runtime.snapshot is not None
    entry = snapshot_runtime.snapshot.entry_for("summary-agent")
    assert entry is not None
    assert entry.binding_status == "isolated"
    assert entry.isolation_reason_code == "invocation_adapter_unhealthy"

    probe.healthy = True
    recovered = await readiness.refresh()
    assert recovered.status == "ok"
    assert snapshot_runtime.snapshot is not None
    assert snapshot_runtime.snapshot.entry_for("summary-agent").binding_status == "ready"

    await catalog_runtime.stop()


@pytest.mark.asyncio
async def test_required_adapter_health_failure_makes_runtime_unready() -> None:
    probe = HealthProbe()
    catalog_runtime = await _runtime(probe, required_adapter_keys={"optional_adapter"})
    readiness = RuntimeReadinessRuntime(catalog_runtime)

    probe.healthy = False
    report = await readiness.refresh()

    assert report.status == "error"
    assert report.runtime_status == "error"
    assert report.reason_code == "runtime_catalog_unavailable"

    await catalog_runtime.stop()


@pytest.mark.asyncio
async def test_missing_required_adapter_makes_runtime_unready_without_exposing_its_key() -> None:
    catalog_runtime = await _runtime(HealthProbe(), required_adapter_keys={"missing_adapter"})
    readiness = RuntimeReadinessRuntime(catalog_runtime)
    readiness.record_primary_registry_state(RegistryState(status="ok", active_source="file"))

    report = await readiness.refresh()

    assert report.status == "error"
    assert report.reason_code == "runtime_catalog_unavailable"
    assert "missing_adapter" not in str(report)

    await catalog_runtime.stop()


@pytest.mark.asyncio
async def test_static_snapshot_isolation_does_not_change_process_readiness() -> None:
    catalog_runtime = await _runtime(HealthProbe())
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(None))
    snapshot_runtime.load([_definition()], source="primary")
    readiness = RuntimeReadinessRuntime(catalog_runtime)
    readiness.attach_snapshot_runtime(snapshot_runtime)
    readiness.record_primary_registry_state(RegistryState(status="ok", active_source="file"))

    report = readiness.report()

    assert report.status == "ok"
    assert report.runtime_status == "ready"
    assert report.reason_code is None
    assert report.impacted_definition_count == 0
    await catalog_runtime.stop()


@pytest.mark.asyncio
async def test_fresh_snapshot_routing_applies_current_adapter_health_overlay() -> None:
    probe = HealthProbe()
    catalog_runtime = await _runtime(probe)
    catalog = catalog_runtime.catalog
    assert catalog is not None
    routing = SnapshotRoutingService(
        router_factory=lambda _runtime: None,  # type: ignore[arg-type]
        runtime_catalog=catalog,
        adapter_health_provider=lambda: {"optional_adapter"},
    )

    snapshot_runtime = routing._build_snapshot_runtime([_definition()], source="host")

    entry = snapshot_runtime.snapshot.entry_for("summary-agent")  # type: ignore[union-attr]
    assert entry.binding_status == "isolated"
    assert entry.isolation_reason_code == "invocation_adapter_unhealthy"
    await catalog_runtime.stop()


def test_snapshot_reload_keeps_last_known_good_and_commits_replacement_atomically() -> None:
    runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(None))
    initial = runtime.load([_ui_definition()], source="primary")

    with pytest.raises(ValueError, match="source"):
        runtime.load([_ui_definition(revision=2)], source="")

    assert runtime.snapshot is initial
    assert runtime.status.status == "degraded"
    assert runtime.status.reason_code == "registry_snapshot_reload_failed"

    replacement = runtime.load([_ui_definition(revision=2)], source="primary")

    assert replacement is runtime.snapshot
    assert replacement is not initial
    assert replacement.entry_for("summary-agent").revision == 2
    assert runtime.status.status == "ready"
    assert runtime.status.reason_code is None


def test_snapshot_admin_inventory_redacts_all_handling_references() -> None:
    runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(None))
    runtime.load([_definition(connector_ref="connector-private")], source="primary")

    [entry] = runtime.admin_inventory()

    assert entry.agent_id == "summary-agent"
    assert entry.handling == {
        "kind": "invocation",
        "adapter_key": "***REDACTED***",
        "connector_ref": "***REDACTED***",
        "config": {"function": "***REDACTED***", "max_tokens": 128},
    }
    assert "connector-private" not in str(entry)
    assert "optional_adapter" not in str(entry)


def test_public_snapshot_catalog_has_no_binding_or_admin_diagnostics() -> None:
    runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(None))
    runtime.load([_ui_definition()], source="primary")

    [public] = runtime.public_definitions(UserContext(id="operator"))
    payload = public.model_dump()

    assert payload["handling_kind"] == "ui_handoff"
    assert {"handling", "binding_status", "isolation_reason_code"}.isdisjoint(payload)
    assert "/summary" not in str(payload)


def test_health_is_probe_free_and_ready_inventory_are_safe() -> None:
    class RegistryProbe:
        calls = 0

        async def load(self) -> RegistryState:
            self.calls += 1
            return RegistryState(status="ok", active_source="file")

    registry = RegistryProbe()
    probe = HealthProbe()

    def snapshot_mapper(_state: RegistryState) -> RegistrySnapshotSourceInput:
        return RegistrySnapshotSourceInput(
            source="primary",
            definitions=[
                _definition(connector_ref="connector-private"),
                _ui_definition().model_copy(
                    update={
                        "agent_id": "https://private.example/agent?token=secret-marker",
                    }
                ),
                RegistrySnapshotQuarantineInput(
                    agent_id="https://private.example/agent?token=other-secret-marker",
                    reason_code="legacy_definition_unmappable",
                ),
            ],
        )

    app = create_app(
        runtime_descriptors=[_descriptor("optional_adapter", probe)],
        registry_snapshot_mapper=snapshot_mapper,
        application_composition_factory=_container_composition_factory(registry),
    )

    with TestClient(app) as client:
        snapshot_runtime = app.state.registry_snapshot_runtime
        assert isinstance(snapshot_runtime, RegistrySnapshotRuntime)
        assert snapshot_runtime.snapshot is not None
        assert snapshot_runtime.snapshot.entry_for("summary-agent") is not None
        calls_before_liveness = probe.calls
        registry_calls_before_liveness = registry.calls

        liveness = client.get("/health")

        assert liveness.status_code == 200
        assert liveness.json() == {"status": "ok"}
        assert probe.calls == calls_before_liveness
        assert registry.calls == registry_calls_before_liveness

        probe.healthy = False
        readiness = client.get("/ready")
        inventory = client.get("/api/v1/admin/runtime/inventory")

    assert readiness.status_code == 200
    assert readiness.json()["status"] == "degraded"
    assert readiness.json()["runtime_status"] == "degraded"
    assert readiness.json()["reason_code"] == "runtime_adapter_unhealthy"
    assert readiness.json()["impacted_definition_count"] == 1
    assert registry.calls == registry_calls_before_liveness
    assert inventory.status_code == 200
    body = inventory.json()
    assert body["status"] == "degraded"
    assert body["definitions"] == [
        {
            "agent_id": "summary-agent",
            "revision": 1,
            "enabled": True,
            "handling_kind": "invocation",
            "handling": {
                "kind": "invocation",
                "adapter_key": "***REDACTED***",
                "connector_ref": "***REDACTED***",
                "config": {"function": "***REDACTED***", "max_tokens": 128},
            },
            "binding_status": "isolated",
            "isolation_reason_code": "invocation_adapter_unhealthy",
        }
    ]
    assert body["quarantined_definition_count"] == 2
    assert body["quarantined_definitions"] == [
        {
            "source_index": 1,
            "agent_id": None,
            "reason_code": "definition_schema_invalid",
        },
        {
            "source_index": 2,
            "agent_id": None,
            "reason_code": "legacy_definition_unmappable",
        },
    ]
    serialized = str(body)
    assert "connector-private" not in serialized
    assert "optional_adapter" not in serialized
    assert "secret-marker" not in serialized
    assert "other-secret-marker" not in serialized


def test_primary_registry_and_core_startup_failures_abort_with_safe_errors(monkeypatch) -> None:
    class BrokenRegistry:
        async def load(self):
            raise RuntimeError("postgresql://user:password@private.example/registry")

    registry_app = create_app(
        runtime_descriptors=[_descriptor("optional_adapter", HealthProbe())],
        application_composition_factory=_container_composition_factory(BrokenRegistry()),
    )
    with pytest.raises(ApplicationStartupError) as registry_error:
        with TestClient(registry_app):
            pass

    assert str(registry_error.value) == "application_startup_failed"

    async def broken_schema(self) -> None:
        raise RuntimeError("database password=private")

    monkeypatch.setattr(ManagedDatabase, "initialize_schema", broken_schema)
    core_app = create_app(
        settings=Settings(
            storage_backend="database",
            database_url="sqlite+aiosqlite:///:memory:",
            registry_backend="file",
            memory_mode="off",
        ),
        runtime_descriptors=[_descriptor("optional_adapter", HealthProbe())],
    )
    with pytest.raises(ApplicationStartupError) as core_error:
        with TestClient(core_app):
            pass

    assert str(core_error.value) == "application_startup_failed"


def test_startup_failure_logs_never_include_secret_markers(monkeypatch, caplog) -> None:
    marker = "postgresql://operator:secret-marker@private.example/runtime"

    class BrokenRegistry:
        async def load(self):
            raise RuntimeError(marker)

    registry_app = create_app(
        runtime_descriptors=[_descriptor("optional_adapter", HealthProbe())],
        application_composition_factory=_container_composition_factory(BrokenRegistry()),
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ApplicationStartupError):
            with TestClient(registry_app):
                pass

    assert "primary_registry_initialization_failed" in caplog.text
    assert marker not in caplog.text

    caplog.clear()

    async def broken_schema(self) -> None:
        raise RuntimeError(marker)

    monkeypatch.setattr(ManagedDatabase, "initialize_schema", broken_schema)
    core_app = create_app(
        settings=Settings(
            storage_backend="database",
            database_url="sqlite+aiosqlite:///:memory:",
            registry_backend="file",
            memory_mode="off",
        ),
        runtime_descriptors=[_descriptor("optional_adapter", HealthProbe())],
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ApplicationStartupError):
            with TestClient(core_app):
                pass

    assert marker not in caplog.text


def test_required_adapter_policy_is_applied_during_lifespan() -> None:
    app = create_app(
        settings=Settings(
            storage_backend="memory",
            registry_backend="file",
            runtime_required_adapter_keys="optional_adapter",
        ),
        runtime_descriptors=[_descriptor("optional_adapter", HealthProbe(healthy=False))],
    )

    with TestClient(app) as client:
        readiness = client.get("/ready")

    assert readiness.status_code == 503
    assert readiness.json() == {
        "status": "error",
        "runtime_status": "error",
        "runtime_reason": "runtime_catalog_unavailable",
    }


def test_failed_admin_registry_reload_updates_cached_readiness() -> None:
    class ReloadingRegistry:
        async def load(self) -> RegistryState:
            return RegistryState(status="ok", active_source="file")

        async def reload(self) -> RegistryState:
            raise RegistryUnavailableError("Registry unavailable")

    registry = ReloadingRegistry()
    app = create_app(
        runtime_descriptors=[_descriptor("optional_adapter", HealthProbe())],
        application_composition_factory=_container_composition_factory(registry),
    )

    with TestClient(app) as client:
        reload_response = client.post("/api/v1/admin/registry/reload")
        readiness = client.get("/ready")

    assert reload_response.status_code == 503
    assert readiness.status_code == 503
    assert readiness.json() == {
        "status": "error",
        "runtime_status": "error",
        "runtime_reason": "primary_registry_unavailable",
    }


def test_admin_registry_reload_replaces_the_composed_live_snapshot() -> None:
    class ReloadingRegistry:
        revision = 1

        def _state(self) -> RegistryState:
            return RegistryState(
                status="ok",
                active_source="file",
                agents=[_ui_definition(revision=self.revision)],
            )

        async def load(self) -> RegistryState:
            return self._state()

        async def reload(self) -> RegistryState:
            self.revision = 2
            return self._state()

    def snapshot_mapper(state: RegistryState) -> RegistrySnapshotSourceInput:
        return RegistrySnapshotSourceInput(
            source="host_registry",
            definitions=state.agents,
        )

    registry = ReloadingRegistry()
    app = create_app(
        runtime_descriptors=[_descriptor("optional_adapter", HealthProbe())],
        registry_snapshot_mapper=snapshot_mapper,
        application_composition_factory=_container_composition_factory(registry),
    )

    with TestClient(app) as client:
        initial_inventory = client.get("/api/v1/admin/runtime/inventory")
        reloaded = client.post("/api/v1/admin/registry/reload")
        replacement_inventory = client.get("/api/v1/admin/runtime/inventory")

    assert initial_inventory.status_code == 200
    assert initial_inventory.json()["definitions"][0]["revision"] == 1
    assert reloaded.status_code == 200
    assert replacement_inventory.status_code == 200
    assert replacement_inventory.json()["definitions"][0]["revision"] == 2


def test_native_registry_crud_refreshes_the_process_snapshot_after_each_write() -> None:
    class MutableRegistry:
        def __init__(self) -> None:
            self.agents = {}
            self.load_calls = 0

        async def load(self) -> RegistryState:
            self.load_calls += 1
            return RegistryState(
                status="ok", active_source="file", agents=list(self.agents.values())
            )

        async def upsert_definition(self, definition):
            current = self.agents.get(definition.agent_id)
            revision = current.revision + 1 if current is not None else 1
            updated = definition.model_copy(update={"revision": revision})
            self.agents[updated.agent_id] = updated
            return updated

        async def set_enabled(self, agent_id: str, enabled: bool):
            current = self.agents.get(agent_id)
            if current is None:
                return None
            updated = current.model_copy(
                update={"enabled": enabled, "revision": current.revision + 1}
            )
            self.agents[agent_id] = updated
            return updated

        async def delete_definition(self, agent_id: str) -> bool:
            return self.agents.pop(agent_id, None) is not None

    registry = MutableRegistry()
    app = create_app(
        runtime_descriptors=[_descriptor("optional_adapter", HealthProbe())],
        application_composition_factory=_container_composition_factory(registry),
    )
    payload = {
        "schema_version": "oir-agent-v2",
        "agent_id": "native-agent",
        "name": "Native Agent",
        "description": "keeps the runtime snapshot current",
        "handling": {"kind": "invocation", "adapter_key": "optional_adapter"},
    }

    with TestClient(app) as client:
        assert client.post("/api/v1/admin/agents", json=payload).status_code == 200
        assert client.put("/api/v1/admin/agents/native-agent", json=payload).status_code == 200
        assert (
            client.patch(
                "/api/v1/admin/agents/native-agent/enabled",
                json={"enabled": False},
            ).status_code
            == 200
        )
        assert client.delete("/api/v1/admin/agents/native-agent").status_code == 200

    # Lifespan initialization reads once; every committed CRUD mutation rereads
    # the trusted source under the process Snapshot refresh fence.
    assert registry.load_calls == 5


@pytest.mark.asyncio
async def test_snapshot_refresh_fence_keeps_the_latest_committed_source_state() -> None:
    """A delayed source projection cannot overwrite a later committed Registry view."""

    catalog_runtime = await _runtime(HealthProbe())
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(None))
    first_projection_started = asyncio.Event()
    release_first_projection = asyncio.Event()
    projected_revisions: list[int] = []

    class RegistrySource:
        revision = 1
        load_calls = 0

        async def load(self) -> RegistryState:
            self.load_calls += 1
            return RegistryState(
                status="ok",
                active_source="file",
                agents=[_ui_definition(revision=self.revision)],
            )

    async def snapshot_mapper(state: RegistryState) -> RegistrySnapshotSourceInput:
        revision = state.agents[0].revision
        projected_revisions.append(revision)
        if revision == 1:
            first_projection_started.set()
            await release_first_projection.wait()
        return RegistrySnapshotSourceInput(source="primary", definitions=state.agents)

    readiness = RuntimeReadinessRuntime(catalog_runtime, snapshot_mapper=snapshot_mapper)
    readiness.attach_snapshot_runtime(snapshot_runtime)
    registry = RegistrySource()

    first = asyncio.create_task(readiness.refresh_registry_snapshot(registry))
    await first_projection_started.wait()
    registry.revision = 2
    second = asyncio.create_task(readiness.refresh_registry_snapshot(registry))
    await asyncio.sleep(0)
    assert registry.load_calls == 1

    release_first_projection.set()
    assert await first is True
    assert await second is True

    assert projected_revisions == [1, 2]
    assert snapshot_runtime.snapshot is not None
    assert snapshot_runtime.snapshot.entry_for("summary-agent").revision == 2
    await catalog_runtime.stop()
