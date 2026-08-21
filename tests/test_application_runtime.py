import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.core.errors import (
    ApplicationRuntimeUnavailable,
    ApplicationShutdownError,
    ApplicationStartupError,
)
from app.core.memory_runtime import build_memory_runtime_policy
from app.db.managed import ManagedDatabase
from app.main import _minimal_databases, create_app
from app.runtime.application import ApplicationContainer, ApplicationRuntime
from app.runtime.services import build_application_service_composition
from app.services.registry_service import RegistryState


def test_lifespan_publishes_a_complete_container_from_explicit_settings(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'application-runtime.db'}",
        registry_backend="file",
        memory_mode="off",
    )
    app = create_app(settings=settings)

    assert getattr(app.state, "application_runtime_view", None) is None

    with TestClient(app) as client:
        view = app.state.application_runtime_view
        container = view.require_container()

        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").status_code == 200
        assert container.registry is not None
        assert container.services is not None
        assert container.services.router_service is not None
        assert container.services.invocation_service is not None
        assert container.services.memory_service is not None
        assert not hasattr(container, "engine")
        assert not hasattr(container, "session_factory")
        assert not hasattr(container, "settings")
        assert not hasattr(container.services, "engine")
        assert not hasattr(container.services, "session_factory")
        assert not hasattr(container.services, "settings")

    assert app.state.application_runtime_view is None
    with pytest.raises(ApplicationRuntimeUnavailable):
        view.require_container()


def test_database_targets_share_equal_specs_and_isolate_distinct_specs(tmp_path) -> None:
    shared_url = f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}"
    core_only = _minimal_databases(
        Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'core-only.db'}",
            memory_database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused-memory.db'}",
            memory_mode="off",
        )
    )
    shared = _minimal_databases(
        Settings(
            storage_backend="database",
            database_url=shared_url,
            memory_database_url=shared_url,
            memory_mode="off",
        ),
        required_targets=("core", "memory"),
    )
    isolated = _minimal_databases(
        Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
            memory_database_url=f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}",
            memory_mode="off",
        ),
        required_targets=("core", "memory"),
    )

    assert set(core_only) == {"core"}
    assert set(shared) == {"core", "memory"}
    assert shared["core"] is shared["memory"]
    assert isolated["core"] is not isolated["memory"]


def test_database_target_selection_rejects_unknown_or_missing_core_targets() -> None:
    settings = Settings(storage_backend="database", memory_mode="off")

    with pytest.raises(ValueError, match="Unknown Managed Database targets: cache"):
        _minimal_databases(settings, required_targets=("core", "cache"))
    assert set(_minimal_databases(settings, required_targets=("memory",))) == {"memory"}
    assert _minimal_databases(settings, required_targets=()) == {}


def test_full_service_composition_derives_all_selected_database_consumers() -> None:
    composition = build_application_service_composition(
        settings=Settings(storage_backend="database", memory_mode="off")
    )

    assert composition.required_database_targets == frozenset({"core", "memory"})


def test_lifespan_uses_the_explicit_settings_snapshot_without_global_dependency_reads(
    tmp_path, monkeypatch
) -> None:
    def fail_global_settings_read() -> Settings:
        raise AssertionError("the Application Runtime must not read global Settings")

    monkeypatch.setattr("app.main.get_settings", fail_global_settings_read)
    app = create_app(
        settings=Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'settings-snapshot.db'}",
            registry_backend="file",
            memory_mode="off",
        )
    )

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200


def test_custom_composition_factory_receives_the_app_settings_snapshot(tmp_path) -> None:
    source = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'composition-snapshot.db'}",
        memory_mode="off",
    )
    observed: list[Settings] = []

    def composition_factory(snapshot: Settings):
        observed.append(snapshot)
        return build_application_service_composition(settings=snapshot)

    app = create_app(settings=source, application_composition_factory=composition_factory)

    assert observed == [app.state.application_settings]
    assert observed[0] is app.state.application_settings
    assert observed[0] is not source


def test_custom_composition_does_not_resolve_default_state_rehearsal_configuration() -> None:
    called = False

    def composition_factory(_snapshot: Settings):
        nonlocal called
        called = True
        raise AssertionError("custom composition factory must not run")

    with pytest.raises(ValueError, match="owns graph configuration"):
        create_app(
            settings=Settings(memory_mode="off"),
            application_composition_factory=composition_factory,
            memory_runtime_policy=build_memory_runtime_policy(
                "off", execution_plane="state_rehearsal"
            ),
        )

    assert called is False


async def test_managed_database_owns_its_engine_for_one_explicit_scope(tmp_path) -> None:
    database = ManagedDatabase.from_settings(
        Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'managed.db'}",
            memory_mode="off",
        )
    )

    async with database:
        await database.initialize_schema()

        assert await database.probe(timeout_seconds=1)
        assert database.session_factory is not None
        assert not hasattr(database, "engine")

    with pytest.raises(RuntimeError, match="unavailable"):
        _ = database.session_factory


async def test_managed_database_postgresql_lifecycle_preflight() -> None:
    database_url = os.getenv("OIR_TEST_POSTGRESQL_URL")
    if not database_url:
        pytest.skip("OIR_TEST_POSTGRESQL_URL is required for PostgreSQL lifecycle preflight")
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    database = ManagedDatabase.from_settings(
        Settings(
            storage_backend="database",
            database_url=database_url,
            memory_mode="off",
        )
    )

    async with database:
        await database.initialize_schema()
        assert await database.probe(timeout_seconds=5)
        async with database.session_factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1

    with pytest.raises(RuntimeError, match="unavailable"):
        _ = database.session_factory


def test_each_lifespan_gets_a_fresh_runtime_and_container(tmp_path) -> None:
    app = create_app(
        settings=Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'fresh-runtime.db'}",
            registry_backend="file",
            memory_mode="off",
        )
    )

    with TestClient(app):
        first_runtime = app.state.application_runtime
        first_container = app.state.application_runtime_view.require_container()

    with TestClient(app):
        second_runtime = app.state.application_runtime
        second_container = app.state.application_runtime_view.require_container()

    assert second_runtime is not first_runtime
    assert second_container is not first_container


def test_required_startup_failure_rolls_back_and_hides_the_cause(tmp_path, monkeypatch) -> None:
    async def fail_schema(self) -> None:
        raise RuntimeError("sqlite+aiosqlite:///private/secret-marker.db")

    closes: list[ManagedDatabase] = []
    original_close = ManagedDatabase.aclose

    async def track_close(self) -> None:
        closes.append(self)
        await original_close(self)

    monkeypatch.setattr(ManagedDatabase, "initialize_schema", fail_schema)
    monkeypatch.setattr(ManagedDatabase, "aclose", track_close)
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'startup-failure.db'}"
    app = create_app(
        settings=Settings(
            storage_backend="database",
            database_url=database_url,
            memory_database_url=database_url,
            memory_mode="off",
        )
    )

    with pytest.raises(ApplicationStartupError) as error:
        with TestClient(app):
            pass

    assert str(error.value) == "application_startup_failed"
    assert "secret-marker" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert len(closes) == 1


def test_ready_reports_a_transient_managed_database_failure(tmp_path, monkeypatch) -> None:
    async def unavailable_probe(self, *, timeout_seconds: float) -> bool:
        return False

    monkeypatch.setattr(ManagedDatabase, "probe", unavailable_probe)
    app = create_app(
        settings=Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'probe-failure.db'}",
            registry_backend="file",
            memory_mode="off",
        )
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "runtime_status": "error",
        "runtime_reason": "database_unavailable",
    }


async def test_runtime_continues_reverse_cleanup_after_a_cleanup_failure() -> None:
    events: list[str] = []
    database = _DatabaseProbe(events)
    first = _BackgroundProbe("first", events, fail_stop=True)
    second = _BackgroundProbe("second", events)
    catalog = _CatalogProbe(events)
    runtime = _runtime_under_test(
        catalog=catalog,
        database=database,
        background_runtimes=(first, second),
    )

    with pytest.raises(ApplicationShutdownError) as error:
        async with runtime:
            pass

    assert error.value.failure_count == 1
    assert events == [
        "catalog.start",
        "database.enter",
        "database.initialize",
        "registry.load",
        "background.start:first",
        "background.start:second",
        "background.stop:second",
        "background.stop:first",
        "database.close",
        "catalog.stop",
    ]


async def test_cancellation_during_cleanup_still_attempts_later_resources() -> None:
    events: list[str] = []
    database = _DatabaseProbe(events, block_close=True)
    catalog = _CatalogProbe(events)
    runtime = _runtime_under_test(catalog=catalog, database=database)
    await runtime.__aenter__()

    closing = asyncio.create_task(runtime.__aexit__(None, None, None))
    await database.close_started.wait()
    closing.cancel()
    database.allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await closing

    assert events[-1] == "catalog.stop"


def _runtime_under_test(
    *,
    catalog: "_CatalogProbe",
    database: "_DatabaseProbe",
    background_runtimes: tuple[object, ...] = (),
) -> ApplicationRuntime:
    registry = _RegistryProbe(catalog.events)
    return ApplicationRuntime(
        settings=Settings(memory_mode="off", application_cleanup_timeout_seconds=1),
        runtime_catalog=catalog,
        database_factory=lambda: {"core": database},
        container_builder=lambda _catalog, _databases: ApplicationContainer(
            registry=registry,
            runtime_catalog=object(),
            registry_snapshot_runtime=None,
            _background_runtimes=background_runtimes,
        ),
    )


class _CatalogProbe:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.catalog = object()
        self.status = SimpleNamespace(status="ready", reason_code=None)
        self.health = SimpleNamespace(status="ready", reason_code=None)

    async def start(self) -> None:
        self.events.append("catalog.start")

    async def stop(self) -> None:
        self.events.append("catalog.stop")

    async def refresh_health(self):
        return self.health


class _RegistryProbe:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def load(self) -> RegistryState:
        self.events.append("registry.load")
        return RegistryState(status="ok", active_source="file")


class _DatabaseProbe:
    def __init__(self, events: list[str], *, block_close: bool = False) -> None:
        self.events = events
        self._block_close = block_close
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()

    async def __aenter__(self):
        self.events.append("database.enter")
        return self

    async def initialize_schema(self) -> None:
        self.events.append("database.initialize")

    async def probe(self, *, timeout_seconds: float) -> bool:
        return True

    async def aclose(self) -> None:
        self.events.append("database.close")
        self.close_started.set()
        if self._block_close:
            await self.allow_close.wait()


class _BackgroundProbe:
    def __init__(self, name: str, events: list[str], *, fail_stop: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail_stop = fail_stop

    async def start(self) -> None:
        self.events.append(f"background.start:{self.name}")

    async def stop(self) -> None:
        self.events.append(f"background.stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError("cleanup failure")
