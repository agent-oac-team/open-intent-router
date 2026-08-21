from pathlib import Path

from scripts.check_database_ownership import unexpected_database_entrypoints


def test_database_entrypoints_are_limited_to_explicit_ownership_scopes() -> None:
    root = Path(__file__).resolve().parents[1]

    assert unexpected_database_entrypoints(root) == ()


def test_database_ownership_gate_rejects_hidden_engine_creation(tmp_path) -> None:
    source = tmp_path / "app" / "hidden_database.py"
    source.parent.mkdir()
    source.write_text(
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "\n"
        "def hidden_engine():\n"
        "    return create_async_engine('sqlite+aiosqlite:///:memory:')\n",
        encoding="utf-8",
    )

    assert unexpected_database_entrypoints(tmp_path) == (
        "app/hidden_database.py:1: create_async_engine import outside an ownership scope",
        "app/hidden_database.py:4: create_async_engine outside an ownership scope",
    )


def test_database_ownership_gate_rejects_aliased_engine_creation(tmp_path) -> None:
    source = tmp_path / "app" / "hidden_database.py"
    source.parent.mkdir()
    source.write_text(
        "from sqlalchemy.ext.asyncio import create_async_engine as make_engine\n"
        "\n"
        "def hidden_engine():\n"
        "    return make_engine('sqlite+aiosqlite:///:memory:')\n",
        encoding="utf-8",
    )

    assert unexpected_database_entrypoints(tmp_path) == (
        "app/hidden_database.py:1: create_async_engine import outside an ownership scope",
        "app/hidden_database.py:4: create_async_engine outside an ownership scope",
    )


def test_database_ownership_gate_rejects_engine_factory_reexports(tmp_path) -> None:
    source = tmp_path / "app" / "hidden_database.py"
    source.parent.mkdir()
    source.write_text(
        "from app.db.managed import _create_async_engine as make_engine\n"
        "\n"
        "def hidden_engine():\n"
        "    return make_engine('sqlite+aiosqlite:///:memory:')\n",
        encoding="utf-8",
    )

    assert unexpected_database_entrypoints(tmp_path) == (
        "app/hidden_database.py:1: create_async_engine import outside an ownership scope",
    )
