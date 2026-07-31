import json
import sqlite3
from pathlib import Path

import pytest

import scripts.cleanup_oir_knowledge as cleanup_module
from scripts.cleanup_oir_knowledge import (
    KNOWLEDGE_TABLES,
    CleanupBlocked,
    cleanup_oir_knowledge,
)


def _database(path: Path, *, chunk_count: int = 269) -> None:
    connection = sqlite3.connect(path)
    for table in KNOWLEDGE_TABLES:
        connection.execute(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY, payload TEXT)')
    for index in range(chunk_count):
        connection.execute(
            "INSERT INTO knowledge_asset_chunks VALUES (?, ?)",
            (f"chunk-{index}", f"secret body {index}"),
        )
    connection.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY, content TEXT)")
    connection.execute("INSERT INTO memory_items VALUES ('memory-1', 'keep me')")
    connection.execute("CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY, input_text TEXT)")
    connection.execute(
        "INSERT INTO agent_runs VALUES (?, ?)",
        (
            "run-1",
            json.dumps(
                {
                    "knowledge_context": {
                        "status": "ok",
                        "trace_id": "trace-1",
                        "summary": "secret body",
                        "items": [
                            {
                                "item_id": "item-1",
                                "source_id": "source-1",
                                "content": "secret body",
                            }
                        ],
                        "citations": [{"source_id": "source-1", "title": "secret"}],
                    }
                }
            ),
        ),
    )
    connection.commit()
    connection.close()


def _vectors(path: Path, *, collection: str = "oir_knowledge_vectors") -> None:
    collection_path = path / "collections" / collection
    collection_path.mkdir(parents=True)
    (collection_path / "manifest.json").write_text('{"row_count":269}', encoding="utf-8")


def test_dry_run_manifest_is_body_free_and_does_not_mutate(tmp_path) -> None:
    database = tmp_path / "oir.db"
    vectors = tmp_path / "oir_knowledge_milvus.db"
    _database(database)
    _vectors(vectors)

    report = cleanup_oir_knowledge(database=database, knowledge_vectors=vectors, confirm=False)

    assert report["status"] == "dry_run_passed"
    assert report["reconciliation"] == {
        "oir_chunk_count": 269,
        "knowledge_sys_chunk_count": 270,
        "difference": 1,
        "disposition": "no_migration_knowledge_sys_is_canonical",
    }
    assert set(report["manifest"]["tables"]) == set(KNOWLEDGE_TABLES)
    assert report["manifest"]["tables"]["knowledge_asset_chunks"]["row_count"] == 269
    serialized = json.dumps(report)
    assert "secret body" not in serialized
    assert vectors.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_asset_chunks").fetchone() == (
            269,
        )


def test_unknown_table_collection_or_json_shape_blocks_without_deleting(tmp_path) -> None:
    for kind in ("table", "collection", "json"):
        database = tmp_path / f"{kind}.db"
        vectors = tmp_path / f"{kind}" / "oir_knowledge_milvus.db"
        _database(database)
        _vectors(vectors)
        with sqlite3.connect(database) as connection:
            if kind == "table":
                connection.execute("CREATE TABLE knowledge_surprise (id TEXT)")
            elif kind == "json":
                connection.execute(
                    "UPDATE agent_runs SET input_text=?",
                    (json.dumps({"knowledge_context": ["unknown"]}),),
                )
            connection.commit()
        if kind == "collection":
            _vectors(vectors, collection="unknown_vectors")

        with pytest.raises(CleanupBlocked):
            cleanup_oir_knowledge(database=database, knowledge_vectors=vectors, confirm=True)

        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM knowledge_asset_chunks").fetchone() == (
                269,
            )
        assert vectors.exists()


def test_confirm_drops_only_knowledge_and_sanitizes_history(tmp_path) -> None:
    database = tmp_path / "oir.db"
    vectors = tmp_path / "oir_knowledge_milvus.db"
    memory_vectors = tmp_path / "oir_memory_milvus.db"
    _database(database)
    _vectors(vectors)
    _vectors(memory_vectors, collection="oir_memory_vectors")
    memory_before = (memory_vectors / "collections/oir_memory_vectors/manifest.json").read_bytes()

    report = cleanup_oir_knowledge(
        database=database,
        knowledge_vectors=vectors,
        memory_vectors=memory_vectors,
        confirm=True,
    )

    assert report["status"] == "executed"
    assert report["memory_unchanged"] is True
    assert not vectors.exists()
    assert memory_vectors.exists()
    assert (
        memory_vectors / "collections/oir_memory_vectors/manifest.json"
    ).read_bytes() == memory_before
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not set(KNOWLEDGE_TABLES) & tables
        assert "memory_items" in tables
        value = json.loads(
            connection.execute("SELECT input_text FROM agent_runs WHERE run_id='run-1'").fetchone()[
                0
            ]
        )
    context = value["knowledge_context"]
    assert context["trace_id"] == "trace-1"
    assert context["items"] == [{"item_id": "item-1", "source_id": "source-1"}]
    assert context["citations"] == [{"source_id": "source-1"}]
    assert "summary" not in context
    assert "content" not in json.dumps(context)


def test_unexplained_chunk_count_blocks_confirmation(tmp_path) -> None:
    database = tmp_path / "oir.db"
    vectors = tmp_path / "oir_knowledge_milvus.db"
    _database(database, chunk_count=268)
    _vectors(vectors)

    with pytest.raises(CleanupBlocked, match="expected 269"):
        cleanup_oir_knowledge(database=database, knowledge_vectors=vectors, confirm=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_asset_chunks").fetchone() == (
            268,
        )


def test_unknown_text_body_location_blocks_without_deleting(tmp_path) -> None:
    database = tmp_path / "oir.db"
    vectors = tmp_path / "oir_knowledge_milvus.db"
    _database(database)
    _vectors(vectors)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE audit_notes (id TEXT, note TEXT)")
        connection.execute(
            "INSERT INTO audit_notes VALUES (?, ?)",
            ("note-1", json.dumps({"knowledge_context": {"summary": "secret body"}})),
        )
        connection.commit()

    with pytest.raises(CleanupBlocked, match=r"Unknown Knowledge body location: audit_notes\.note"):
        cleanup_oir_knowledge(database=database, knowledge_vectors=vectors, confirm=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_asset_chunks").fetchone() == (
            269,
        )
    assert vectors.exists()


def test_vector_quarantine_failure_rolls_back_database_deletion(tmp_path, monkeypatch) -> None:
    database = tmp_path / "oir.db"
    vectors = tmp_path / "oir_knowledge_milvus.db"
    _database(database)
    _vectors(vectors)
    original_rename = Path.rename

    def fail_target_rename(path: Path, target: Path) -> Path:
        if path == vectors:
            raise OSError("injected rename failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_target_rename)

    with pytest.raises(CleanupBlocked, match="Could not quarantine"):
        cleanup_oir_knowledge(database=database, knowledge_vectors=vectors, confirm=True)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(KNOWLEDGE_TABLES) <= tables
        value = json.loads(
            connection.execute("SELECT input_text FROM agent_runs WHERE run_id='run-1'").fetchone()[
                0
            ]
        )
        assert value["knowledge_context"]["summary"] == "secret body"
    assert vectors.exists()


def test_post_commit_quarantine_delete_failure_is_reported_as_recoverable(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "oir.db"
    vectors = tmp_path / "oir_knowledge_milvus.db"
    _database(database)
    _vectors(vectors)

    def fail_delete(path: Path) -> None:
        raise OSError("injected delete failure")

    monkeypatch.setattr(cleanup_module.shutil, "rmtree", fail_delete)

    report = cleanup_oir_knowledge(
        database=database,
        knowledge_vectors=vectors,
        confirm=True,
    )

    assert report["status"] == "executed_with_quarantine_cleanup_pending"
    assert report["deleted_vector_path"] is None
    quarantine_name = report["recoverable_vector_quarantine"]
    assert quarantine_name
    assert (tmp_path / quarantine_name).exists()
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not set(KNOWLEDGE_TABLES) & tables


def test_confirm_revalidates_dry_run_fingerprint_after_write_lock(tmp_path, monkeypatch) -> None:
    database = tmp_path / "oir.db"
    vectors = tmp_path / "oir_knowledge_milvus.db"
    _database(database)
    _vectors(vectors)
    original_manifest = cleanup_module._manifest
    scans = 0

    def mutate_after_first_scan(connection, vector_path, *, expected_collections):
        nonlocal scans
        manifest = original_manifest(
            connection,
            vector_path,
            expected_collections=expected_collections,
        )
        scans += 1
        if scans == 1:
            with sqlite3.connect(database) as writer:
                writer.execute(
                    "UPDATE knowledge_asset_chunks SET payload='changed after dry run' "
                    "WHERE id='chunk-0'"
                )
                writer.commit()
        return manifest

    monkeypatch.setattr(cleanup_module, "_manifest", mutate_after_first_scan)

    with pytest.raises(CleanupBlocked, match="changed after dry-run"):
        cleanup_oir_knowledge(database=database, knowledge_vectors=vectors, confirm=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_asset_chunks").fetchone() == (
            269,
        )
    assert vectors.exists()


def test_extra_collection_requires_explicit_repeatable_allowlist(tmp_path) -> None:
    database = tmp_path / "oir.db"
    vectors = tmp_path / "oir_knowledge_milvus.db"
    _database(database)
    _vectors(vectors)
    _vectors(vectors, collection="oir_knowledge_vectors_rehearsal")

    with pytest.raises(CleanupBlocked, match="collection inventory mismatch"):
        cleanup_oir_knowledge(database=database, knowledge_vectors=vectors, confirm=False)

    report = cleanup_oir_knowledge(
        database=database,
        knowledge_vectors=vectors,
        confirm=True,
        expected_collections=(
            "oir_knowledge_vectors",
            "oir_knowledge_vectors_rehearsal",
        ),
    )

    assert report["deleted_collections"] == [
        "oir_knowledge_vectors",
        "oir_knowledge_vectors_rehearsal",
    ]
