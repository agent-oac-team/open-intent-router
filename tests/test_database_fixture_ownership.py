import asyncio

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.db.managed import ManagedDatabase
from tests.support.database import ManagedTestDatabases, _DatabaseOwnershipRegistry


def _settings(tmp_path, name: str) -> Settings:
    return Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / name}",
    )


async def test_fixture_closes_database_scope_after_a_test_failure(tmp_path) -> None:
    databases = ManagedTestDatabases()

    with pytest.raises(RuntimeError, match="test body failed"):
        try:
            factory = await databases.session_factory(_settings(tmp_path, "test-failure.db"))
            async with factory() as session:
                assert await session.scalar(text("SELECT 1")) == 1
            raise RuntimeError("test body failed")
        finally:
            await databases.aclose()


async def test_fixture_releases_setup_failure_before_reraising(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _SetupFailure:
        closed = False

        async def __aenter__(self):
            raise RuntimeError("setup failed")

        async def aclose(self) -> None:
            self.closed = True

    failing_database = _SetupFailure()
    monkeypatch.setattr(
        ManagedDatabase,
        "from_settings",
        staticmethod(lambda _settings: failing_database),
    )
    databases = ManagedTestDatabases()

    with pytest.raises(RuntimeError, match="setup failed"):
        await databases.session_factory(_settings(tmp_path, "setup-failure.db"))

    assert failing_database.closed is True
    databases._ownership.assert_clean()


async def test_fixture_releases_every_scope_when_one_teardown_fails() -> None:
    class _TeardownFailure:
        async def aclose(self) -> None:
            raise RuntimeError("dispose failed")

    class _SuccessfulDisposal:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    failing_database = _TeardownFailure()
    succeeding_database = _SuccessfulDisposal()
    databases = ManagedTestDatabases()
    databases._databases.extend((succeeding_database, failing_database))
    databases._ownership.register(succeeding_database)
    databases._ownership.register(failing_database)

    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        await databases.aclose()

    assert succeeding_database.closed is True
    databases._ownership.assert_clean()


async def test_fixture_waits_for_disposal_when_cleanup_is_cancelled() -> None:
    class _DelayedDisposal:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False

        async def aclose(self) -> None:
            self.started.set()
            await self.release.wait()
            self.closed = True

    delayed_database = _DelayedDisposal()
    databases = ManagedTestDatabases()
    databases._databases.append(delayed_database)
    databases._ownership.register(delayed_database)

    cleanup = asyncio.create_task(databases.aclose())
    await delayed_database.started.wait()
    cleanup.cancel()
    delayed_database.release.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert delayed_database.closed is True
    databases._ownership.assert_clean()


def test_controlled_leak_regression_is_detected_without_thread_inspection() -> None:
    registry = _DatabaseOwnershipRegistry()
    leaked_database = object()
    registry.register(leaked_database)

    with pytest.raises(AssertionError, match="without disposal"):
        registry.assert_clean()

    registry.release(leaked_database)
    registry.assert_clean()
