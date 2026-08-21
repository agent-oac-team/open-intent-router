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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import ValidationError
from sqlalchemy import select
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
    AgentDefinition,
    AgentDefinitionV2,
    ExternalExecutionHandling,
    InvocationHandling,
    SafeHandlingConfiguration,
    UiHandoffHandling,
    is_safe_agent_identifier,
)
from app.services.registry_snapshot import RegistrySnapshotBuilder

MigrationSource = Literal["database", "file"]

MIGRATION_VERSION = "oir-native-definition-v2-migration-v1"
_FILE_SNAPSHOT_CONTRACT = "oir-native-definition-rollback-snapshot-v1"
_DATABASE_SNAPSHOT_CONTRACT = "oir-native-definition-database-rollback-snapshot-v1"
_SAFE_LEGACY_RUNTIME_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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
class _SourceRecord:
    source_index: int
    payload: Mapping[str, object]
    storage: Mapping[str, object]
    legacy: bool

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

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def prepare(
        self,
        *,
        source: MigrationSource,
        native_writes_frozen: bool,
        new_execution_frozen: bool,
    ) -> None:
        """Record the release-window assertions required before a dry-run.

        This is audit material for an offline operation, not a request-time flag.
        Operators must stop Native writes and new execution before they assert it.
        """

        _require_source(source)
        if not native_writes_frozen or not new_execution_frozen:
            raise NativeDefinitionMigrationError("migration_preparation_required")
        await ensure_native_definition_migration_schema(self._session_factory)
        async with self._session_factory() as session, session.begin():
            preparation = await session.get(NativeDefinitionMigrationPreparationModel, source)
            if preparation is None:
                session.add(
                    NativeDefinitionMigrationPreparationModel(
                        source=source,
                        native_writes_frozen=True,
                        new_execution_frozen=True,
                        recorded_at=datetime.now(UTC),
                    )
                )
            else:
                preparation.native_writes_frozen = True
                preparation.new_execution_frozen = True
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
        records = tuple(
            _SourceRecord(
                source_index=index,
                payload={**item, "source": "file"} if isinstance(item, dict) else {},
                storage=dict(item) if isinstance(item, dict) else {},
                legacy=not (
                    isinstance(item, dict) and item.get("schema_version") == "oir-agent-v2"
                ),
            )
            for index, item in enumerate(raw_agents)
        )
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
        self._require_ready(plan)
        if plan.report.migratable_definition_count == 0:
            return NativeDefinitionMigrationResult(report=plan.report, applied=False, snapshot=None)

        snapshot = await self._create_database_snapshot(plan, legacy_runtime_version)
        try:
            async with self._session_factory() as session, session.begin():
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
        except NativeDefinitionMigrationError:
            raise
        except Exception as exc:
            raise NativeDefinitionMigrationError("migration_apply_failed") from exc
        return NativeDefinitionMigrationResult(report=plan.report, applied=True, snapshot=snapshot)

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
        self._require_ready(plan)
        if plan.report.migratable_definition_count == 0:
            return NativeDefinitionMigrationResult(report=plan.report, applied=False, snapshot=None)
        path = plan._file_path
        document = plan._file_document
        source_text = plan._file_text
        source_mode = plan._file_mode
        if path is None or document is None or source_text is None or source_mode is None:
            raise NativeDefinitionMigrationError("migration_source_invalid")

        current = await self.dry_run_file(path)
        if current._input_digest != plan._input_digest:
            raise NativeDefinitionMigrationError("migration_source_changed")
        self._require_ready(current)
        migrated_document = dict(document)
        migrated_document["agents"] = [
            _file_v2_payload(plan._target_definitions[record.agent_id])
            for record in plan._records
            if record.agent_id is not None and record.agent_id in plan._target_definitions
        ]
        migrated_contents = _serialize_document(path, migrated_document)
        snapshot = _write_file_snapshot(
            path=path,
            source_text=source_text,
            source_mode=source_mode,
            snapshot_directory=snapshot_directory,
            legacy_runtime_version=legacy_runtime_version,
            input_digest=plan._input_digest,
            input_fingerprint=plan.report.input_fingerprint,
            target_digest=_digest(migrated_contents),
            definition_count=plan.report.source_definition_count,
        )
        try:
            self._write_file_atomically(path, migrated_contents, source_mode)
            verified = await self.dry_run_file(path)
            if verified.report.issues or verified.report.migratable_definition_count:
                raise NativeDefinitionMigrationError("migration_target_validation_failed")
        except NativeDefinitionMigrationError:
            self._restore_after_file_apply_failure(snapshot.location)
            raise
        except Exception as exc:
            self._restore_after_file_apply_failure(snapshot.location)
            raise NativeDefinitionMigrationError("migration_apply_failed") from exc
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
        try:
            async with self._session_factory() as session, session.begin():
                snapshot = await session.get(NativeDefinitionMigrationSnapshotModel, snapshot_id)
                if snapshot is None or snapshot.source != "database":
                    raise NativeDefinitionMigrationError("migration_snapshot_not_found")
                payload = _load_database_snapshot_payload(snapshot.payload_text)
                records = payload["records"]
                if not isinstance(records, list):
                    raise NativeDefinitionMigrationError("migration_snapshot_invalid")
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
                return _database_snapshot_metadata(snapshot, payload)
        except NativeDefinitionMigrationError:
            raise
        except Exception as exc:
            raise NativeDefinitionMigrationError("migration_rollback_failed") from exc

    async def rollback_file(
        self,
        snapshot_location: Path,
        *,
        legacy_runtime_restored: bool,
    ) -> NativeDefinitionMigrationSnapshot:
        if not legacy_runtime_restored:
            raise NativeDefinitionMigrationError("migration_legacy_binary_restore_required")
        try:
            return self._restore_file_snapshot(snapshot_location)
        except NativeDefinitionMigrationError:
            raise
        except Exception as exc:
            raise NativeDefinitionMigrationError("migration_rollback_failed") from exc

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
            try:
                definition = _convert_record(record)
                _validate_v2_target(definition)
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
            if isinstance(definition.handling, InvocationHandling):
                adapter_keys.add(definition.handling.adapter_key)
            elif isinstance(definition.handling, ExternalExecutionHandling):
                executor_refs.add(definition.handling.executor_ref)

        active_plans, active_runs = await self._active_legacy_work(tuple(legacy_agent_ids))
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
        if preparation is None:
            return False, False
        return preparation.native_writes_frozen, preparation.new_execution_frozen

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
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "database")
        if (
            preparation is None
            or not preparation.native_writes_frozen
            or not preparation.new_execution_frozen
        ):
            raise NativeDefinitionMigrationError("migration_preparation_required")
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
            _validate_v2_target(actual)
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
            os.replace(temporary_path, path)
        except Exception:
            if temporary_path.exists():
                temporary_path.unlink()
            raise

    def _before_file_replace(self, _source: Path, _temporary_path: Path) -> None:
        """Narrow failure-injection seam for the atomic-write conformance test."""

    def _restore_file_snapshot(
        self,
        snapshot_location: Path | None,
        *,
        require_target_match: bool = True,
    ) -> NativeDefinitionMigrationSnapshot:
        if snapshot_location is None or not snapshot_location.is_file():
            raise NativeDefinitionMigrationError("migration_snapshot_not_found")
        try:
            raw_payload = json.loads(snapshot_location.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NativeDefinitionMigrationError("migration_snapshot_invalid") from exc
        if (
            not isinstance(raw_payload, dict)
            or raw_payload.get("contract") != _FILE_SNAPSHOT_CONTRACT
        ):
            raise NativeDefinitionMigrationError("migration_snapshot_invalid")
        path_value = raw_payload.get("registry_path")
        source_text = raw_payload.get("source_text")
        source_mode = raw_payload.get("source_mode")
        target_digest = raw_payload.get("target_digest")
        if (
            not isinstance(path_value, str)
            or not isinstance(source_text, str)
            or not isinstance(source_mode, int)
            or not isinstance(target_digest, str)
        ):
            raise NativeDefinitionMigrationError("migration_snapshot_invalid")
        path = Path(path_value)
        if require_target_match:
            try:
                current_digest = _digest(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise NativeDefinitionMigrationError("migration_rollback_source_changed") from exc
            if current_digest != target_digest:
                raise NativeDefinitionMigrationError("migration_rollback_source_changed")
        self._write_file_atomically(
            path,
            source_text,
            source_mode,
            invoke_before_replace=False,
        )
        return _file_snapshot_metadata(snapshot_location, raw_payload)

    def _restore_after_file_apply_failure(self, snapshot_location: Path | None) -> None:
        try:
            self._restore_file_snapshot(snapshot_location, require_target_match=False)
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
        legacy = AgentDefinition.model_validate(dict(record.payload))
    except (TypeError, ValidationError, ValueError) as exc:
        raise _DefinitionConversionError("legacy_definition_invalid") from exc
    return _legacy_to_v2(legacy)


def _legacy_to_v2(legacy: AgentDefinition) -> AgentDefinitionV2:
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


def _validate_v2_target(definition: AgentDefinitionV2) -> None:
    snapshot = RegistrySnapshotBuilder(runtime_catalog=None).build(
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


def _database_record_from_row(index: int, row: AgentDefinitionModel) -> _SourceRecord:
    storage = {name: getattr(row, name) for name in _LEGACY_DATABASE_FIELDS}
    schema_version = row.schema_version
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
    snapshot_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(snapshot_directory, 0o700)
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
    except Exception:
        if location.exists():
            location.unlink()
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
    if migration_version != MIGRATION_VERSION:
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
            _validate_v2_target(definition)
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
    if not _SAFE_LEGACY_RUNTIME_VERSION.fullmatch(value):
        raise NativeDefinitionMigrationError("migration_legacy_runtime_version_required")


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
    }
    return messages.get(reason_code, "Native Definition migration cannot proceed.")
