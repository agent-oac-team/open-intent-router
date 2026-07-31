"""PostgreSQL implementation of the fail-closed OIR Knowledge cleanup."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from app.schemas.knowledge_persistence import sanitize_persisted_knowledge
from scripts.cleanup_oir_knowledge import (
    _DROP_ORDER,
    _JSON_COLUMNS,
    _KNOWLEDGE_SENTINELS,
    _MEMORY_TABLE_PREFIX,
    KNOWLEDGE_TABLES,
    CleanupBlocked,
    _cleanup_fingerprint,
    _collection_inventory,
    _hash,
    _path_hash,
    _quarantine_knowledge_vectors,
    _validate_knowledge_shapes,
    _validated_expected_collections,
)


async def cleanup_oir_knowledge_postgres(
    *,
    database_url: str,
    knowledge_vectors: Path,
    memory_vectors: Path | None = None,
    confirm: bool,
    expected_oir_chunks: int = 269,
    delete_vectors: bool = True,
    expected_collections: tuple[str, ...] = ("oir_knowledge_vectors",),
    schema: str | None = None,
) -> dict[str, Any]:
    """Inspect or remove the fixed Knowledge replica from a PostgreSQL deployment."""
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - installation contract
        raise CleanupBlocked("asyncpg is required for PostgreSQL cleanup") from exc

    vectors = knowledge_vectors.resolve()
    memory_vectors = memory_vectors.resolve() if memory_vectors else None
    expected_collections = _validated_expected_collections(expected_collections)
    _validate_vector_targets(vectors, memory_vectors)
    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        connection = await asyncpg.connect(dsn=dsn)
    except Exception:
        raise CleanupBlocked("PostgreSQL connection failed") from None

    quarantine: Path | None = None
    transaction = None
    committed = False
    try:
        if schema is not None:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
                raise CleanupBlocked("PostgreSQL schema name is invalid")
            await connection.execute(f"SET search_path TO {_quote_identifier(schema)}")
        manifest, history_plan, memory_before, tables = await _scan(
            connection,
            vectors,
            memory_vectors,
            expected_collections,
        )
        report = _build_report(
            manifest=manifest,
            history_plan=history_plan,
            memory_before=memory_before,
            vectors=vectors,
            expected_oir_chunks=expected_oir_chunks,
            delete_vectors=delete_vectors,
            expected_collections=expected_collections,
        )
        if report["database_chunk_count"] != expected_oir_chunks:
            raise CleanupBlocked(
                f"Unexplained OIR chunk count: expected {expected_oir_chunks}, "
                f"got {report['database_chunk_count']}"
            )
        if not confirm:
            return report

        dry_run_fingerprint = _cleanup_fingerprint(manifest, history_plan, memory_before)
        transaction = connection.transaction(isolation="serializable")
        await transaction.start()
        await connection.execute("SELECT pg_advisory_xact_lock($1)", 1_853_221_669)
        for table in sorted(tables):
            await connection.execute(
                f"LOCK TABLE {_quote_identifier(table)} IN ACCESS EXCLUSIVE MODE"
            )
        locked_manifest, locked_history, locked_memory, locked_tables = await _scan(
            connection,
            vectors,
            memory_vectors,
            expected_collections,
        )
        if (
            locked_tables != tables
            or _cleanup_fingerprint(
                locked_manifest,
                locked_history,
                locked_memory,
            )
            != dry_run_fingerprint
        ):
            raise CleanupBlocked("Cleanup target changed after dry-run")

        if delete_vectors:
            quarantine = _quarantine_knowledge_vectors(
                vectors,
                expected_collections=expected_collections,
            )
        for table, column, locator, rendered in locked_history["updates"]:
            await connection.execute(
                f"UPDATE {_quote_identifier(table)} "
                f"SET {_quote_identifier(column)}=$1 "
                "WHERE ctid=$2::tid",
                rendered,
                locator,
            )
        for table in _DROP_ORDER:
            await connection.execute(f"DROP TABLE {_quote_identifier(table)}")
        memory_after = await _pg_memory_baseline(connection, memory_vectors)
        if memory_after != locked_memory:
            raise CleanupBlocked("Memory baseline changed during Knowledge cleanup")
        await transaction.commit()
        committed = True

        quarantine_cleanup_pending = False
        if quarantine is not None:
            try:
                shutil.rmtree(quarantine)
            except OSError:
                quarantine_cleanup_pending = True
        report.update(
            {
                "status": (
                    "executed_with_quarantine_cleanup_pending"
                    if quarantine_cleanup_pending
                    else "executed"
                ),
                "deleted_tables": list(_DROP_ORDER),
                "deleted_collections": (
                    list(expected_collections)
                    if delete_vectors and not quarantine_cleanup_pending
                    else []
                ),
                "deleted_vector_path": (
                    vectors.name if delete_vectors and not quarantine_cleanup_pending else None
                ),
                "recoverable_vector_quarantine": (
                    quarantine.name if quarantine_cleanup_pending and quarantine else None
                ),
                "knowledge_vectors_deferred": not delete_vectors,
                "history_rows_sanitized": len(locked_history["updates"]),
                "memory_after": memory_after,
                "memory_unchanged": True,
                "untouched_memory_objects": [
                    "memory_* tables",
                    "oir_memory_vectors",
                    "oir_memory_milvus.db",
                    "MEMORY_* configuration",
                ],
            }
        )
        return report
    except Exception:
        if transaction is not None and not committed:
            await transaction.rollback()
        if quarantine is not None and quarantine.exists() and not vectors.exists():
            try:
                quarantine.rename(vectors)
            except OSError as restore_error:
                raise CleanupBlocked(
                    "Cleanup failed and quarantined Knowledge vectors could not be restored"
                ) from restore_error
        raise
    finally:
        await connection.close()


def _validate_vector_targets(vectors: Path, memory_vectors: Path | None) -> None:
    if vectors.name != "oir_knowledge_milvus.db":
        raise CleanupBlocked("Knowledge vector path must target oir_knowledge_milvus.db")
    if memory_vectors and vectors == memory_vectors:
        raise CleanupBlocked("Knowledge and Memory vector paths must differ")


async def _scan(connection, vectors, memory_vectors, expected_collections):
    tables = await _pg_tables(connection)
    manifest = await _pg_manifest(
        connection,
        tables,
        vectors,
        expected_collections,
    )
    history = await _pg_history_plan(connection, tables)
    memory = await _pg_memory_baseline(connection, memory_vectors)
    return manifest, history, memory, tables


async def _pg_tables(connection) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT tablename
        FROM pg_catalog.pg_tables
        WHERE schemaname = current_schema()
        """
    )
    return {row["tablename"] for row in rows}


async def _pg_manifest(connection, tables, vectors, expected_collections):
    unknown = sorted(
        name for name in tables if "knowledge" in name and name not in KNOWLEDGE_TABLES
    )
    missing = sorted(set(KNOWLEDGE_TABLES) - tables)
    if unknown or missing:
        raise CleanupBlocked(
            f"Knowledge table inventory mismatch: unknown={unknown}, missing={missing}"
        )
    table_manifest = {}
    for table in KNOWLEDGE_TABLES:
        schema = [
            dict(row)
            for row in await connection.fetch(
                """
                SELECT column_name, data_type, udt_name, ordinal_position
                FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name=$1
                ORDER BY ordinal_position
                """,
                table,
            )
        ]
        row_json = [
            row["row_json"]
            for row in await connection.fetch(
                f"SELECT to_jsonb(t)::text AS row_json "
                f"FROM (SELECT * FROM {_quote_identifier(table)}) AS t"
            )
        ]
        table_manifest[table] = {
            "schema_fingerprint": _hash(schema),
            "row_count": len(row_json),
            "aggregate_hash": _hash(sorted(row_json)),
        }
    collections = _collection_inventory(vectors)
    if collections != sorted(expected_collections):
        raise CleanupBlocked(f"Knowledge collection inventory mismatch: {collections}")
    return {
        "schema_version": "oir-knowledge-replica/v1",
        "tables": table_manifest,
        "collections": {
            collection: {
                "present": True,
                "aggregate_hash": _path_hash(vectors / "collections" / collection),
            }
            for collection in expected_collections
        },
        "local_vector_path": {
            "name": vectors.name,
            "aggregate_hash": _path_hash(vectors),
        },
    }


async def _pg_history_plan(connection, tables):
    columns_by_table = await _pg_columns(connection)
    await _pg_block_unknown_text_locations(connection, tables, columns_by_table)
    updates = []
    scanned = 0
    for table, desired_columns in _JSON_COLUMNS.items():
        if table not in tables:
            continue
        columns = columns_by_table.get(table, {})
        for column in desired_columns:
            if column not in columns:
                continue
            rows = await connection.fetch(
                f"SELECT ctid::text AS locator, "
                f"{_quote_identifier(column)}::text AS raw "
                f"FROM {_quote_identifier(table)} "
                f"WHERE {_quote_identifier(column)} IS NOT NULL"
            )
            for row in rows:
                scanned += 1
                value = _load_json(row["raw"], table=table, column=column)
                _validate_knowledge_shapes(value)
                sanitized = sanitize_persisted_knowledge(value)
                if sanitized != value:
                    updates.append(
                        (
                            table,
                            column,
                            row["locator"],
                            json.dumps(
                                sanitized,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                    )
    return {"rows_scanned": scanned, "updates": updates}


async def _pg_columns(connection) -> dict[str, dict[str, str]]:
    rows = await connection.fetch(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema=current_schema()
        """
    )
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        result.setdefault(row["table_name"], {})[row["column_name"]] = row["data_type"]
    return result


async def _pg_block_unknown_text_locations(connection, tables, columns_by_table):
    allowed = {(table, column) for table, columns in _JSON_COLUMNS.items() for column in columns}
    text_types = {"text", "character varying", "character", "json", "jsonb"}
    for table in sorted(tables - set(KNOWLEDGE_TABLES)):
        for column, data_type in columns_by_table.get(table, {}).items():
            if (table, column) in allowed or data_type not in text_types:
                continue
            rows = await connection.fetch(
                f"SELECT {_quote_identifier(column)}::text AS raw "
                f"FROM {_quote_identifier(table)} "
                f"WHERE {_quote_identifier(column)} IS NOT NULL"
            )
            if any(
                isinstance(row["raw"], str)
                and any(sentinel in row["raw"] for sentinel in _KNOWLEDGE_SENTINELS)
                for row in rows
            ):
                raise CleanupBlocked(f"Unknown Knowledge body location: {table}.{column}")


async def _pg_memory_baseline(connection, memory_vectors):
    tables = sorted(
        table for table in await _pg_tables(connection) if table.startswith(_MEMORY_TABLE_PREFIX)
    )
    table_state = {}
    for table in tables:
        rows = [
            row["row_json"]
            for row in await connection.fetch(
                f"SELECT to_jsonb(t)::text AS row_json "
                f"FROM (SELECT * FROM {_quote_identifier(table)}) AS t"
            )
        ]
        table_state[table] = {
            "row_count": len(rows),
            "aggregate_hash": _hash(sorted(rows)),
        }
    return {
        "tables": table_state,
        "collection": "oir_memory_vectors",
        "vector_path": (
            {"name": memory_vectors.name, "aggregate_hash": _path_hash(memory_vectors)}
            if memory_vectors and memory_vectors.exists()
            else None
        ),
    }


def _load_json(raw: str, *, table: str, column: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CleanupBlocked(f"Invalid persisted JSON at {table}.{column}") from exc


def _build_report(
    *,
    manifest,
    history_plan,
    memory_before,
    vectors,
    expected_oir_chunks,
    delete_vectors,
    expected_collections,
):
    return {
        "contract": "oir-knowledge-cleanup/v1",
        "status": "dry_run_passed",
        "database": "postgresql",
        "manifest": manifest,
        "reconciliation": {
            "oir_chunk_count": 269,
            "knowledge_sys_chunk_count": 270,
            "difference": 1,
            "disposition": "no_migration_knowledge_sys_is_canonical",
        },
        "database_chunk_count": manifest["tables"]["knowledge_asset_chunks"]["row_count"],
        "expected_database_chunk_count": expected_oir_chunks,
        "history": {
            "rows_scanned": history_plan["rows_scanned"],
            "rows_to_sanitize": len(history_plan["updates"]),
        },
        "memory_before": memory_before,
        "planned_actions": {
            "drop_tables": list(_DROP_ORDER),
            "drop_collections": (list(expected_collections) if delete_vectors else "deferred"),
            "delete_local_vector_path": vectors.name if delete_vectors else "deferred",
        },
    }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
