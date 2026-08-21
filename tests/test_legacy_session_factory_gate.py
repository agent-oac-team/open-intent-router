from pathlib import Path

from scripts.check_legacy_session_factory_usage import (
    find_call_sites,
    production_call_sites,
    unexpected_call_sites,
)


def test_legacy_session_factory_call_sites_can_only_shrink() -> None:
    root = Path(__file__).resolve().parents[1]

    assert production_call_sites(root) == frozenset()
    assert unexpected_call_sites(root) == frozenset()


def test_legacy_session_factory_gate_detects_module_aliases(tmp_path) -> None:
    source = tmp_path / "app" / "sneaky_factory.py"
    source.parent.mkdir()
    source.write_text(
        (
            "import app.db.session\n"
            "import app.db.session as database\n\n"
            "app.db.session.create_session_factory(settings)\n"
            "database.create_session_factory(settings)\n"
        ),
        encoding="utf-8",
    )

    assert find_call_sites(tmp_path) == frozenset(
        {
            "app/sneaky_factory.py:4",
            "app/sneaky_factory.py:5",
        }
    )
