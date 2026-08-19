import asyncio
import time
from dataclasses import dataclass, replace

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.core.config import Settings
from app.core.errors import RuntimeCatalogUnavailableError
from app.main import create_app
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

RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass
class LifecycleProbe:
    key: str
    events: list[str]
    healthy: bool = True


class BackgroundRuntimeProbe:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


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


def _stub_background_runtimes(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "build_memory_formation_runtime",
        lambda: BackgroundRuntimeProbe(),
    )
    monkeypatch.setattr(
        main_module,
        "build_memory_maintenance_runtime",
        lambda: BackgroundRuntimeProbe(),
    )
    monkeypatch.setattr(
        main_module,
        "build_delegated_run_timeout_runtime",
        lambda: BackgroundRuntimeProbe(),
    )


def test_lifespan_constructs_catalog_once_and_surfaces_only_safe_startup_failure(
    monkeypatch,
) -> None:
    _stub_background_runtimes(monkeypatch)
    successful_events: list[str] = []
    app = create_app(runtime_descriptors=[descriptor("test", successful_events)])

    with TestClient(app) as client:
        runtime = app.state.runtime_catalog_runtime
        catalog = runtime.catalog
        assert catalog is not None
        assert catalog.get("test").key == "test"
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/health").status_code == 200
        assert successful_events == ["activate:test", "health:test"]

    assert successful_events[-1] == "dispose:test"

    failed_events: list[str] = []
    failed_app = create_app(
        runtime_descriptors=[descriptor("broken", failed_events, healthy=False)]
    )
    with TestClient(failed_app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        ready = client.get("/ready")

    assert ready.status_code == 503
    assert ready.json() == {
        "status": "error",
        "runtime_status": "error",
        "runtime_reason": "runtime_catalog_activation_failed",
    }
    assert failed_events == [
        "activate:broken",
        "health:broken",
        "dispose:broken",
    ]


def test_lifespan_turns_malformed_descriptors_into_safe_readiness_and_documents_it(
    monkeypatch,
) -> None:
    _stub_background_runtimes(monkeypatch)
    events: list[str] = []
    malformed = replace(descriptor("malformed", events), capability=None)
    app = create_app(runtime_descriptors=[malformed])

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
        "runtime_reason": "runtime_catalog_activation_failed",
    }
    assert events == []


def test_lifespan_rolls_back_catalog_when_a_later_runtime_start_fails(monkeypatch) -> None:
    catalog_events: list[str] = []
    background_events: list[str] = []
    formation_runtime = TrackedBackgroundRuntime("formation", background_events)
    maintenance_runtime = TrackedBackgroundRuntime(
        "maintenance", background_events, fail_start=True
    )
    timeout_runtime = TrackedBackgroundRuntime("timeout", background_events)

    async def create_tables(_settings) -> None:
        return None

    monkeypatch.setattr(main_module, "create_all_tables", create_tables)
    monkeypatch.setattr(
        main_module,
        "build_memory_formation_runtime",
        lambda: formation_runtime,
    )
    monkeypatch.setattr(
        main_module,
        "build_memory_maintenance_runtime",
        lambda: maintenance_runtime,
    )
    monkeypatch.setattr(
        main_module,
        "build_delegated_run_timeout_runtime",
        lambda: timeout_runtime,
    )
    app = create_app(runtime_descriptors=[descriptor("test", catalog_events)])

    with pytest.raises(RuntimeError, match="maintenance startup failed"):
        with TestClient(app):
            pass

    assert background_events == [
        "start:formation",
        "start:maintenance",
        "stop:formation",
    ]
    assert catalog_events == ["activate:test", "health:test", "dispose:test"]
