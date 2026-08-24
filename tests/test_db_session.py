import pytest

from app.core.config import Settings
from app.db import managed as db_managed


@pytest.mark.parametrize(
    ("database_url", "expected_options"),
    [
        ("sqlite+aiosqlite:///:memory:", {"future": True}),
        (
            "postgresql+asyncpg://user:password@localhost/test",
            {"future": True, "pool_pre_ping": True},
        ),
    ],
)
async def test_managed_database_checks_database_connections_before_reuse(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    expected_options: dict[str, bool],
) -> None:
    calls = []

    class _Engine:
        async def dispose(self) -> None:
            return None

    engine = _Engine()

    def fake_create_async_engine(url: str, **options):
        calls.append((url, options))
        return engine

    monkeypatch.setattr(db_managed, "_create_async_engine", fake_create_async_engine)

    database = db_managed.ManagedDatabase.from_settings(
        Settings(storage_backend="database", database_url=database_url)
    )
    await database.__aenter__()
    await database.aclose()

    assert calls == [(database_url, expected_options)]
