from types import SimpleNamespace

import pytest

from app.db import session as db_session


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
def test_create_engine_checks_database_connections_before_reuse(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    expected_options: dict[str, bool],
) -> None:
    calls = []
    engine = object()

    def fake_create_async_engine(url: str, **options):
        calls.append((url, options))
        return engine

    monkeypatch.setattr(db_session, "create_async_engine", fake_create_async_engine)

    actual = db_session.create_engine(SimpleNamespace(database_url=database_url))

    assert actual is engine
    assert calls == [(database_url, expected_options)]
