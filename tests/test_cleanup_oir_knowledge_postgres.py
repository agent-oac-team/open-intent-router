import json
import os
from uuid import uuid4

import pytest

from scripts.cleanup_oir_knowledge import KNOWLEDGE_TABLES
from scripts.cleanup_oir_knowledge_postgres import (
    CleanupBlocked,
    _parse_pg_tid,
    cleanup_oir_knowledge_postgres,
)


def test_postgres_tid_locator_is_converted_for_asyncpg() -> None:
    assert _parse_pg_tid("(0,3)") == (0, 3)
    assert _parse_pg_tid("(123,45)") == (123, 45)
    with pytest.raises(CleanupBlocked, match="invalid ctid"):
        _parse_pg_tid("0,3")


@pytest.mark.skipif(
    not os.getenv("OIR_CLEANUP_TEST_DATABASE_URL"),
    reason="requires an explicitly isolated PostgreSQL integration database",
)
async def test_postgres_cleanup_locks_sanitizes_and_preserves_memory(tmp_path) -> None:
    import asyncpg

    database_url = os.environ["OIR_CLEANUP_TEST_DATABASE_URL"]
    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    schema = f"cleanup_test_{uuid4().hex}"
    connection = await asyncpg.connect(dsn=dsn)
    vectors = tmp_path / "oir_knowledge_milvus.db"
    collection = vectors / "collections" / "oir_knowledge_vectors"
    collection.mkdir(parents=True)
    (collection / "manifest.json").write_text('{"row_count":269}', encoding="utf-8")
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        for table in KNOWLEDGE_TABLES:
            await connection.execute(f'CREATE TABLE "{table}" (id text PRIMARY KEY, payload text)')
        await connection.executemany(
            "INSERT INTO knowledge_asset_chunks VALUES ($1, $2)",
            [(f"chunk-{index}", f"body-{index}") for index in range(269)],
        )
        await connection.execute("CREATE TABLE memory_items (id text PRIMARY KEY, content text)")
        await connection.execute("INSERT INTO memory_items VALUES ('memory-1', 'keep me')")
        await connection.execute(
            "CREATE TABLE agent_runs (run_id text PRIMARY KEY, input_text text)"
        )
        await connection.execute(
            "INSERT INTO agent_runs VALUES ($1, $2)",
            "run-1",
            json.dumps(
                {
                    "knowledge_context": {
                        "status": "ok",
                        "trace_id": "trace-1",
                        "summary": "remove me",
                        "items": [
                            {
                                "item_id": "item-1",
                                "source_id": "source-1",
                                "content": "remove me",
                            }
                        ],
                    }
                }
            ),
        )

        report = await cleanup_oir_knowledge_postgres(
            database_url=database_url,
            schema=schema,
            knowledge_vectors=vectors,
            confirm=True,
        )

        assert report["status"] == "executed"
        assert report["database"] == "postgresql"
        assert database_url not in json.dumps(report)
        remaining = {
            row["tablename"]
            for row in await connection.fetch(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=$1",
                schema,
            )
        }
        assert not remaining.intersection(KNOWLEDGE_TABLES)
        assert "memory_items" in remaining
        stored = json.loads(
            await connection.fetchval("SELECT input_text FROM agent_runs WHERE run_id='run-1'")
        )
        assert stored["knowledge_context"]["items"] == [
            {"item_id": "item-1", "source_id": "source-1"}
        ]
        assert "summary" not in stored["knowledge_context"]
    finally:
        await connection.execute("SET search_path TO public")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
