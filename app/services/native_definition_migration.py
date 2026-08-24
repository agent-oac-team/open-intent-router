"""Offline, rollback-safe cutover from legacy Native Definitions to v2.

This module is intentionally an operator-facing migration boundary.  Runtime
callers do not import it and do not dual-read the two definition shapes.  The
release sequence records an auditable maintenance-window assertion, verifies
that legacy work is drained, performs a dry-run, creates private rollback
material, and then applies one atomic source replacement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator, SchemaError
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    AgentDefinitionModel,
    AgentRunModel,
    NativeDefinitionMigrationPreparationModel,
    NativeDefinitionMigrationSnapshotModel,
    PlanModel,
    PlanStepModel,
)
from app.db.session import ensure_native_definition_migration_schema
from app.repositories.json_utils import dumps, loads
from app.schemas.agents import (
    AgentDefinitionV2,
    ExternalExecutionHandling,
    InvocationHandling,
    LegacyAgentDefinition,
    SafeHandlingConfiguration,
    UiHandoffHandling,
    is_safe_agent_identifier,
)
from app.services.registry_snapshot import RegistrySnapshotBuilder

MigrationSource = Literal["database", "file"]

MIGRATION_VERSION = "oir-native-definition-v2-migration-v1"
_FILE_SNAPSHOT_CONTRACT = "oir-native-definition-rollback-snapshot-v1"
_DATABASE_SNAPSHOT_CONTRACT = "oir-native-definition-database-rollback-snapshot-v1"
_TARGET_CAPABILITY_MANIFEST_CONTRACT = "oir-native-definition-target-capabilities-v1"
_GLOBAL_MIGRATION_FENCE_SOURCE = "__native_definition_global__"
# A controlled release tag is intentionally narrower than a generic string.
# It prevents the rollback field from becoming a convenient opaque channel for
# arbitrary configuration, even if a credential does not match one of the
# well-known token-shaped patterns below.
_SAFE_LEGACY_RUNTIME_VERSION = re.compile(
    r"^oir-legacy-[0-9]+(?:\.[0-9]+){0,2}(?:-[a-z0-9]+(?:\.[a-z0-9]+)*)?$"
)
_SECRET_LIKE_VERSION_PATTERNS = (
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^AIza[A-Za-z0-9_-]{35}$"),
    re.compile(r"^ghp_[A-Za-z0-9]{36}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{22,}$"),
    re.compile(r"^glpat-[A-Za-z0-9_-]{20,}$"),
    re.compile(r"^sk-(?:live|proj)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^sk_(?:live|test)_[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^xoxb-[0-9A-Za-z-]{20,}$"),
)
_TERMINAL_PLAN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "invalid_output", "cancelled", "timeout", "timed_out"}
)
_LEGACY_DATABASE_FIELDS = (
    "agent_id",
    "name",
    "description",
    "version",
    "revision",
    "type",
    "schema_version",
    "handling_text",
    "enabled",
    "domain",
    "capabilities_text",
    "tags_text",
    "trigger_text",
    "access_policy_text",
    "required_inputs_text",
    "optional_inputs_text",
    "input_schema_text",
    "output_schema_text",
    "invocation_text",
    "ui_handoff_text",
    "context_text",
    "priority",
    "metadata_text",
    "source",
)


class NativeDefinitionMigrationError(RuntimeError):
    """A non-leaking operational refusal or migration failure."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(_error_message(reason_code))


class _DefinitionConversionError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class NativeDefinitionMigrationTargetCapabilities:
    """A private target-release manifest used to validate v2 Binding Requirements.

    The offline tool deliberately does not guess from the legacy runtime.  Its
    caller supplies the logical Runtime/Executor capabilities of the *target*
    deployment, including each Runtime Adapter configuration schema.  The
    manifest is never persisted in rollback material or projected in CLI
    output; only the Definition's already-safe required identifiers are.
    """

    _runtime_adapter_schemas: Mapping[str, Mapping[str, object]]
    _runtime_adapter_invocation: frozenset[str]
    _executor_refs: frozenset[str]

    @classmethod
    def from_manifest(cls, payload: object) -> NativeDefinitionMigrationTargetCapabilities:
        if (
            not isinstance(payload, Mapping)
            or payload.get("contract") != _TARGET_CAPABILITY_MANIFEST_CONTRACT
        ):
            raise NativeDefinitionMigrationError("migration_target_capabilities_invalid")
        raw_adapters = payload.get("runtime_adapters")
        raw_executors = payload.get("executor_refs", [])
        if not isinstance(raw_adapters, list) or not isinstance(raw_executors, list):
            raise NativeDefinitionMigrationError("migration_target_capabilities_invalid")

        schemas: dict[str, Mapping[str, object]] = {}
        invocation_adapters: set[str] = set()
        for raw_adapter in raw_adapters:
            if not isinstance(raw_adapter, Mapping):
                raise NativeDefinitionMigrationError("migration_target_capabilities_invalid")
            key = raw_adapter.get("adapter_key")
            invocation = raw_adapter.get("invocation")
            schema = raw_adapter.get("config_schema")
            if (
                not isinstance(key, str)
                or not isinstance(invocation, bool)
                or not isinstance(schema, Mapping)
                or key in schemas
                or "v2_invocation" in raw_adapter
            ):
                raise NativeDefinitionMigrationError("migration_target_capabilities_invalid")
            try:
                InvocationHandling(adapter_key=key)
                schema_copy = json.loads(json.dumps(schema, sort_keys=True))
                if not isinstance(schema_copy, dict):
                    raise TypeError("Runtime Adapter schema must be an object")
                Draft202012Validator.check_schema(schema_copy)
            except (SchemaError, TypeError, ValidationError, ValueError) as exc:
                raise NativeDefinitionMigrationError(
                    "migration_target_capabilities_invalid"
                ) from exc
            schemas[key] = MappingProxyType(schema_copy)
            if invocation:
                invocation_adapters.add(key)

        executor_refs: set[str] = set()
        for raw_executor in raw_executors:
            if not isinstance(raw_executor, str) or raw_executor in executor_refs:
                raise NativeDefinitionMigrationError("migration_target_capabilities_invalid")
            try:
                ExternalExecutionHandling(executor_ref=raw_executor)
            except (ValidationError, ValueError) as exc:
                raise NativeDefinitionMigrationError(
                    "migration_target_capabilities_invalid"
                ) from exc
            executor_refs.add(raw_executor)
        return cls(
            _runtime_adapter_schemas=MappingProxyType(schemas),
            _runtime_adapter_invocation=frozenset(invocation_adapters),
            _executor_refs=frozenset(executor_refs),
        )

    @property
    def executor_refs(self) -> frozenset[str]:
        return self._executor_refs

    def validate(self, definition: AgentDefinitionV2) -> None:
        """Fail closed unless this target deployment can execute the Binding."""

        if not definition.enabled:
            return
        handling = definition.handling
        if isinstance(handling, InvocationHandling):
            schema = self._runtime_adapter_schemas.get(handling.adapter_key)
            if schema is None:
                raise _DefinitionConversionError("runtime_adapter_capability_missing")
            if handling.adapter_key not in self._runtime_adapter_invocation:
                raise _DefinitionConversionError("runtime_adapter_capability_unsupported")
            try:
                if (
                    next(
                        Draft202012Validator(dict(schema)).iter_errors(
                            handling.config.model_dump(mode="json", exclude_none=True)
                        ),
                        None,
                    )
                    is not None
                ):
                    raise _DefinitionConversionError("runtime_adapter_configuration_invalid")
            except _DefinitionConversionError:
                raise
            except (SchemaError, TypeError, ValueError) as exc:
                raise _DefinitionConversionError("runtime_adapter_configuration_invalid") from exc
            return
        if (
            isinstance(handling, ExternalExecutionHandling)
            and handling.executor_ref not in self._executor_refs
        ):
            raise _DefinitionConversionError("external_executor_capability_missing")


@dataclass(frozen=True, slots=True)
class NativeDefinitionMigrationIssue:
    source_index: int
    reason_code: str
    agent_id: str | None = None

    def to_safe_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_index": self.source_index,
            "reason_code": self.reason_code,
        }
        if self.agent_id is not None:
            payload["agent_id"] = self.agent_id
        return payload


@dataclass(frozen=True, slots=True)
class NativeDefinitionMigrationReport:
    source: MigrationSource
    source_definition_count: int
    migratable_definition_count: int
    already_v2_definition_count: int
    issues: tuple[NativeDefinitionMigrationIssue, ...]
    required_runtime_adapter_keys: tuple[str, ...]
    required_executor_refs: tuple[str, ...]
    native_writes_frozen: bool
    new_execution_frozen: bool
    active_legacy_plan_count: int
    active_legacy_run_count: int
    input_fingerprint: str

    @property
    def invalid_definition_count(self) -> int:
        return len(self.issues)

    @property
    def ready_to_migrate(self) -> bool:
        return (
            self.native_writes_frozen
            and self.new_execution_frozen
            and self.active_legacy_plan_count == 0
            and self.active_legacy_run_count == 0
            and not self.issues
        )

    def to_safe_payload(self) -> dict[str, object]:
        """Return operator evidence without raw source values or exceptions."""

        return {
            "contract": MIGRATION_VERSION,
            "source": self.source,
            "source_definition_count": self.source_definition_count,
            "migratable_definition_count": self.migratable_definition_count,
            "already_v2_definition_count": self.already_v2_definition_count,
            "invalid_definition_count": self.invalid_definition_count,
            "issues": [item.to_safe_payload() for item in self.issues],
            "required_runtime_adapter_keys": list(self.required_runtime_adapter_keys),
            "required_executor_refs": list(self.required_executor_refs),
            "native_writes_frozen": self.native_writes_frozen,
            "new_execution_frozen": self.new_execution_frozen,
            "active_legacy_plan_count": self.active_legacy_plan_count,
            "active_legacy_run_count": self.active_legacy_run_count,
            "ready_to_migrate": self.ready_to_migrate,
            "input_fingerprint": self.input_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class NativeDefinitionMigrationSnapshot:
    snapshot_id: str
    source: MigrationSource
    migration_version: str
    legacy_runtime_version: str
    definition_count: int
    input_fingerprint: str
    created_at: datetime
    location: Path | None = None


@dataclass(frozen=True, slots=True)
class NativeDefinitionMigrationResult:
    report: NativeDefinitionMigrationReport
    applied: bool
    snapshot: NativeDefinitionMigrationSnapshot | None


@dataclass(frozen=True, slots=True)
class _FileRollbackMaterial:
    snapshot: NativeDefinitionMigrationSnapshot
    path: Path
    source_text: str
    source_mode: int
    input_digest: str
    target_digest: str


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    source_index: int
    payload: Mapping[str, object]
    storage: Mapping[str, object]
    legacy: bool
    conversion_issue: str | None = None

    @property
    def agent_id(self) -> str | None:
        candidate = self.payload.get("agent_id")
        return candidate if is_safe_agent_identifier(candidate) else None


@dataclass(frozen=True, slots=True)
class NativeDefinitionMigrationPlan:
    """A short-lived in-process plan; its opaque digest prevents stale apply."""

    source: MigrationSource
    report: NativeDefinitionMigrationReport
    _input_digest: str
    _records: tuple[_SourceRecord, ...]
    _target_definitions: Mapping[str, AgentDefinitionV2]
    _legacy_agent_ids: tuple[str, ...]
    _file_path: Path | None = None
    _file_document: Mapping[str, object] | None = None
    _file_text: str | None = None
    _file_mode: int | None = None


class NativeDefinitionMigrationService:
    """Prepare, inspect, apply and roll back the offline Native v2 cutover."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        target_capabilities: NativeDefinitionMigrationTargetCapabilities | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._target_capabilities = target_capabilities

    async def prepare(
        self,
        *,
        source: MigrationSource,
        native_writes_frozen: bool,
        new_execution_frozen: bool,
    ) -> None:
        """Record the release-window assertions required before a dry-run.

        This enables a durable application fence as well as recording the
        operator assertion.  Database Native writes and new execution paths
        lock the same row and reject work until this migration either commits
        or is rolled back.  Operators must still stop older binaries which do
        not contain this fence before they assert the maintenance window.
        """

        _require_source(source)
        if not native_writes_frozen or not new_execution_frozen:
            raise NativeDefinitionMigrationError("migration_preparation_required")
        await ensure_native_definition_migration_schema(self._session_factory)
        async with self._session_factory() as session, session.begin():
            await self._lock_global_preparation_fence(session)
            active_other_source = await session.scalar(
                select(NativeDefinitionMigrationPreparationModel.source)
                .where(
                    NativeDefinitionMigrationPreparationModel.active.is_(True),
                    NativeDefinitionMigrationPreparationModel.source != source,
                )
                .with_for_update()
            )
            if active_other_source is not None:
                raise NativeDefinitionMigrationError("migration_preparation_conflict")
            preparation = await session.get(NativeDefinitionMigrationPreparationModel, source)
            if preparation is None:
                session.add(
                    NativeDefinitionMigrationPreparationModel(
                        source=source,
                        native_writes_frozen=True,
                        new_execution_frozen=True,
                        active=True,
                        recorded_at=datetime.now(UTC),
                    )
                )
            else:
                if not preparation.active:
                    # A completed window may retain its prior target for
                    # auditability. A fresh operator-approved window starts
                    # without that recovery constraint.
                    preparation.target_fingerprint = None
                preparation.native_writes_frozen = True
                preparation.new_execution_frozen = True
                preparation.active = True
                preparation.recorded_at = datetime.now(UTC)

    async def dry_run_database(self) -> NativeDefinitionMigrationPlan:
        """Read and analyze database Registry source without changing its rows."""

        records = await self._read_database_records()
        preparation = await self._read_preparation("database")
        return await self._build_plan(
            source="database",
            records=records,
            native_writes_frozen=preparation[0],
            new_execution_frozen=preparation[1],
        )

    async def dry_run_file(self, path: Path) -> NativeDefinitionMigrationPlan:
        """Read and analyze a YAML/JSON File Registry without writing it."""

        document, source_text, source_mode = _read_file_document(path)
        raw_agents = document.get("agents")
        if not isinstance(raw_agents, list):
            raise NativeDefinitionMigrationError("migration_source_invalid")
        records = _file_records(raw_agents)
        preparation = await self._read_preparation("file")
        plan = await self._build_plan(
            source="file",
            records=records,
            native_writes_frozen=preparation[0],
            new_execution_frozen=preparation[1],
            input_digest=_digest(source_text),
        )
        return NativeDefinitionMigrationPlan(
            source=plan.source,
            report=plan.report,
            _input_digest=plan._input_digest,
            _records=plan._records,
            _target_definitions=plan._target_definitions,
            _legacy_agent_ids=plan._legacy_agent_ids,
            _file_path=path,
            _file_document=document,
            _file_text=source_text,
            _file_mode=source_mode,
        )

    async def migrate_database(
        self,
        plan: NativeDefinitionMigrationPlan,
        *,
        legacy_runtime_version: str,
    ) -> NativeDefinitionMigrationResult:
        """Persist a private snapshot then atomically replace legacy DB rows."""

        _require_plan_source(plan, "database")
        _require_legacy_runtime_version(legacy_runtime_version)
        await ensure_native_definition_migration_schema(self._session_factory)
        if plan.report.issues:
            raise NativeDefinitionMigrationError("migration_validation_failed")
        if plan.report.migratable_definition_count == 0:
            await self._release_database_noop(plan)
            return NativeDefinitionMigrationResult(report=plan.report, applied=False, snapshot=None)
        self._require_ready(plan)

        snapshot = await self._create_database_snapshot(plan, legacy_runtime_version)
        target_fingerprint = _target_definitions_fingerprint(plan._target_definitions)
        result = NativeDefinitionMigrationResult(
            report=plan.report, applied=True, snapshot=snapshot
        )
        mutation_ready_to_commit = False
        try:
            async with self._session_factory() as session, session.begin():
                preparation = await self._lock_migration_window(session, source="database")
                preparation.target_fingerprint = target_fingerprint
                records = await self._read_database_records(session, lock=True)
                await self._require_current_database_window(session, plan, records)
                rows = {row.agent_id: row for row in await self._database_rows(session, lock=True)}
                for record in plan._records:
                    if not record.legacy:
                        continue
                    agent_id = record.agent_id
                    target = plan._target_definitions.get(agent_id or "")
                    row = rows.get(agent_id or "")
                    if agent_id is None or target is None or row is None:
                        raise NativeDefinitionMigrationError("migration_source_changed")
                    _apply_v2_database_values(row, target)
                await session.flush()
                await self._verify_database_target(session, plan._target_definitions)
                self._deactivate_preparation(preparation)
                # An exception after this point can be the transaction
                # manager's commit acknowledgement, rather than a failed
                # body. Re-read durable state before reporting a failure.
                mutation_ready_to_commit = True
        except NativeDefinitionMigrationError:
            await self._ensure_failure_fence(
                "database",
                target_fingerprint=target_fingerprint,
            )
            raise
        except Exception as exc:
            if mutation_ready_to_commit:
                return await self._recover_database_migration_commit_outcome(
                    plan,
                    result,
                    exc,
                    target_fingerprint=target_fingerprint,
                )
            await self._ensure_failure_fence(
                "database",
                target_fingerprint=target_fingerprint,
            )
            raise NativeDefinitionMigrationError("migration_apply_failed") from exc
        try:
            await self._after_database_migration_commit(snapshot)
        except Exception as exc:
            return await self._recover_database_migration_commit_outcome(
                plan,
                result,
                exc,
                target_fingerprint=target_fingerprint,
            )
        return result

    async def migrate_file(
        self,
        plan: NativeDefinitionMigrationPlan,
        *,
        snapshot_directory: Path,
        legacy_runtime_version: str,
    ) -> NativeDefinitionMigrationResult:
        """Atomically replace a file Registry after writing a private snapshot."""

        _require_plan_source(plan, "file")
        _require_legacy_runtime_version(legacy_runtime_version)
        if plan.report.issues:
            raise NativeDefinitionMigrationError("migration_validation_failed")
        if plan.report.migratable_definition_count == 0:
            await self._release_file_noop(plan)
            return NativeDefinitionMigrationResult(report=plan.report, applied=False, snapshot=None)
        self._require_ready(plan)
        path = plan._file_path
        if path is None or plan._file_text is None:
            raise NativeDefinitionMigrationError("migration_source_invalid")

        snapshot: NativeDefinitionMigrationSnapshot | None = None
        migrated_contents: str | None = None
        target_fingerprint: str | None = None
        mutation_ready_to_commit = False
        try:
            with _exclusive_file_migration_lock(path):
                # Read and construct the candidate File target before opening
                # any database transaction. The cooperative source lock keeps
                # supported writers out; ``_write_file_atomically`` performs
                # the final source-digest check for the actual replacement.
                document, current_text, source_mode = _read_file_document(path)
                raw_agents = document.get("agents")
                if not isinstance(raw_agents, list):
                    raise NativeDefinitionMigrationError("migration_source_invalid")
                if _digest(current_text) != plan._input_digest:
                    raise NativeDefinitionMigrationError("migration_source_changed")
                migrated_document = dict(document)
                migrated_document["agents"] = [
                    _file_v2_payload(plan._target_definitions[record.agent_id])
                    for record in plan._records
                    if record.agent_id is not None and record.agent_id in plan._target_definitions
                ]
                migrated_contents = _serialize_document(path, migrated_document)
                target_fingerprint = _digest(migrated_contents)

                # Keep the database transaction short: establish the durable
                # fence and recheck drain, then perform filesystem work only
                # after this transaction has committed.
                async with self._session_factory() as session, session.begin():
                    preparation = await self._lock_migration_window(session, source="file")
                    await self._require_current_file_window(session, plan)
                    preparation.target_fingerprint = target_fingerprint
                try:
                    snapshot = _write_file_snapshot(
                        path=path,
                        source_text=current_text,
                        source_mode=source_mode,
                        snapshot_directory=snapshot_directory,
                        legacy_runtime_version=legacy_runtime_version,
                        input_digest=plan._input_digest,
                        input_fingerprint=plan.report.input_fingerprint,
                        target_digest=target_fingerprint,
                        definition_count=plan.report.source_definition_count,
                    )
                    self._write_file_atomically(
                        path,
                        migrated_contents,
                        source_mode,
                        expected_source_digest=plan._input_digest,
                    )
                    self._after_file_replace(path)
                    verified_document, verified_text, _verified_mode = _read_file_document(path)
                    verified_agents = verified_document.get("agents")
                    if (
                        not isinstance(verified_agents, list)
                        or _digest(verified_text) != target_fingerprint
                    ):
                        raise NativeDefinitionMigrationError("migration_target_validation_failed")
                    verified = await self._build_plan(
                        source="file",
                        records=_file_records(verified_agents),
                        native_writes_frozen=True,
                        new_execution_frozen=True,
                        input_digest=_digest(verified_text),
                        active_legacy_work=(0, 0),
                    )
                    if verified.report.issues or verified.report.migratable_definition_count:
                        raise NativeDefinitionMigrationError("migration_target_validation_failed")
                    if not _file_digest_matches(path, target_fingerprint):
                        raise NativeDefinitionMigrationError("migration_recovery_target_mismatch")
                    async with self._session_factory() as session, session.begin():
                        preparation = await self._lock_migration_window(session, source="file")
                        await self._require_current_file_window(session, plan)
                        if preparation.target_fingerprint != target_fingerprint:
                            raise NativeDefinitionMigrationError(
                                "migration_recovery_target_mismatch"
                            )
                        self._deactivate_preparation(preparation)
                        # A transaction-exit error after this point may have
                        # committed the durable fence update. Resolve it from
                        # the file digest plus the durable fence state before
                        # restoring or reporting a failure.
                        mutation_ready_to_commit = True
                except Exception as exc:
                    if mutation_ready_to_commit:
                        if snapshot is None or target_fingerprint is None:
                            raise NativeDefinitionMigrationError("migration_apply_failed") from exc
                        return await self._recover_file_migration_commit_outcome(
                            plan,
                            snapshot,
                            target_fingerprint,
                            exc,
                        )
                    if (
                        snapshot is not None
                        and migrated_contents is not None
                        and _file_digest_matches(path, _digest(migrated_contents))
                    ):
                        self._restore_after_file_apply_failure(snapshot.location)
                    raise
                self._after_file_migration_commit(path)
                if target_fingerprint is None or not _file_digest_matches(path, target_fingerprint):
                    raise NativeDefinitionMigrationError("migration_commit_outcome_unknown")
        except NativeDefinitionMigrationError:
            await self._ensure_failure_fence(
                "file",
                target_fingerprint=target_fingerprint,
            )
            raise
        except Exception as exc:
            await self._ensure_failure_fence(
                "file",
                target_fingerprint=target_fingerprint,
            )
            raise NativeDefinitionMigrationError("migration_apply_failed") from exc
        if snapshot is None:  # pragma: no cover - snapshot is required before a write
            raise NativeDefinitionMigrationError("migration_apply_failed")
        return NativeDefinitionMigrationResult(report=plan.report, applied=True, snapshot=snapshot)

    async def rollback_database(
        self,
        snapshot_id: str,
        *,
        legacy_runtime_restored: bool,
    ) -> NativeDefinitionMigrationSnapshot:
        """Restore the legacy source only when the paired legacy binary is ready."""

        if not legacy_runtime_restored:
            raise NativeDefinitionMigrationError("migration_legacy_binary_restore_required")
        await self._preflight_database_rollback_snapshot(snapshot_id)
        await self._activate_migration_fence("database")
        rollback: NativeDefinitionMigrationSnapshot | None = None
        mutation_ready_to_commit = False
        try:
            async with self._session_factory() as session, session.begin():
                preparation = await self._lock_migration_window(session, source="database")
                snapshot = await session.get(NativeDefinitionMigrationSnapshotModel, snapshot_id)
                if snapshot is None or snapshot.source != "database":
                    raise NativeDefinitionMigrationError("migration_snapshot_not_found")
                if not _is_safe_legacy_runtime_version(snapshot.legacy_runtime_version):
                    raise NativeDefinitionMigrationError("migration_snapshot_invalid")
                payload = _load_database_snapshot_payload(snapshot.payload_text)
                records = payload["records"]
                if not isinstance(records, list):
                    raise NativeDefinitionMigrationError("migration_snapshot_invalid")
                if _digest(records) != snapshot.input_digest:
                    raise NativeDefinitionMigrationError("migration_snapshot_invalid")
                await self._require_global_drain(session)
                current_records = await self._read_database_records(session, lock=True)
                _require_current_database_rollback_target(current_records, payload)
                current = {
                    row.agent_id: row for row in await self._database_rows(session, lock=True)
                }
                for storage in records:
                    if not isinstance(storage, dict):
                        raise NativeDefinitionMigrationError("migration_snapshot_invalid")
                    agent_id = storage.get("agent_id")
                    if not isinstance(agent_id, str):
                        raise NativeDefinitionMigrationError("migration_snapshot_invalid")
                    row = current.get(agent_id)
                    if row is None:
                        row = AgentDefinitionModel(**storage)
                        session.add(row)
                    else:
                        _restore_database_values(row, storage)
                await session.flush()
                self._deactivate_preparation(preparation)
                rollback = _database_snapshot_metadata(snapshot, payload)
                mutation_ready_to_commit = True
        except NativeDefinitionMigrationError:
            await self._ensure_failure_fence("database")
            raise
        except Exception as exc:
            if mutation_ready_to_commit:
                return await self._recover_database_rollback_commit_outcome(snapshot_id, exc)
            await self._ensure_failure_fence("database")
            raise NativeDefinitionMigrationError("migration_rollback_failed") from exc
        if rollback is None:  # pragma: no cover - transaction always produces rollback metadata
            raise NativeDefinitionMigrationError("migration_rollback_failed")
        try:
            await self._after_database_rollback_commit(rollback)
        except Exception as exc:
            return await self._recover_database_rollback_commit_outcome(snapshot_id, exc)
        return rollback

    async def rollback_file(
        self,
        snapshot_location: Path,
        *,
        legacy_runtime_restored: bool,
    ) -> NativeDefinitionMigrationSnapshot:
        if not legacy_runtime_restored:
            raise NativeDefinitionMigrationError("migration_legacy_binary_restore_required")
        material = self._read_file_rollback_material(snapshot_location)
        path = material.path
        await self._activate_migration_fence("file")
        rollback: NativeDefinitionMigrationSnapshot | None = None
        mutation_ready_to_commit = False
        try:
            with _exclusive_file_migration_lock(path):
                # First DB transaction: retain the fence and recheck the
                # global drain before touching the source file.
                async with self._session_factory() as session, session.begin():
                    await self._lock_migration_window(session, source="file")
                    await self._require_global_drain(session)
                try:
                    # File validation/replacement is intentionally outside a
                    # database transaction. The durable fence remains active
                    # across this short filesystem phase.
                    rollback = self._restore_file_snapshot(snapshot_location)
                    mutation_ready_to_commit = True
                    if not _file_digest_matches(path, material.input_digest):
                        raise NativeDefinitionMigrationError("migration_recovery_target_mismatch")
                    async with self._session_factory() as session, session.begin():
                        preparation = await self._lock_migration_window(session, source="file")
                        await self._require_global_drain(session)
                        self._deactivate_preparation(preparation)
                except Exception as exc:
                    if mutation_ready_to_commit:
                        return await self._recover_file_rollback_commit_outcome(
                            snapshot_location,
                            exc,
                            file_lock_held=True,
                        )
                    if _file_digest_matches(path, material.input_digest):
                        return await self._recover_file_rollback_commit_outcome(
                            snapshot_location,
                            exc,
                            file_lock_held=True,
                        )
                    raise
                self._after_file_rollback_source_commit(path)
                if not _file_digest_matches(path, material.input_digest):
                    raise NativeDefinitionMigrationError("migration_commit_outcome_unknown")
        except NativeDefinitionMigrationError:
            await self._ensure_failure_fence("file")
            raise
        except Exception as exc:
            await self._ensure_failure_fence("file")
            raise NativeDefinitionMigrationError("migration_rollback_failed") from exc
        if rollback is None:  # pragma: no cover - restoration always has metadata
            raise NativeDefinitionMigrationError("migration_rollback_failed")
        try:
            await self._after_file_rollback_commit(rollback)
        except Exception as exc:
            return await self._recover_file_rollback_commit_outcome(snapshot_location, exc)
        return rollback

    async def snapshot_count(self, *, source: MigrationSource) -> int:
        _require_source(source)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(NativeDefinitionMigrationSnapshotModel.snapshot_id).where(
                    NativeDefinitionMigrationSnapshotModel.source == source
                )
            )
            return len(rows.all())

    async def _build_plan(
        self,
        *,
        source: MigrationSource,
        records: Sequence[_SourceRecord],
        native_writes_frozen: bool,
        new_execution_frozen: bool,
        input_digest: str | None = None,
        active_legacy_work: tuple[int, int] | None = None,
    ) -> NativeDefinitionMigrationPlan:
        issues: list[NativeDefinitionMigrationIssue] = []
        targets: dict[str, AgentDefinitionV2] = {}
        legacy_agent_ids: list[str] = []
        migratable_count = 0
        already_v2_count = 0
        adapter_keys: set[str] = set()
        executor_refs: set[str] = set()

        for record in records:
            if record.legacy and record.agent_id is not None:
                legacy_agent_ids.append(record.agent_id)
            if record.conversion_issue is not None:
                issues.append(
                    NativeDefinitionMigrationIssue(
                        source_index=record.source_index,
                        reason_code=record.conversion_issue,
                        agent_id=record.agent_id,
                    )
                )
                continue
            try:
                definition = _convert_record(record)
                if isinstance(definition.handling, InvocationHandling):
                    adapter_keys.add(definition.handling.adapter_key)
                elif isinstance(definition.handling, ExternalExecutionHandling):
                    executor_refs.add(definition.handling.executor_ref)
                _validate_v2_target(definition, self._target_capabilities)
            except _DefinitionConversionError as exc:
                issues.append(
                    NativeDefinitionMigrationIssue(
                        source_index=record.source_index,
                        reason_code=exc.reason_code,
                        agent_id=record.agent_id,
                    )
                )
                continue
            if definition.agent_id in targets:
                issues.append(
                    NativeDefinitionMigrationIssue(
                        source_index=record.source_index,
                        reason_code="duplicate_agent_id",
                        agent_id=definition.agent_id,
                    )
                )
                continue
            targets[definition.agent_id] = definition
            if record.legacy:
                migratable_count += 1
            else:
                already_v2_count += 1

        active_plans, active_runs = (
            active_legacy_work
            if active_legacy_work is not None
            else await self._active_legacy_work(tuple(legacy_agent_ids))
        )
        report = NativeDefinitionMigrationReport(
            source=source,
            source_definition_count=len(records),
            migratable_definition_count=migratable_count,
            already_v2_definition_count=already_v2_count,
            issues=tuple(issues),
            required_runtime_adapter_keys=tuple(sorted(adapter_keys)),
            required_executor_refs=tuple(sorted(executor_refs)),
            native_writes_frozen=native_writes_frozen,
            new_execution_frozen=new_execution_frozen,
            active_legacy_plan_count=active_plans,
            active_legacy_run_count=active_runs,
            input_fingerprint=_safe_input_fingerprint(source, records),
        )
        return NativeDefinitionMigrationPlan(
            source=source,
            report=report,
            _input_digest=input_digest or _records_digest(records),
            _records=tuple(records),
            _target_definitions=targets,
            _legacy_agent_ids=tuple(sorted(set(legacy_agent_ids))),
        )

    async def _read_database_records(
        self,
        session: AsyncSession | None = None,
        *,
        lock: bool = False,
    ) -> tuple[_SourceRecord, ...]:
        if session is None:
            try:
                async with self._session_factory() as owned_session:
                    rows = await self._database_rows(owned_session, lock=lock)
            except SQLAlchemyError as exc:
                raise NativeDefinitionMigrationError("migration_support_schema_missing") from exc
        else:
            rows = await self._database_rows(session, lock=lock)
        return tuple(_database_record_from_row(index, row) for index, row in enumerate(rows))

    async def _database_rows(
        self,
        session: AsyncSession,
        *,
        lock: bool = False,
    ) -> list[AgentDefinitionModel]:
        statement = select(AgentDefinitionModel).order_by(AgentDefinitionModel.agent_id)
        if lock:
            statement = statement.with_for_update()
        return list((await session.execute(statement)).scalars().all())

    async def _read_preparation(self, source: MigrationSource) -> tuple[bool, bool]:
        try:
            async with self._session_factory() as session:
                preparation = await session.get(NativeDefinitionMigrationPreparationModel, source)
        except SQLAlchemyError:
            return False, False
        if preparation is None or not preparation.active:
            return False, False
        return preparation.native_writes_frozen, preparation.new_execution_frozen

    async def _lock_migration_window(
        self,
        session: AsyncSession,
        *,
        source: MigrationSource,
    ) -> NativeDefinitionMigrationPreparationModel:
        """Lock the durable fence and the source/Plan/Run write sets.

        On PostgreSQL this is a table-level transactional fence, which blocks
        inserts/updates that could otherwise appear between the drain query and
        source replacement. SQLite obtains its equivalent single-writer lock by
        touching the already-locked preparation row. All application writers
        inspect that same row before they create Native work.
        """

        preparation = await session.scalar(
            select(NativeDefinitionMigrationPreparationModel)
            .where(NativeDefinitionMigrationPreparationModel.source == source)
            .with_for_update()
        )
        if (
            preparation is None
            or not preparation.active
            or not preparation.native_writes_frozen
            or not preparation.new_execution_frozen
        ):
            raise NativeDefinitionMigrationError("migration_preparation_required")
        connection = await session.connection()
        if connection.dialect.name == "postgresql":
            tables = ["plans", "plan_steps", "agent_runs"]
            if source == "database":
                tables.append("agent_definitions")
            await session.execute(
                text(f"LOCK TABLE {', '.join(tables)} IN SHARE ROW EXCLUSIVE MODE")
            )
        else:
            # ``FOR UPDATE`` is ignored by SQLite. A benign UPDATE acquires its
            # reserved writer lock before the source and drain rechecks.
            preparation.recorded_at = datetime.now(UTC)
            await session.flush()
        return preparation

    @staticmethod
    def _deactivate_preparation(preparation: NativeDefinitionMigrationPreparationModel) -> None:
        preparation.active = False
        preparation.native_writes_frozen = False
        preparation.new_execution_frozen = False
        preparation.recorded_at = datetime.now(UTC)

    async def _release_database_noop(self, plan: NativeDefinitionMigrationPlan) -> None:
        """Recover a restart after a committed source replacement without a reply."""

        try:
            async with self._session_factory() as session, session.begin():
                preparation = await self._lock_migration_window(session, source="database")
                records = await self._read_database_records(session, lock=True)
                await self._require_current_database_window(session, plan, records)
                if any(record.legacy for record in records):
                    raise NativeDefinitionMigrationError("migration_source_changed")
                if (
                    preparation.target_fingerprint is not None
                    and _target_definitions_fingerprint(plan._target_definitions)
                    != preparation.target_fingerprint
                ):
                    raise NativeDefinitionMigrationError("migration_recovery_target_mismatch")
                self._deactivate_preparation(preparation)
        except NativeDefinitionMigrationError as exc:
            if exc.reason_code == "migration_preparation_required":
                return
            raise

    async def _release_file_noop(self, plan: NativeDefinitionMigrationPlan) -> None:
        path = plan._file_path
        if path is None:
            raise NativeDefinitionMigrationError("migration_source_invalid")
        try:
            with _exclusive_file_migration_lock(path):
                document, source_text, _source_mode = _read_file_document(path)
                raw_agents = document.get("agents")
                if not isinstance(raw_agents, list) or _digest(source_text) != plan._input_digest:
                    raise NativeDefinitionMigrationError("migration_source_changed")
                if any(record.legacy for record in _file_records(raw_agents)):
                    raise NativeDefinitionMigrationError("migration_source_changed")
                # Prove that these exact v2 bytes are the target of an active
                # recovery window before treating a restart as a successful
                # no-op. Keep the DB transaction short; the directory sync
                # below must not run while it is held.
                async with self._session_factory() as session, session.begin():
                    preparation = await self._lock_migration_window(session, source="file")
                    await self._require_current_file_window(session, plan)
                    if (
                        preparation.target_fingerprint is not None
                        and plan._input_digest != preparation.target_fingerprint
                    ):
                        raise NativeDefinitionMigrationError("migration_recovery_target_mismatch")
                # A process may have died after ``os.replace`` and before the
                # original directory fsync. Re-synchronize the verified entry
                # before a second short transaction can reopen Runtime work.
                _fsync_directory(path.parent)
                if not _file_digest_matches(path, plan._input_digest):
                    raise NativeDefinitionMigrationError("migration_source_changed")
                async with self._session_factory() as session, session.begin():
                    preparation = await self._lock_migration_window(session, source="file")
                    await self._require_current_file_window(session, plan)
                    if (
                        preparation.target_fingerprint is not None
                        and plan._input_digest != preparation.target_fingerprint
                    ):
                        raise NativeDefinitionMigrationError("migration_recovery_target_mismatch")
                    self._deactivate_preparation(preparation)
        except NativeDefinitionMigrationError as exc:
            if exc.reason_code == "migration_preparation_required":
                return
            raise

    async def _activate_migration_fence(
        self,
        source: MigrationSource,
        *,
        target_fingerprint: str | None = None,
    ) -> None:
        await ensure_native_definition_migration_schema(self._session_factory)
        async with self._session_factory() as session, session.begin():
            await self._lock_global_preparation_fence(session)
            active_other_source = await session.scalar(
                select(NativeDefinitionMigrationPreparationModel.source)
                .where(
                    NativeDefinitionMigrationPreparationModel.active.is_(True),
                    NativeDefinitionMigrationPreparationModel.source != source,
                )
                .with_for_update()
            )
            if active_other_source is not None:
                raise NativeDefinitionMigrationError("migration_preparation_conflict")
            preparation = await session.scalar(
                select(NativeDefinitionMigrationPreparationModel)
                .where(NativeDefinitionMigrationPreparationModel.source == source)
                .with_for_update()
            )
            if preparation is None:
                session.add(
                    NativeDefinitionMigrationPreparationModel(
                        source=source,
                        native_writes_frozen=True,
                        new_execution_frozen=True,
                        active=True,
                        target_fingerprint=target_fingerprint,
                        recorded_at=datetime.now(UTC),
                    )
                )
            else:
                preparation.native_writes_frozen = True
                preparation.new_execution_frozen = True
                preparation.active = True
                if target_fingerprint is not None:
                    preparation.target_fingerprint = target_fingerprint
                preparation.recorded_at = datetime.now(UTC)

    async def _ensure_failure_fence(
        self,
        source: MigrationSource,
        *,
        target_fingerprint: str | None = None,
    ) -> None:
        """Keep an uncertain source state fail-closed after any apply attempt."""

        try:
            await self._activate_migration_fence(
                source,
                target_fingerprint=target_fingerprint,
            )
        except Exception as exc:
            raise NativeDefinitionMigrationError("migration_fence_recovery_failed") from exc

    async def _recover_database_migration_commit_outcome(
        self,
        plan: NativeDefinitionMigrationPlan,
        result: NativeDefinitionMigrationResult,
        cause: Exception,
        *,
        target_fingerprint: str,
    ) -> NativeDefinitionMigrationResult:
        """Resolve a commit/acknowledgement error from durable source state.

        The database source and fence change share one transaction. A fresh
        read can therefore prove success, while every other outcome remains
        frozen for an operator rather than being retried against an ambiguous
        Registry state.
        """

        if await self._database_matches_target(plan):
            return result
        try:
            await self._ensure_failure_fence(
                "database",
                target_fingerprint=target_fingerprint,
            )
        except NativeDefinitionMigrationError as fence_exc:
            raise NativeDefinitionMigrationError("migration_commit_outcome_unknown") from fence_exc
        raise NativeDefinitionMigrationError("migration_commit_outcome_unknown") from cause

    async def _recover_file_migration_commit_outcome(
        self,
        plan: NativeDefinitionMigrationPlan,
        snapshot: NativeDefinitionMigrationSnapshot,
        target_fingerprint: str,
        cause: Exception,
    ) -> NativeDefinitionMigrationResult:
        """Resolve the two durable halves of a File migration after ACK loss.

        File replacement precedes the transaction that opens the Runtime again.
        If that transaction committed, its inactive fence plus exact target
        bytes prove success. If it rolled back, restore only our exact target
        bytes; any other file contents remain untouched and the fence stays
        closed for operator recovery.
        """

        path = plan._file_path
        if path is not None and _file_digest_matches(path, target_fingerprint):
            try:
                async with self._session_factory() as session:
                    preparation = await session.get(
                        NativeDefinitionMigrationPreparationModel,
                        "file",
                    )
                if preparation is not None and not preparation.active:
                    return NativeDefinitionMigrationResult(
                        report=plan.report,
                        applied=True,
                        snapshot=snapshot,
                    )
                if preparation is not None and preparation.active:
                    self._restore_after_file_apply_failure(snapshot.location)
            except Exception:
                # The recovery fence below is deliberately retained if either
                # durable observation is unavailable.
                pass
        try:
            await self._ensure_failure_fence(
                "file",
                target_fingerprint=target_fingerprint,
            )
        except NativeDefinitionMigrationError as fence_exc:
            raise NativeDefinitionMigrationError("migration_commit_outcome_unknown") from fence_exc
        raise NativeDefinitionMigrationError("migration_commit_outcome_unknown") from cause

    async def _recover_database_rollback_commit_outcome(
        self,
        snapshot_id: str,
        cause: Exception,
    ) -> NativeDefinitionMigrationSnapshot:
        """Treat a rollback commit acknowledgement as successful only if proven."""

        rollback = await self._database_rollback_matches_snapshot(snapshot_id)
        if rollback is not None:
            return rollback
        try:
            await self._ensure_failure_fence("database")
        except NativeDefinitionMigrationError as fence_exc:
            raise NativeDefinitionMigrationError("migration_commit_outcome_unknown") from fence_exc
        raise NativeDefinitionMigrationError("migration_commit_outcome_unknown") from cause

    async def _database_rollback_matches_snapshot(
        self,
        snapshot_id: str,
    ) -> NativeDefinitionMigrationSnapshot | None:
        try:
            async with self._session_factory() as session:
                snapshot = await session.get(NativeDefinitionMigrationSnapshotModel, snapshot_id)
                if snapshot is None or snapshot.source != "database":
                    return None
                payload = _load_database_snapshot_payload(snapshot.payload_text)
                records = payload.get("records")
                if not isinstance(records, list) or _digest(records) != snapshot.input_digest:
                    return None
                current_records = await self._read_database_records(session)
                preparation = await session.get(
                    NativeDefinitionMigrationPreparationModel,
                    "database",
                )
                if (
                    preparation is None
                    or preparation.active
                    or _records_digest(current_records) != snapshot.input_digest
                ):
                    return None
                return _database_snapshot_metadata(snapshot, payload)
        except Exception:
            return None

    async def _recover_file_rollback_commit_outcome(
        self,
        snapshot_location: Path,
        cause: Exception,
        *,
        file_lock_held: bool = False,
    ) -> NativeDefinitionMigrationSnapshot:
        """Finish a rollback whose File replacement may outlive DB ACK loss."""

        material = self._read_file_rollback_material(snapshot_location)
        if not _file_digest_matches(material.path, material.input_digest):
            return await self._raise_unknown_file_rollback_outcome(cause)
        if not file_lock_held:
            with _exclusive_file_migration_lock(material.path):
                return await self._recover_file_rollback_commit_outcome(
                    snapshot_location,
                    cause,
                    file_lock_held=True,
                )
        try:
            # A previous restore may have replaced the source but lost its
            # directory fsync acknowledgement. Never reopen Runtime traffic
            # until the exact restored entry has been synchronized again.
            _fsync_directory(material.path.parent)
            rollback: NativeDefinitionMigrationSnapshot | None = None
            async with self._session_factory() as session, session.begin():
                preparation = await session.get(
                    NativeDefinitionMigrationPreparationModel,
                    "file",
                )
                if preparation is None:
                    raise NativeDefinitionMigrationError("migration_commit_outcome_unknown")
                if not preparation.active:
                    rollback = material.snapshot
                else:
                    preparation = await self._lock_migration_window(session, source="file")
                    await self._require_global_drain(session)
                    self._deactivate_preparation(preparation)
                    rollback = material.snapshot
            if rollback is not None:
                return rollback
        except Exception:
            pass
        return await self._raise_unknown_file_rollback_outcome(cause)

    async def _raise_unknown_file_rollback_outcome(
        self,
        cause: Exception,
    ) -> NativeDefinitionMigrationSnapshot:
        try:
            await self._ensure_failure_fence("file")
        except NativeDefinitionMigrationError as fence_exc:
            raise NativeDefinitionMigrationError("migration_commit_outcome_unknown") from fence_exc
        raise NativeDefinitionMigrationError("migration_commit_outcome_unknown") from cause

    async def _lock_global_preparation_fence(self, session: AsyncSession) -> None:
        """Serialize activation of the database/file maintenance windows."""

        fence = await session.scalar(
            select(NativeDefinitionMigrationPreparationModel)
            .where(
                NativeDefinitionMigrationPreparationModel.source == _GLOBAL_MIGRATION_FENCE_SOURCE
            )
            .with_for_update()
        )
        if fence is None:
            # ``ensure_native_definition_migration_schema`` installs this row.
            # Treat a damaged support table as a safe failure rather than
            # allowing two independent prepare calls to create overlapping gates.
            raise NativeDefinitionMigrationError("migration_support_schema_missing")
        # SQLite ignores FOR UPDATE; this durable no-op write takes its single
        # writer lock before querying the active source rows.
        fence.recorded_at = datetime.now(UTC)
        await session.flush()

    async def _preflight_database_rollback_snapshot(self, snapshot_id: str) -> None:
        try:
            async with self._session_factory() as session:
                snapshot = await session.get(NativeDefinitionMigrationSnapshotModel, snapshot_id)
                if snapshot is None or snapshot.source != "database":
                    raise NativeDefinitionMigrationError("migration_snapshot_not_found")
                if not _is_safe_legacy_runtime_version(snapshot.legacy_runtime_version):
                    raise NativeDefinitionMigrationError("migration_snapshot_invalid")
                payload = _load_database_snapshot_payload(snapshot.payload_text)
                records = payload.get("records")
                if not isinstance(records, list) or _digest(records) != snapshot.input_digest:
                    raise NativeDefinitionMigrationError("migration_snapshot_invalid")
        except NativeDefinitionMigrationError:
            raise
        except Exception as exc:
            raise NativeDefinitionMigrationError("migration_snapshot_invalid") from exc

    async def _require_global_drain(self, session: AsyncSession) -> None:
        active_plans = await session.scalar(
            select(PlanModel.plan_id)
            .where(PlanModel.status.not_in(_TERMINAL_PLAN_STATUSES))
            .limit(1)
        )
        active_runs = await session.scalar(
            select(AgentRunModel.run_id)
            .where(AgentRunModel.status.not_in(_TERMINAL_RUN_STATUSES))
            .limit(1)
        )
        if active_plans is not None or active_runs is not None:
            raise NativeDefinitionMigrationError("migration_drain_required")

    def _file_snapshot_path(self, snapshot_location: Path) -> Path:
        return self._read_file_rollback_material(snapshot_location).path

    def _read_file_rollback_material(
        self,
        snapshot_location: Path | None,
    ) -> _FileRollbackMaterial:
        """Parse all private rollback fields before touching the Registry path."""

        if snapshot_location is None or not snapshot_location.is_file():
            raise NativeDefinitionMigrationError("migration_snapshot_not_found")
        try:
            payload = json.loads(snapshot_location.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NativeDefinitionMigrationError("migration_snapshot_invalid") from exc
        if not isinstance(payload, dict) or payload.get("contract") != _FILE_SNAPSHOT_CONTRACT:
            raise NativeDefinitionMigrationError("migration_snapshot_invalid")
        snapshot = _file_snapshot_metadata(snapshot_location, payload)
        path_value = payload.get("registry_path")
        source_text = payload.get("source_text")
        source_mode = payload.get("source_mode")
        input_digest = payload.get("input_digest")
        target_digest = payload.get("target_digest")
        if (
            not isinstance(path_value, str)
            or not isinstance(source_text, str)
            or not isinstance(source_mode, int)
            or not isinstance(input_digest, str)
            or not isinstance(target_digest, str)
            or _digest(source_text) != input_digest
        ):
            raise NativeDefinitionMigrationError("migration_snapshot_invalid")
        return _FileRollbackMaterial(
            snapshot=snapshot,
            path=Path(path_value),
            source_text=source_text,
            source_mode=source_mode,
            input_digest=input_digest,
            target_digest=target_digest,
        )

    async def _database_matches_target(self, plan: NativeDefinitionMigrationPlan) -> bool:
        try:
            records = await self._read_database_records()
            definitions: dict[str, AgentDefinitionV2] = {}
            for record in records:
                if record.legacy or record.conversion_issue is not None:
                    return False
                definition = _convert_record(record)
                _validate_v2_target(definition, self._target_capabilities)
                if definition.agent_id in definitions:
                    return False
                definitions[definition.agent_id] = definition
            return _target_definitions_fingerprint(definitions) == _target_definitions_fingerprint(
                plan._target_definitions
            )
        except Exception:
            return False

    async def _after_database_migration_commit(
        self,
        _snapshot: NativeDefinitionMigrationSnapshot,
    ) -> None:
        """Failure-injection seam for unknown commit acknowledgement recovery."""

    async def _after_database_rollback_commit(
        self,
        _snapshot: NativeDefinitionMigrationSnapshot,
    ) -> None:
        """Failure-injection seam after a database rollback commit boundary."""

    async def _after_file_rollback_commit(
        self,
        _snapshot: NativeDefinitionMigrationSnapshot,
    ) -> None:
        """Failure-injection seam after a file rollback commit boundary."""

    async def _active_legacy_work(self, legacy_agent_ids: tuple[str, ...]) -> tuple[int, int]:
        if not legacy_agent_ids:
            return 0, 0
        try:
            async with self._session_factory() as session:
                return await self._active_legacy_work_in_session(session, legacy_agent_ids)
        except SQLAlchemyError as exc:
            raise NativeDefinitionMigrationError("migration_drain_state_unavailable") from exc

    async def _active_legacy_work_in_session(
        self,
        session: AsyncSession,
        legacy_agent_ids: tuple[str, ...],
    ) -> tuple[int, int]:
        if not legacy_agent_ids:
            return 0, 0
        plan_ids = (
            await session.scalars(
                select(PlanModel.plan_id)
                .join(PlanStepModel, PlanStepModel.plan_id == PlanModel.plan_id)
                .where(
                    PlanModel.status.not_in(_TERMINAL_PLAN_STATUSES),
                    PlanStepModel.agent_id.in_(legacy_agent_ids),
                )
                .distinct()
            )
        ).all()
        run_ids = (
            await session.scalars(
                select(AgentRunModel.run_id).where(
                    AgentRunModel.agent_id.in_(legacy_agent_ids),
                    AgentRunModel.status.not_in(_TERMINAL_RUN_STATUSES),
                )
            )
        ).all()
        return len(plan_ids), len(run_ids)

    async def _create_database_snapshot(
        self,
        plan: NativeDefinitionMigrationPlan,
        legacy_runtime_version: str,
    ) -> NativeDefinitionMigrationSnapshot:
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_migration_window(session, source="database")
                records = await self._read_database_records(session, lock=True)
                await self._require_current_database_window(session, plan, records)
                snapshot_id = f"native_definition_snapshot_{uuid4().hex}"
                created_at = datetime.now(UTC)
                payload = {
                    "contract": _DATABASE_SNAPSHOT_CONTRACT,
                    "source": "database",
                    "migration_version": MIGRATION_VERSION,
                    "records": [dict(record.storage) for record in records],
                    "target_fingerprint": _target_definitions_fingerprint(plan._target_definitions),
                }
                row = NativeDefinitionMigrationSnapshotModel(
                    snapshot_id=snapshot_id,
                    source="database",
                    migration_version=MIGRATION_VERSION,
                    legacy_runtime_version=legacy_runtime_version,
                    input_digest=plan._input_digest,
                    input_fingerprint=plan.report.input_fingerprint,
                    payload_text=dumps(payload),
                    created_at=created_at,
                )
                session.add(row)
                await session.flush()
                return NativeDefinitionMigrationSnapshot(
                    snapshot_id=snapshot_id,
                    source="database",
                    migration_version=MIGRATION_VERSION,
                    legacy_runtime_version=legacy_runtime_version,
                    definition_count=len(records),
                    input_fingerprint=plan.report.input_fingerprint,
                    created_at=created_at,
                )
        except NativeDefinitionMigrationError:
            raise
        except Exception as exc:
            raise NativeDefinitionMigrationError("migration_snapshot_failed") from exc

    async def _require_current_database_window(
        self,
        session: AsyncSession,
        plan: NativeDefinitionMigrationPlan,
        records: Sequence[_SourceRecord],
    ) -> None:
        if _records_digest(records) != plan._input_digest:
            raise NativeDefinitionMigrationError("migration_source_changed")
        active_plans, active_runs = await self._active_legacy_work_in_session(
            session,
            plan._legacy_agent_ids,
        )
        if active_plans or active_runs:
            raise NativeDefinitionMigrationError("migration_drain_required")

    async def _require_current_file_window(
        self,
        session: AsyncSession,
        plan: NativeDefinitionMigrationPlan,
    ) -> None:
        active_plans, active_runs = await self._active_legacy_work_in_session(
            session,
            plan._legacy_agent_ids,
        )
        if active_plans or active_runs:
            raise NativeDefinitionMigrationError("migration_drain_required")

    async def _verify_database_target(
        self,
        session: AsyncSession,
        target_definitions: Mapping[str, AgentDefinitionV2],
    ) -> None:
        rows = {row.agent_id: row for row in await self._database_rows(session, lock=False)}
        for agent_id, expected in target_definitions.items():
            row = rows.get(agent_id)
            if row is None:
                raise NativeDefinitionMigrationError("migration_target_validation_failed")
            payload = _v2_payload_from_database_row(row)
            try:
                actual = AgentDefinitionV2.model_validate(payload)
            except ValidationError as exc:
                raise NativeDefinitionMigrationError("migration_target_validation_failed") from exc
            _validate_v2_target(actual, self._target_capabilities)
            if actual.model_dump(mode="json", exclude_none=True) != expected.model_dump(
                mode="json", exclude_none=True
            ):
                raise NativeDefinitionMigrationError("migration_target_validation_failed")
            if (
                row.type
                or row.invocation_text != "{}"
                or row.ui_handoff_text != "{}"
                or row.metadata_text != "{}"
            ):
                raise NativeDefinitionMigrationError("migration_target_validation_failed")

    def _require_ready(self, plan: NativeDefinitionMigrationPlan) -> None:
        report = plan.report
        if not report.native_writes_frozen or not report.new_execution_frozen:
            raise NativeDefinitionMigrationError("migration_preparation_required")
        if report.active_legacy_plan_count or report.active_legacy_run_count:
            raise NativeDefinitionMigrationError("migration_drain_required")
        if report.issues:
            raise NativeDefinitionMigrationError("migration_validation_failed")

    def _write_file_atomically(
        self,
        path: Path,
        contents: str,
        mode: int,
        *,
        invoke_before_replace: bool = True,
        expected_source_digest: str | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".native-definition-migration.tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            if invoke_before_replace:
                self._before_file_replace(path, temporary_path)
            if expected_source_digest is not None and not _file_digest_matches(
                path,
                expected_source_digest,
            ):
                raise NativeDefinitionMigrationError("migration_source_changed")
            os.replace(temporary_path, path)
            _fsync_directory(path.parent)
        except Exception:
            if temporary_path.exists():
                temporary_path.unlink()
            raise

    def _before_file_replace(self, _source: Path, _temporary_path: Path) -> None:
        """Narrow failure-injection seam for the atomic-write conformance test."""

    def _after_file_replace(self, _source: Path) -> None:
        """Narrow seam proving target verification rejects post-replace drift."""

    def _after_file_migration_commit(self, _source: Path) -> None:
        """Narrow seam proving the final committed target digest is rechecked."""

    def _after_file_rollback_source_commit(self, _source: Path) -> None:
        """Narrow seam proving rollback rechecks restored source bytes."""

    def _restore_file_snapshot(
        self,
        snapshot_location: Path | None,
        *,
        require_target_match: bool = True,
    ) -> NativeDefinitionMigrationSnapshot:
        material = self._read_file_rollback_material(snapshot_location)
        if require_target_match:
            try:
                current_digest = _digest(material.path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise NativeDefinitionMigrationError("migration_rollback_source_changed") from exc
            if current_digest != material.target_digest:
                raise NativeDefinitionMigrationError("migration_rollback_source_changed")
        self._write_file_atomically(
            material.path,
            material.source_text,
            material.source_mode,
            invoke_before_replace=False,
            expected_source_digest=material.target_digest if require_target_match else None,
        )
        return material.snapshot

    def _restore_after_file_apply_failure(self, snapshot_location: Path | None) -> None:
        try:
            self._restore_file_snapshot(snapshot_location, require_target_match=True)
        except Exception as exc:
            raise NativeDefinitionMigrationError("migration_rollback_failed") from exc


def _convert_record(record: _SourceRecord) -> AgentDefinitionV2:
    if not record.payload:
        raise _DefinitionConversionError("legacy_definition_invalid")
    schema_version = record.payload.get("schema_version")
    if schema_version is not None:
        if schema_version != "oir-agent-v2":
            raise _DefinitionConversionError("definition_schema_invalid")
        try:
            return AgentDefinitionV2.model_validate(record.payload)
        except (TypeError, ValidationError, ValueError) as exc:
            raise _DefinitionConversionError("definition_schema_invalid") from exc
    try:
        legacy = LegacyAgentDefinition.model_validate(dict(record.payload))
    except (TypeError, ValidationError, ValueError) as exc:
        raise _DefinitionConversionError("legacy_definition_invalid") from exc
    return _legacy_to_v2(legacy)


def _legacy_to_v2(legacy: LegacyAgentDefinition) -> AgentDefinitionV2:
    if legacy.invocation.provider_config:
        raise _DefinitionConversionError("legacy_provider_configuration_unsupported")
    payload = legacy.model_dump(
        mode="json",
        exclude={"type", "invocation", "ui_handoff", "metadata"},
    )
    payload["schema_version"] = "oir-agent-v2"
    payload["revision"] = legacy.revision + 1
    if legacy.type == "ui_handoff":
        if legacy.ui_handoff.mode == "none" or not legacy.ui_handoff.route:
            raise _DefinitionConversionError("legacy_ui_handoff_invalid")
        try:
            params = SafeHandlingConfiguration.model_validate(legacy.ui_handoff.params)
            payload["handling"] = UiHandoffHandling(
                route=legacy.ui_handoff.route,
                params=params,
            ).model_dump(mode="json", exclude_none=True)
        except (TypeError, ValidationError, ValueError) as exc:
            raise _DefinitionConversionError("legacy_ui_handoff_invalid") from exc
    else:
        config = dict(legacy.invocation.config)
        connector_ref = config.pop("connector_ref", None)
        try:
            safe_config = SafeHandlingConfiguration.model_validate(config)
            payload["handling"] = InvocationHandling(
                adapter_key=legacy.type,
                connector_ref=connector_ref,
                config=safe_config,
            ).model_dump(mode="json", exclude_none=True)
        except (TypeError, ValidationError, ValueError) as exc:
            raise _DefinitionConversionError("legacy_invocation_configuration_invalid") from exc
    try:
        return AgentDefinitionV2.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise _DefinitionConversionError("definition_schema_invalid") from exc


def _validate_v2_target(
    definition: AgentDefinitionV2,
    target_capabilities: NativeDefinitionMigrationTargetCapabilities | None = None,
    *,
    require_target_capabilities: bool = True,
) -> None:
    """Validate the durable v2 shape and its target-deployment binding.

    ``RegistrySnapshotBuilder`` still owns the closed Definition/Requirement
    contract.  A no-catalog builder intentionally isolates Invocation
    Definitions, so it cannot by itself prove a target can execute them.  The
    migration gate therefore additionally requires a target capability
    manifest for enabled Invocation and External Execution handling.
    """

    snapshot = RegistrySnapshotBuilder(
        runtime_catalog=None,
        supported_executor_refs=(
            target_capabilities.executor_refs if target_capabilities is not None else ()
        ),
    ).build(
        [definition.model_dump(mode="json", exclude_none=True)],
        source="native_definition_migration",
    )
    entry = snapshot.entry_for(definition.agent_id)
    if (
        entry is None
        or snapshot.quarantined
        or (definition.enabled and entry.binding_requirement is None)
    ):
        raise _DefinitionConversionError("definition_schema_invalid")
    if not definition.enabled or not require_target_capabilities:
        return
    if isinstance(definition.handling, UiHandoffHandling):
        return
    if target_capabilities is None:
        raise _DefinitionConversionError("migration_target_capabilities_required")
    target_capabilities.validate(definition)


def _database_record_from_row(index: int, row: AgentDefinitionModel) -> _SourceRecord:
    storage = {name: getattr(row, name) for name in _LEGACY_DATABASE_FIELDS}
    schema_version = row.schema_version
    try:
        if schema_version:
            payload = _v2_payload_from_database_row(row)
            legacy = False
        else:
            payload = {
                "agent_id": row.agent_id,
                "name": row.name,
                "description": row.description,
                "version": row.version,
                "revision": row.revision,
                "enabled": row.enabled,
                "type": row.type,
                "domain": row.domain,
                "capabilities": loads(row.capabilities_text, []),
                "tags": loads(row.tags_text, []),
                "trigger": loads(row.trigger_text, {}),
                "access_policy": loads(row.access_policy_text, {}),
                "required_inputs": loads(row.required_inputs_text, []),
                "optional_inputs": loads(row.optional_inputs_text, []),
                "input_schema": loads(row.input_schema_text, {}),
                "output_schema": loads(row.output_schema_text, {}),
                "invocation": loads(row.invocation_text, {}),
                "ui_handoff": loads(row.ui_handoff_text, {}),
                "context": loads(row.context_text, {}),
                "priority": row.priority,
                "metadata": loads(row.metadata_text, {}),
                "source": row.source,
            }
            legacy = True
    except (TypeError, ValueError, json.JSONDecodeError):
        return _SourceRecord(
            source_index=index,
            payload={"agent_id": row.agent_id},
            storage=storage,
            legacy=not bool(schema_version),
            conversion_issue="database_definition_serialization_invalid",
        )
    return _SourceRecord(index, payload, storage, legacy)


def _v2_payload_from_database_row(row: AgentDefinitionModel) -> dict[str, object]:
    return {
        "schema_version": row.schema_version,
        "agent_id": row.agent_id,
        "name": row.name,
        "description": row.description,
        "version": row.version,
        "revision": row.revision,
        "enabled": row.enabled,
        "domain": row.domain,
        "capabilities": loads(row.capabilities_text, []),
        "tags": loads(row.tags_text, []),
        "trigger": loads(row.trigger_text, {}),
        "access_policy": loads(row.access_policy_text, {}),
        "required_inputs": loads(row.required_inputs_text, []),
        "optional_inputs": loads(row.optional_inputs_text, []),
        "input_schema": loads(row.input_schema_text, {}),
        "output_schema": loads(row.output_schema_text, {}),
        "context": loads(row.context_text, {}),
        "priority": row.priority,
        "source": row.source,
        "handling": loads(row.handling_text, {}),
    }


def _apply_v2_database_values(row: AgentDefinitionModel, definition: AgentDefinitionV2) -> None:
    row.name = definition.name
    row.description = definition.description
    row.version = definition.version
    row.revision = definition.revision
    row.enabled = definition.enabled
    row.domain = definition.domain
    row.capabilities_text = dumps(definition.capabilities)
    row.tags_text = dumps(definition.tags)
    row.trigger_text = dumps(definition.trigger.model_dump())
    row.access_policy_text = dumps(definition.access_policy.model_dump())
    row.required_inputs_text = dumps(definition.required_inputs)
    row.optional_inputs_text = dumps(definition.optional_inputs)
    row.input_schema_text = dumps(definition.input_schema.model_dump())
    row.output_schema_text = dumps(definition.output_schema.model_dump())
    row.context_text = dumps(definition.context.model_dump())
    row.priority = definition.priority
    row.source = definition.source
    row.schema_version = definition.schema_version
    row.handling_text = dumps(definition.handling.model_dump(mode="json", exclude_none=True))
    # These fields may contain endpoints, headers or credentials in a legacy
    # source.  They are cleared before the release can start the v2 binary.
    row.type = ""
    row.invocation_text = "{}"
    row.ui_handoff_text = "{}"
    row.metadata_text = "{}"


def _restore_database_values(row: AgentDefinitionModel, storage: Mapping[str, object]) -> None:
    for name in _LEGACY_DATABASE_FIELDS:
        if name not in storage:
            raise NativeDefinitionMigrationError("migration_snapshot_invalid")
        setattr(row, name, storage[name])


def _file_v2_payload(definition: AgentDefinitionV2) -> dict[str, object]:
    payload = definition.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"source", "created_at", "updated_at"},
    )
    return payload


def _file_records(raw_agents: Sequence[object]) -> tuple[_SourceRecord, ...]:
    return tuple(
        _SourceRecord(
            source_index=index,
            payload={**item, "source": "file"} if isinstance(item, dict) else {},
            storage=dict(item) if isinstance(item, dict) else {},
            legacy=not (isinstance(item, dict) and item.get("schema_version") == "oir-agent-v2"),
        )
        for index, item in enumerate(raw_agents)
    )


@contextmanager
def _exclusive_file_migration_lock(path: Path):
    """Take the cooperative source lock before a file dry-run is applied.

    Registry files are intentionally read-only to the Runtime.  The sidecar
    lock serializes supported operator tooling, while the source digest is
    checked immediately before ``replace`` to fail closed for an out-of-band
    edit instead of overwriting it.
    """

    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - supported deployments are POSIX
        raise NativeDefinitionMigrationError("migration_source_lock_unavailable") from exc
    lock_path = path.with_name(f".{path.name}.native-definition-migration.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NativeDefinitionMigrationError("migration_source_busy") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    """Durably record a replace/create directory entry before reporting success."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise NativeDefinitionMigrationError("migration_file_sync_failed") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise NativeDefinitionMigrationError("migration_file_sync_failed") from exc
    finally:
        os.close(descriptor)


def _create_private_snapshot_directory(snapshot_directory: Path) -> None:
    """Create and durably link every newly-created private snapshot ancestor."""

    created: list[Path] = []
    ancestor = snapshot_directory
    while not ancestor.exists():
        created.append(ancestor)
        if ancestor.parent == ancestor:
            raise NativeDefinitionMigrationError("migration_file_sync_failed")
        ancestor = ancestor.parent
    try:
        snapshot_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(snapshot_directory, 0o700)
    except OSError as exc:
        raise NativeDefinitionMigrationError("migration_file_sync_failed") from exc
    # Start at the leaf: first persist its metadata, then its directory entry
    # in the parent. Repeating upward makes a nested, newly-created location
    # reachable after a crash rather than merely syncing the final leaf.
    for directory in created:
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _file_digest_matches(path: Path, expected_digest: str) -> bool:
    try:
        return _digest(path.read_text(encoding="utf-8")) == expected_digest
    except OSError:
        return False


def _read_file_document(path: Path) -> tuple[dict[str, object], str, int]:
    try:
        source_text = path.read_text(encoding="utf-8")
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise NativeDefinitionMigrationError("migration_source_invalid") from exc
    try:
        if path.suffix.lower() == ".json":
            document = json.loads(source_text)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            document = yaml.safe_load(source_text)
        else:
            raise NativeDefinitionMigrationError("migration_source_invalid")
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise NativeDefinitionMigrationError("migration_source_invalid") from exc
    if not isinstance(document, dict):
        raise NativeDefinitionMigrationError("migration_source_invalid")
    return document, source_text, mode


def _serialize_document(path: Path, document: Mapping[str, object]) -> str:
    if path.suffix.lower() == ".json":
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if path.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=False)
    raise NativeDefinitionMigrationError("migration_source_invalid")


def _write_file_snapshot(
    *,
    path: Path,
    source_text: str,
    source_mode: int,
    snapshot_directory: Path,
    legacy_runtime_version: str,
    input_digest: str,
    input_fingerprint: str,
    target_digest: str,
    definition_count: int,
) -> NativeDefinitionMigrationSnapshot:
    _create_private_snapshot_directory(snapshot_directory)
    snapshot_id = f"native_definition_snapshot_{uuid4().hex}"
    location = snapshot_directory / f"{snapshot_id}.json"
    created_at = datetime.now(UTC)
    payload = {
        "contract": _FILE_SNAPSHOT_CONTRACT,
        "snapshot_id": snapshot_id,
        "source": "file",
        "migration_version": MIGRATION_VERSION,
        "legacy_runtime_version": legacy_runtime_version,
        "input_digest": input_digest,
        "input_fingerprint": input_fingerprint,
        "target_digest": target_digest,
        "definition_count": definition_count,
        "created_at": created_at.isoformat(),
        "registry_path": str(path),
        "source_mode": source_mode,
        # Private rollback material: it may contain legacy credentials and is
        # written with 0600 permissions; it is never printed or served.
        "source_text": source_text,
    }
    descriptor = os.open(location, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(location, 0o600)
        _fsync_directory(snapshot_directory)
    except Exception:
        if location.exists():
            location.unlink()
            try:
                _fsync_directory(snapshot_directory)
            except NativeDefinitionMigrationError:
                pass
        raise
    return NativeDefinitionMigrationSnapshot(
        snapshot_id=snapshot_id,
        source="file",
        migration_version=MIGRATION_VERSION,
        legacy_runtime_version=legacy_runtime_version,
        definition_count=definition_count,
        input_fingerprint=input_fingerprint,
        created_at=created_at,
        location=location,
    )


def _file_snapshot_metadata(
    location: Path,
    payload: Mapping[str, object],
) -> NativeDefinitionMigrationSnapshot:
    try:
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        snapshot_id = str(payload["snapshot_id"])
        migration_version = str(payload["migration_version"])
        legacy_runtime_version = str(payload["legacy_runtime_version"])
        definition_count = int(payload["definition_count"])
        input_fingerprint = str(payload["input_fingerprint"])
    except (KeyError, TypeError, ValueError) as exc:
        raise NativeDefinitionMigrationError("migration_snapshot_invalid") from exc
    if migration_version != MIGRATION_VERSION or not _is_safe_legacy_runtime_version(
        legacy_runtime_version
    ):
        raise NativeDefinitionMigrationError("migration_snapshot_invalid")
    return NativeDefinitionMigrationSnapshot(
        snapshot_id=snapshot_id,
        source="file",
        migration_version=migration_version,
        legacy_runtime_version=legacy_runtime_version,
        definition_count=definition_count,
        input_fingerprint=input_fingerprint,
        created_at=created_at,
        location=location,
    )


def _load_database_snapshot_payload(payload_text: str) -> dict[str, object]:
    payload = loads(payload_text, None)
    if (
        not isinstance(payload, dict)
        or payload.get("contract") != _DATABASE_SNAPSHOT_CONTRACT
        or payload.get("source") != "database"
        or payload.get("migration_version") != MIGRATION_VERSION
    ):
        raise NativeDefinitionMigrationError("migration_snapshot_invalid")
    return payload


def _require_current_database_rollback_target(
    records: Sequence[_SourceRecord],
    payload: Mapping[str, object],
) -> None:
    expected_fingerprint = payload.get("target_fingerprint")
    if not isinstance(expected_fingerprint, str):
        raise NativeDefinitionMigrationError("migration_snapshot_invalid")
    current_definitions: dict[str, AgentDefinitionV2] = {}
    for record in records:
        if record.legacy:
            raise NativeDefinitionMigrationError("migration_rollback_source_changed")
        try:
            definition = _convert_record(record)
            _validate_v2_target(definition, require_target_capabilities=False)
        except _DefinitionConversionError as exc:
            raise NativeDefinitionMigrationError("migration_rollback_source_changed") from exc
        if definition.agent_id in current_definitions:
            raise NativeDefinitionMigrationError("migration_rollback_source_changed")
        current_definitions[definition.agent_id] = definition
    if _target_definitions_fingerprint(current_definitions) != expected_fingerprint:
        raise NativeDefinitionMigrationError("migration_rollback_source_changed")


def _database_snapshot_metadata(
    snapshot: NativeDefinitionMigrationSnapshotModel,
    payload: Mapping[str, object],
) -> NativeDefinitionMigrationSnapshot:
    records = payload.get("records")
    if not isinstance(records, list):
        raise NativeDefinitionMigrationError("migration_snapshot_invalid")
    return NativeDefinitionMigrationSnapshot(
        snapshot_id=snapshot.snapshot_id,
        source="database",
        migration_version=snapshot.migration_version,
        legacy_runtime_version=snapshot.legacy_runtime_version,
        definition_count=len(records),
        input_fingerprint=snapshot.input_fingerprint,
        created_at=snapshot.created_at,
    )


def _records_digest(records: Sequence[_SourceRecord]) -> str:
    return _digest([dict(record.storage) for record in records])


def _target_definitions_fingerprint(
    definitions: Mapping[str, AgentDefinitionV2],
) -> str:
    return _digest(
        {
            agent_id: definition.model_dump(mode="json", exclude_none=True)
            for agent_id, definition in sorted(definitions.items())
        }
    )


def _safe_input_fingerprint(source: MigrationSource, records: Sequence[_SourceRecord]) -> str:
    safe_identity = [
        {
            "source_index": record.source_index,
            "agent_id": record.agent_id,
            "revision": record.payload.get("revision"),
            "schema_version": record.payload.get("schema_version"),
        }
        for record in records
    ]
    return _digest({"source": source, "definitions": safe_identity})


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_source(source: str) -> None:
    if source not in {"database", "file"}:
        raise NativeDefinitionMigrationError("migration_source_invalid")


def _require_plan_source(plan: NativeDefinitionMigrationPlan, source: MigrationSource) -> None:
    if plan.source != source:
        raise NativeDefinitionMigrationError("migration_source_invalid")


def _require_legacy_runtime_version(value: str) -> None:
    if not _is_safe_legacy_runtime_version(value):
        raise NativeDefinitionMigrationError("migration_legacy_runtime_version_required")


def _is_safe_legacy_runtime_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(_SAFE_LEGACY_RUNTIME_VERSION.fullmatch(value))
        and not any(pattern.fullmatch(value) for pattern in _SECRET_LIKE_VERSION_PATTERNS)
    )


def _error_message(reason_code: str) -> str:
    messages = {
        "migration_preparation_required": "Native writes and new execution must be frozen first.",
        "migration_drain_required": "Legacy Plans or Runs must be drained before migration.",
        "migration_validation_failed": "Native Definition migration validation failed.",
        "migration_source_changed": "Registry source changed after the dry-run.",
        "migration_snapshot_failed": "Rollback snapshot could not be created.",
        "migration_snapshot_not_found": "Rollback snapshot was not found.",
        "migration_snapshot_invalid": "Rollback snapshot is invalid.",
        "migration_apply_failed": "Native Definition migration could not be applied safely.",
        "migration_target_validation_failed": "Migrated Native Definitions did not validate.",
        "migration_rollback_failed": "Native Definition rollback could not be completed.",
        "migration_rollback_source_changed": "Registry source changed after migration; rollback is unsafe.",
        "migration_legacy_binary_restore_required": "Rollback requires the legacy binary to be restored.",
        "migration_legacy_runtime_version_required": "Legacy runtime version is required for rollback.",
        "migration_support_schema_missing": "Migration support schema is unavailable.",
        "migration_drain_state_unavailable": "Legacy Plan and Run drain state is unavailable.",
        "migration_source_invalid": "Native Definition migration source is invalid.",
        "migration_source_busy": "Native Definition Registry source is already locked for migration.",
        "migration_source_lock_unavailable": "Native Definition Registry source lock is unavailable.",
        "migration_file_sync_failed": "Native Definition File source could not be durably synchronized.",
        "migration_target_capabilities_required": "Target Runtime and Executor capabilities are required.",
        "migration_target_capabilities_invalid": "Target Runtime and Executor capabilities are invalid.",
        "migration_preparation_conflict": "Another Native Definition migration window is active.",
        "runtime_adapter_capability_missing": "A required Runtime Adapter is absent from the target.",
        "runtime_adapter_capability_unsupported": "A target Runtime Adapter cannot invoke Agents.",
        "runtime_adapter_configuration_invalid": "Definition configuration is invalid for the target Runtime Adapter.",
        "external_executor_capability_missing": "A required External Executor is absent from the target.",
        "migration_commit_outcome_unknown": "Migration commit outcome could not be verified safely.",
        "migration_recovery_target_mismatch": (
            "Recovery fence target does not match the current Registry source."
        ),
        "migration_fence_recovery_failed": "Migration safety fence could not be recovered.",
    }
    return messages.get(reason_code, "Native Definition migration cannot proceed.")
