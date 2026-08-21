#!/usr/bin/env python3
"""Operate the offline Native Definition v1 -> v2 migration gate safely."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.native_definition_migration import (
    NativeDefinitionMigrationError,
    NativeDefinitionMigrationService,
    NativeDefinitionMigrationSnapshot,
    NativeDefinitionMigrationTargetCapabilities,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline Native Definition v1 -> v2 migration and rollback gate."
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="Registry/Plan/Run database URL; never printed by this command.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="Record the verified maintenance-window state.")
    _source_arguments(prepare)
    prepare.add_argument("--confirm-native-writes-frozen", action="store_true")
    prepare.add_argument("--confirm-new-execution-frozen", action="store_true")

    dry_run = commands.add_parser("dry-run", help="Inspect source without modifying Registry rows.")
    _source_arguments(dry_run)

    migrate = commands.add_parser("migrate", help="Snapshot and atomically convert the source.")
    _source_arguments(migrate)
    migrate.add_argument("--legacy-runtime-version", required=True)
    migrate.add_argument(
        "--snapshot-directory",
        type=Path,
        help="Required for file source; private snapshots are written with 0600 permissions.",
    )

    rollback = commands.add_parser(
        "rollback", help="Restore legacy source after restoring old binary."
    )
    rollback.add_argument("--source", choices=("database", "file"), required=True)
    rollback.add_argument("--snapshot-id", help="Required for database source.")
    rollback.add_argument("--snapshot-file", type=Path, help="Required for file source.")
    rollback.add_argument("--restore-legacy-binary", action="store_true")
    return parser


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", choices=("database", "file"), required=True)
    parser.add_argument("--registry-file", type=Path, help="Required when --source=file.")
    parser.add_argument(
        "--target-capability-manifest",
        type=Path,
        help=(
            "Private JSON target capability manifest. Required for enabled v2 Invocation "
            "or External Execution bindings."
        ),
    )


async def _main(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    engine = create_async_engine(args.database_url, future=True)
    service = NativeDefinitionMigrationService(
        async_sessionmaker(engine, expire_on_commit=False),
        target_capabilities=_target_capabilities(args),
    )
    try:
        if args.command == "prepare":
            await service.prepare(
                source=args.source,
                native_writes_frozen=args.confirm_native_writes_frozen,
                new_execution_frozen=args.confirm_new_execution_frozen,
            )
            return 0, {"status": "prepared", "source": args.source}

        if args.command == "rollback":
            snapshot = await _rollback(service, args)
            return 0, {"status": "rolled_back", "snapshot": _snapshot_payload(snapshot)}

        plan = await _dry_run(service, args)
        if args.command == "dry-run":
            return 0 if plan.report.ready_to_migrate else 2, {
                "status": "ready" if plan.report.ready_to_migrate else "blocked",
                "report": plan.report.to_safe_payload(),
            }
        if not plan.report.ready_to_migrate:
            return 2, {"status": "blocked", "report": plan.report.to_safe_payload()}
        result = await _migrate(service, plan, args)
        return 0, {
            "status": "migrated" if result.applied else "already_migrated",
            "report": result.report.to_safe_payload(),
            "snapshot": _snapshot_payload(result.snapshot) if result.snapshot else None,
        }
    finally:
        await engine.dispose()


def _target_capabilities(
    args: argparse.Namespace,
) -> NativeDefinitionMigrationTargetCapabilities | None:
    path = getattr(args, "target_capability_manifest", None)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeDefinitionMigrationError("migration_target_capabilities_invalid") from exc
    return NativeDefinitionMigrationTargetCapabilities.from_manifest(payload)


async def _dry_run(service: NativeDefinitionMigrationService, args: argparse.Namespace):
    if args.source == "database":
        return await service.dry_run_database()
    if args.registry_file is None:
        raise NativeDefinitionMigrationError("migration_source_invalid")
    return await service.dry_run_file(args.registry_file)


async def _migrate(service: NativeDefinitionMigrationService, plan: Any, args: argparse.Namespace):
    if args.source == "database":
        return await service.migrate_database(
            plan,
            legacy_runtime_version=args.legacy_runtime_version,
        )
    if args.snapshot_directory is None:
        raise NativeDefinitionMigrationError("migration_source_invalid")
    return await service.migrate_file(
        plan,
        snapshot_directory=args.snapshot_directory,
        legacy_runtime_version=args.legacy_runtime_version,
    )


async def _rollback(
    service: NativeDefinitionMigrationService,
    args: argparse.Namespace,
) -> NativeDefinitionMigrationSnapshot:
    if args.source == "database":
        if not args.snapshot_id:
            raise NativeDefinitionMigrationError("migration_snapshot_not_found")
        return await service.rollback_database(
            args.snapshot_id,
            legacy_runtime_restored=args.restore_legacy_binary,
        )
    if args.snapshot_file is None:
        raise NativeDefinitionMigrationError("migration_snapshot_not_found")
    return await service.rollback_file(
        args.snapshot_file,
        legacy_runtime_restored=args.restore_legacy_binary,
    )


def _snapshot_payload(snapshot: NativeDefinitionMigrationSnapshot) -> dict[str, object]:
    payload: dict[str, object] = {
        "snapshot_id": snapshot.snapshot_id,
        "source": snapshot.source,
        "migration_version": snapshot.migration_version,
        "definition_count": snapshot.definition_count,
        "input_fingerprint": snapshot.input_fingerprint,
        "created_at": snapshot.created_at.isoformat(),
    }
    if snapshot.location is not None:
        payload["location"] = str(snapshot.location)
    return payload


def main() -> int:
    args = _parser().parse_args()
    try:
        exit_code, payload = asyncio.run(_main(args))
    except NativeDefinitionMigrationError as exc:
        exit_code = 2
        payload = {"status": "blocked", "reason_code": exc.reason_code}
    except Exception:
        # Runtime errors can contain a database URL or source configuration. The
        # operator gets a stable code and can inspect the private snapshot/store.
        exit_code = 1
        payload = {"status": "error", "reason_code": "migration_internal_error"}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
