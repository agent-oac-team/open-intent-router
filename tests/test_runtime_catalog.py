import asyncio
import time
from dataclasses import dataclass, replace

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.core.config import Settings
from app.core.errors import ApplicationStartupError, RuntimeCatalogUnavailableError
from app.main import create_app
from app.runtime.application import ApplicationComposition
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalog,
    RuntimeCatalogActivationError,
    RuntimeCatalogRuntime,
    RuntimeCatalogValidationError,
)
from app.services.registry_snapshot import RegistrySnapshotRuntime

RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass
class LifecycleProbe:
    key: str
    events: list[str]
    healthy: bool = True


@dataclass
class TrackedBackgroundRuntime:
    key: str
    events: list[str]
    fail_start: bool = False

    async def start(self) -> None:
        self.events.append(f"start:{self.key}")
        if self.fail_start:
            raise RuntimeError(f"{self.key} startup failed")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.key}")


def descriptor(key: str, events: list[str], *, healthy: bool = True) -> RuntimeAdapterDescriptor:
    async def activate(adapter: LifecycleProbe) -> None:
        events.append(f"activate:{adapter.key}")

    async def health(adapter: LifecycleProbe) -> bool:
        events.append(f"health:{adapter.key}")
        return adapter.healthy

    async def dispose(adapter: LifecycleProbe) -> None:
        events.append(f"dispose:{adapter.key}")

    return RuntimeAdapterDescriptor(
        key=key,
        contract_version="runtime-adapter-v1",
        implementation_version="test-v1",
        config_schema={"type": "object"},
        capability=RuntimeAdapterCapability(invocation=True),
        factory=lambda _context: LifecycleProbe(key=key, events=events, healthy=healthy),
        health_check=health,
        lifecycle=RuntimeAdapterLifecycle(activate=activate, dispose=dispose),
    )


@pytest.mark.asyncio
async def test_catalog_commits_a_frozen_mapping_after_every_adapter_is_healthy() -> None:
    events: list[str] = []
    catalog = await RuntimeCatalog.activate(
        [descriptor("first", events), descriptor("second", events)],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
    )

    assert catalog.keys() == ("first", "second")
    assert catalog.get("first").key == "first"
    assert catalog.descriptor("first").key == "first"
    with pytest.raises(TypeError):
        catalog.adapters["other"] = object()

    await catalog.aclose()

    assert events == [
        "activate:first",
        "health:first",
        "activate:second",
        "health:second",
        "dispose:second",
        "dispose:first",
    ]


@pytest.mark.asyncio
async def test_catalog_descriptor_metadata_is_defensively_detached_from_nested_schema() -> None:
    events: list[str] = []
    source_schema = {
        "type": "object",
        "properties": {"function": {"const": "side_effect"}},
    }
    source = replace(descriptor("metadata", events), config_schema=source_schema)
    catalog = await RuntimeCatalog.activate(
        [source],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
    )

    returned_schema = catalog.descriptor("metadata").config_schema
    returned_schema["properties"]["function"]["const"] = "caller-change"
    source_schema["properties"]["function"]["const"] = "deployment-change"

    assert catalog.descriptor("metadata").config_schema == {
        "type": "object",
        "properties": {"function": {"const": "side_effect"}},
    }

    await catalog.aclose()


@pytest.mark.asyncio
async def test_catalog_rolls_back_all_activated_adapters_in_reverse_after_health_failure() -> None:
    events: list[str] = []

    with pytest.raises(RuntimeCatalogActivationError):
        await RuntimeCatalog.activate(
            [descriptor("first", events), descriptor("second", events, healthy=False)],
            RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
            shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
        )

    assert events == [
        "activate:first",
        "health:first",
        "activate:second",
        "health:second",
        "dispose:second",
        "dispose:first",
    ]


@pytest.mark.asyncio
async def test_catalog_rejects_duplicate_or_invalid_descriptors_before_activation() -> None:
    events: list[str] = []
    context = RuntimeAdapterContext(settings=Settings(storage_backend="memory"))

    with pytest.raises(RuntimeCatalogValidationError):
        await RuntimeCatalog.activate(
            [descriptor("duplicate", events), descriptor("duplicate", events)],
            context,
            shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
        )
    with pytest.raises(RuntimeCatalogValidationError):
        await RuntimeCatalog.activate(
            [descriptor("not valid", events)],
            context,
            shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
        )
    with pytest.raises(RuntimeCatalogValidationError):
        await RuntimeCatalog.activate(
            [
                replace(
                    descriptor("unsafe-principal", events),
                    capability=RuntimeAdapterCapability(
                        invocation=True,
                        accepted_principal_claims=frozenset({"unsupported"}),
                    ),
                )
            ],
            context,
            shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
        )
    with pytest.raises(RuntimeCatalogValidationError):
        await RuntimeCatalog.activate(
            [
                replace(
                    descriptor("credential-principal", events),
                    capability=RuntimeAdapterCapability(
                        invocation=True,
                        accepted_principal_attribute_keys=frozenset({"token"}),
                    ),
                )
            ],
            context,
            shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
        )

    assert events == []


@pytest.mark.asyncio
async def test_runtime_activates_once_when_lifespan_calls_start() -> None:
    events: list[str] = []
    factory_calls = 0

    def factory(_context) -> LifecycleProbe:
        nonlocal factory_calls
        factory_calls += 1
        return LifecycleProbe(key="single", events=events)

    source = descriptor("single", events)
    descriptor_with_counter = RuntimeAdapterDescriptor(
        key=source.key,
        contract_version=source.contract_version,
        implementation_version=source.implementation_version,
        config_schema=source.config_schema,
        capability=source.capability,
        factory=factory,
        health_check=source.health_check,
        lifecycle=source.lifecycle,
    )
    runtime = RuntimeCatalogRuntime(
        [descriptor_with_counter],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
    )

    with pytest.raises(RuntimeCatalogUnavailableError):
        await runtime.get_catalog()
    assert factory_calls == 0

    await asyncio.gather(runtime.start(), runtime.start())
    first, second = await asyncio.gather(
        runtime.get_catalog(),
        runtime.get_catalog(),
    )

    assert first is second
    assert factory_calls == 1

    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_waits_for_an_inflight_health_probe_before_disposal() -> None:
    events: list[str] = []
    health_started = asyncio.Event()
    allow_health_finish = asyncio.Event()
    health_calls = 0
    source = descriptor("gated", events)

    async def health(adapter: LifecycleProbe) -> bool:
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            return True
        events.append("health:start")
        health_started.set()
        await allow_health_finish.wait()
        events.append("health:end")
        return True

    async def dispose(adapter: LifecycleProbe) -> None:
        events.append("dispose")

    runtime = RuntimeCatalogRuntime(
        [
            replace(
                source,
                health_check=health,
                lifecycle=RuntimeAdapterLifecycle(
                    activate=source.lifecycle.activate,
                    dispose=dispose,
                ),
            )
        ],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
    )
    await runtime.start()

    refresh = asyncio.create_task(runtime.refresh_health())
    await asyncio.wait_for(health_started.wait(), timeout=1)
    stop = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)

    assert "dispose" not in events
    allow_health_finish.set()
    await asyncio.gather(refresh, stop)

    assert events.index("health:end") < events.index("dispose")


@pytest.mark.asyncio
async def test_catalog_bounds_slow_disposal() -> None:
    events: list[str] = []
    source = descriptor("slow", events)

    async def slow_dispose(adapter: LifecycleProbe) -> None:
        events.append(f"dispose:{adapter.key}:start")
        await asyncio.Event().wait()

    slow_descriptor = replace(
        source,
        lifecycle=RuntimeAdapterLifecycle(
            activate=source.lifecycle.activate,
            dispose=slow_dispose,
        ),
    )
    catalog = await RuntimeCatalog.activate(
        [slow_descriptor],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=0.01,
    )

    started = time.monotonic()
    await catalog.aclose()

    assert time.monotonic() - started < 0.2
    assert events[-1] == "dispose:slow:start"


@pytest.mark.asyncio
async def test_catalog_bounds_slow_rollback_disposal() -> None:
    events: list[str] = []
    source = descriptor("slow", events)

    async def slow_dispose(adapter: LifecycleProbe) -> None:
        events.append(f"dispose:{adapter.key}:start")
        await asyncio.Event().wait()

    slow_descriptor = replace(
        source,
        lifecycle=RuntimeAdapterLifecycle(
            activate=source.lifecycle.activate,
            dispose=slow_dispose,
        ),
    )

    started = time.monotonic()
    with pytest.raises(RuntimeCatalogActivationError):
        await RuntimeCatalog.activate(
            [slow_descriptor, descriptor("broken", events, healthy=False)],
            RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
            shutdown_timeout_seconds=0.01,
        )

    assert time.monotonic() - started < 0.2
    assert events == [
        "activate:slow",
        "health:slow",
        "activate:broken",
        "health:broken",
        "dispose:broken",
        "dispose:slow:start",
    ]


@pytest.mark.asyncio
async def test_catalog_rejects_synchronous_lifecycle_hooks_before_activation() -> None:
    events: list[str] = []
    source = descriptor("sync", events)
    synchronous_lifecycle = replace(
        source,
        lifecycle=RuntimeAdapterLifecycle(
            activate=source.lifecycle.activate,
            dispose=lambda _adapter: None,
        ),
    )

    with pytest.raises(RuntimeCatalogValidationError):
        await RuntimeCatalog.activate(
            [synchronous_lifecycle],
            RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
            shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
        )

    assert events == []


@pytest.mark.asyncio
async def test_catalog_rejects_synchronous_health_checks_before_activation() -> None:
    events: list[str] = []
    source = descriptor("sync-health", events)
    synchronous_health = replace(source, health_check=lambda _adapter: True)

    with pytest.raises(RuntimeCatalogValidationError):
        await RuntimeCatalog.activate(
            [synchronous_health],
            RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
            shutdown_timeout_seconds=RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS,
        )

    assert events == []


def test_lifespan_constructs_catalog_once_and_surfaces_only_safe_startup_failure() -> None:
    successful_events: list[str] = []
    app = create_app(
        settings=Settings(storage_backend="memory", registry_backend="file"),
        runtime_descriptors=[descriptor("test", successful_events)],
    )

    with TestClient(app) as client:
        runtime = app.state.runtime_catalog_runtime
        catalog = runtime.catalog
        assert catalog is not None
        assert catalog.get("test").key == "test"
        snapshot_runtime = app.state.registry_snapshot_runtime
        assert isinstance(snapshot_runtime, RegistrySnapshotRuntime)
        assert snapshot_runtime.snapshot is not None
        assert snapshot_runtime.snapshot.source == "native_registry"
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/health").status_code == 200
        assert successful_events == ["activate:test", "health:test"]

    assert successful_events[-1] == "dispose:test"
    assert app.state.registry_snapshot_runtime is None

    failed_events: list[str] = []
    failed_app = create_app(
        settings=Settings(storage_backend="memory", registry_backend="file"),
        runtime_descriptors=[descriptor("broken", failed_events, healthy=False)],
    )
    with TestClient(failed_app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        ready = client.get("/ready")

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "degraded",
        "registry_status": "ok",
        "active_source": "file",
        "message": None,
        "runtime_status": "degraded",
        "reason_code": "runtime_adapter_unhealthy",
        "impacted_definition_count": 0,
    }
    assert failed_events == [
        "activate:broken",
        "health:broken",
        "health:broken",
        "dispose:broken",
    ]


def test_lifespan_turns_malformed_descriptors_into_safe_readiness_and_documents_it() -> None:
    events: list[str] = []
    malformed = replace(descriptor("malformed", events), capability=None)
    app = create_app(
        settings=Settings(storage_backend="memory", registry_backend="file"),
        runtime_descriptors=[malformed],
    )

    ready_operation = app.openapi()["paths"]["/ready"]["get"]
    assert ready_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse"
    }
    assert ready_operation["responses"]["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RuntimeCatalogReadinessErrorResponse"
    }

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        ready = client.get("/ready")

    assert ready.status_code == 503
    assert ready.json() == {
        "status": "error",
        "runtime_status": "error",
        "runtime_reason": "runtime_catalog_unavailable",
    }
    assert events == []


def test_lifespan_rolls_back_catalog_when_a_later_runtime_start_fails() -> None:
    catalog_events: list[str] = []
    background_events: list[str] = []
    formation_runtime = TrackedBackgroundRuntime("formation", background_events)
    maintenance_runtime = TrackedBackgroundRuntime(
        "maintenance", background_events, fail_start=True
    )
    timeout_runtime = TrackedBackgroundRuntime("timeout", background_events)

    settings = Settings(storage_backend="memory", registry_backend="file")

    def container_builder(catalog, databases):
        container = main_module._build_minimal_container(
            settings=settings,
            catalog=catalog,
            databases=databases,
            external_executor=None,
        )
        return replace(
            container,
            _background_runtimes=(formation_runtime, maintenance_runtime, timeout_runtime),
        )

    app = create_app(
        settings=settings,
        runtime_descriptors=[descriptor("test", catalog_events)],
        application_composition_factory=lambda _settings: ApplicationComposition(
            container_builder=container_builder,
            required_database_targets=frozenset(),
        ),
    )

    with pytest.raises(ApplicationStartupError, match="application_startup_failed"):
        with TestClient(app):
            pass

    assert background_events == [
        "start:formation",
        "start:maintenance",
        "stop:maintenance",
        "stop:formation",
    ]
    assert catalog_events == ["activate:test", "health:test", "dispose:test"]
