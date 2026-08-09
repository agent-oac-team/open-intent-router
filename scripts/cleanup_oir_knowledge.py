#!/usr/bin/env python3
"""Fail-closed removal of the retired OIR Knowledge replica."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.schemas.knowledge_persistence import sanitize_persisted_knowledge

KNOWLEDGE_TABLES = (
    "knowledge_sources",
    "knowledge_chunks",
    "knowledge_retrieval_logs",
    "knowledge_asset_groups",
    "knowledge_assets",
    "knowledge_asset_chunks",
    "knowledge_import_jobs",
    "knowledge_migration_manifests",
    "knowledge_operation_traces",
)
_DROP_ORDER = (
    "knowledge_operation_traces",
    "knowledge_migration_manifests",
    "knowledge_import_jobs",
    "knowledge_asset_chunks",
    "knowledge_assets",
    "knowledge_asset_groups",
    "knowledge_retrieval_logs",
    "knowledge_chunks",
    "knowledge_sources",
)
_JSON_COLUMNS = {
    "agent_runs": ("input_text", "output_text", "error_text"),
    "agent_results": ("output_text", "error_text"),
    "agent_events": ("payload_text",),
    "conversation_events": ("payload_text",),
    "execution_trace_events": ("facts_text", "evidence_refs_text"),
    "route_logs": ("evidence_text", "raw_output_text", "parsed_output_text", "error_text"),
}
_MEMORY_TABLE_PREFIX = "memory_"
_KNOWN_CONTEXT_KEYS = {
    "status",
    "trace_id",
    "item_count",
    "items",
    "citations",
    "source_ids",
    "truncated",
    "errors",
    "summary",
    "metadata",
}
_SUSPICIOUS_KEYS = {
    "knowledge_body",
    "knowledge_content",
    "knowledge_chunks",
    "retrieved_knowledge",
}
_KNOWLEDGE_SENTINELS = {"knowledge_context", *_SUSPICIOUS_KEYS}


class CleanupBlocked(RuntimeError):
    pass


def cleanup_oir_knowledge(
    *,
    database: Path,
    knowledge_vectors: Path,
    memory_vectors: Path | None = None,
    confirm: bool,
    expected_oir_chunks: int = 269,
    delete_vectors: bool = True,
    expected_collections: tuple[str, ...] = ("oir_knowledge_vectors",),
) -> dict[str, Any]:
    database = database.resolve()
    knowledge_vectors = knowledge_vectors.resolve()
    memory_vectors = memory_vectors.resolve() if memory_vectors else None
    if knowledge_vectors.name != "oir_knowledge_milvus.db":
        raise CleanupBlocked("Knowledge vector path must target oir_knowledge_milvus.db")
    if memory_vectors and knowledge_vectors == memory_vectors:
        raise CleanupBlocked("Knowledge and Memory vector paths must differ")
    if not database.is_file():
        raise CleanupBlocked(f"Database does not exist: {database}")
    expected_collections = _validated_expected_collections(expected_collections)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        manifest = _manifest(
            connection,
            knowledge_vectors,
            expected_collections=expected_collections,
        )
        history_plan = _history_plan(connection)
        memory_before = _memory_baseline(connection, memory_vectors)
        dry_run_fingerprint = _cleanup_fingerprint(manifest, history_plan, memory_before)
        report = {
            "contract": "oir-knowledge-cleanup/v1",
            "status": "dry_run_passed",
            "database": database.name,
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
                "delete_local_vector_path": (
                    knowledge_vectors.name if delete_vectors else "deferred"
                ),
            },
        }
        if report["database_chunk_count"] != expected_oir_chunks:
            raise CleanupBlocked(
                f"Unexplained OIR chunk count: expected {expected_oir_chunks}, "
                f"got {report['database_chunk_count']}"
            )
        if not confirm:
            return report

        quarantine: Path | None = None
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked_manifest = _manifest(
                connection,
                knowledge_vectors,
                expected_collections=expected_collections,
            )
            locked_history_plan = _history_plan(connection)
            locked_memory_before = _memory_baseline(connection, memory_vectors)
            locked_fingerprint = _cleanup_fingerprint(
                locked_manifest,
                locked_history_plan,
                locked_memory_before,
            )
            if locked_fingerprint != dry_run_fingerprint:
                raise CleanupBlocked("Cleanup target changed after dry-run")
            if delete_vectors:
                quarantine = _quarantine_knowledge_vectors(
                    knowledge_vectors,
                    expected_collections=expected_collections,
                )
            history_plan = locked_history_plan
            memory_before = locked_memory_before
            for table, column, rowid, rendered in history_plan["updates"]:
                connection.execute(
                    f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?',
                    (rendered, rowid),
                )
            for table in _DROP_ORDER:
                connection.execute(f'DROP TABLE "{table}"')
            memory_after = _memory_baseline(connection, memory_vectors)
            if memory_after != memory_before:
                raise CleanupBlocked("Memory baseline changed during Knowledge cleanup")
            connection.commit()
            committed = True
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            if quarantine is not None and quarantine.exists() and not knowledge_vectors.exists():
                try:
                    quarantine.rename(knowledge_vectors)
                except OSError as restore_error:
                    raise CleanupBlocked(
                        "Cleanup failed and the quarantined Knowledge vectors could not "
                        f"be restored: {quarantine}"
                    ) from restore_error
            raise

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
                    knowledge_vectors.name
                    if delete_vectors and not quarantine_cleanup_pending
                    else None
                ),
                "recoverable_vector_quarantine": (
                    quarantine.name if quarantine_cleanup_pending and quarantine else None
                ),
                "knowledge_vectors_deferred": not delete_vectors,
                "history_rows_sanitized": len(history_plan["updates"]),
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
        assert committed
        return report
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _manifest(
    connection: sqlite3.Connection,
    vectors: Path,
    *,
    expected_collections: tuple[str, ...],
) -> dict[str, Any]:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
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
        schema = [dict(row) for row in connection.execute(f'PRAGMA table_info("{table}")')]
        rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        table_manifest[table] = {
            "schema_fingerprint": _hash(schema),
            "row_count": len(rows),
            "aggregate_hash": _hash([list(row) for row in rows]),
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


def _history_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    _block_unknown_text_locations(connection, tables)
    updates = []
    scanned = 0
    for table, desired_columns in _JSON_COLUMNS.items():
        if table not in tables:
            continue
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        for column in desired_columns:
            if column not in columns:
                continue
            for rowid, raw in connection.execute(
                f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            ):
                scanned += 1
                value = _load_json(raw, table=table, column=column)
                _validate_knowledge_shapes(value)
                sanitized = sanitize_persisted_knowledge(value)
                if sanitized != value:
                    updates.append(
                        (
                            table,
                            column,
                            rowid,
                            json.dumps(
                                sanitized,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                    )
    return {"rows_scanned": scanned, "updates": updates}


def _block_unknown_text_locations(connection: sqlite3.Connection, tables: set[str]) -> None:
    """Reject Knowledge-shaped text outside the explicitly reviewed persistence columns."""
    allowed = {(table, column) for table, columns in _JSON_COLUMNS.items() for column in columns}
    for table in sorted(tables - set(KNOWLEDGE_TABLES)):
        text_columns = [
            row[1]
            for row in connection.execute(f'PRAGMA table_info("{table}")')
            if "TEXT" in (row[2] or "").upper()
        ]
        for column in text_columns:
            if (table, column) in allowed:
                continue
            for (raw,) in connection.execute(
                f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            ):
                if isinstance(raw, str) and any(
                    sentinel in raw for sentinel in _KNOWLEDGE_SENTINELS
                ):
                    raise CleanupBlocked(f"Unknown Knowledge body location: {table}.{column}")


def _load_json(raw: str, *, table: str, column: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CleanupBlocked(f"Invalid persisted JSON at {table}.{column}") from exc


def _validate_knowledge_shapes(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_knowledge_shapes(item)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in _SUSPICIOUS_KEYS:
            raise CleanupBlocked(f"Unknown Knowledge body location: {key}")
        if key == "knowledge_context":
            if not isinstance(item, dict):
                raise CleanupBlocked("knowledge_context must be an object")
            unknown = set(item) - _KNOWN_CONTEXT_KEYS
            if unknown:
                raise CleanupBlocked(f"Unknown knowledge_context fields: {sorted(unknown)}")
        _validate_knowledge_shapes(item)


def _memory_baseline(connection: sqlite3.Connection, memory_vectors: Path | None) -> dict[str, Any]:
    tables = sorted(
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if row[0].startswith(_MEMORY_TABLE_PREFIX)
    )
    table_state = {}
    for table in tables:
        rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        table_state[table] = {
            "row_count": len(rows),
            "aggregate_hash": _hash([list(row) for row in rows]),
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


def _collection_inventory(path: Path) -> list[str]:
    root = path / "collections"
    if not root.is_dir():
        raise CleanupBlocked(f"Knowledge vector collection directory is missing: {root}")
    return sorted(item.name for item in root.iterdir() if item.is_dir())


def _quarantine_knowledge_vectors(
    path: Path,
    *,
    expected_collections: tuple[str, ...],
) -> Path:
    if path.name != "oir_knowledge_milvus.db" or _collection_inventory(path) != sorted(
        expected_collections
    ):
        raise CleanupBlocked("Knowledge vector target changed after dry-run")
    quarantine = path.with_name(f".{path.name}.cleanup-quarantine-{uuid.uuid4().hex}")
    if quarantine.exists():
        raise CleanupBlocked(f"Knowledge vector quarantine already exists: {quarantine}")
    try:
        path.rename(quarantine)
    except OSError as exc:
        raise CleanupBlocked("Could not quarantine Knowledge vectors") from exc
    return quarantine


def _path_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _cleanup_fingerprint(
    manifest: dict[str, Any],
    history_plan: dict[str, Any],
    memory_baseline: dict[str, Any],
) -> str:
    return _hash(
        {
            "manifest": manifest,
            "history": history_plan,
            "memory": memory_baseline,
        }
    )


def _validated_expected_collections(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(item.strip() for item in values if item.strip()))
    if "oir_knowledge_vectors" not in normalized:
        raise CleanupBlocked("Expected collections must include oir_knowledge_vectors")
    if len(normalized) != len(values):
        raise CleanupBlocked("Expected collections must be non-empty and unique")
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    database_source = parser.add_mutually_exclusive_group(required=True)
    database_source.add_argument("--database", type=Path)
    database_source.add_argument("--database-url")
    parser.add_argument("--database-schema")
    parser.add_argument("--knowledge-vectors", type=Path, required=True)
    parser.add_argument("--memory-vectors", type=Path)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expected-oir-chunks", type=int, default=269)
    parser.add_argument(
        "--expected-collection",
        action="append",
        dest="expected_collections",
        default=None,
    )
    parser.add_argument("--retain-knowledge-vectors", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    cleanup_arguments = {
        "knowledge_vectors": args.knowledge_vectors,
        "memory_vectors": args.memory_vectors,
        "confirm": args.confirm,
        "expected_oir_chunks": args.expected_oir_chunks,
        "delete_vectors": not args.retain_knowledge_vectors,
        "expected_collections": tuple(args.expected_collections or ("oir_knowledge_vectors",)),
    }
    if args.database_url:
        from scripts.cleanup_oir_knowledge_postgres import (
            cleanup_oir_knowledge_postgres,
        )

        report = asyncio.run(
            cleanup_oir_knowledge_postgres(
                database_url=args.database_url,
                schema=args.database_schema,
                **cleanup_arguments,
            )
        )
    else:
        if args.database_schema:
            parser.error("--database-schema requires --database-url")
        report = cleanup_oir_knowledge(
            database=args.database,
            **cleanup_arguments,
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
