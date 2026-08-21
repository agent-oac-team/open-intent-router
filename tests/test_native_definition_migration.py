import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import AgentDefinitionModel, AgentRunModel, PlanModel, PlanStepModel
from app.db.session import create_all_tables, create_session_factory
from app.repositories.database import DatabaseAgentDefinitionRepository
from app.repositories.json_utils import loads
from app.schemas.agents import AccessPolicy, AgentDefinition, InvocationSpec
from app.services.native_definition_migration import (
    NativeDefinitionMigrationError,
    NativeDefinitionMigrationService,
)


def _legacy_agent(
    *,
    agent_id: str = "legacy_lookup",
    config: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        name="Legacy lookup",
        description="Looks up an account through a trusted adapter.",
        version="legacy-v1",
        revision=4,
        enabled=True,
        type="local_function",
        access_policy=AccessPolicy(allow_roles=["operator"], allow_tenants=["tenant-a"]),
        required_inputs=["account_id"],
        input_schema={
            "type": "object",
            "required": ["account_id"],
            "properties": {"account_id": {"type": "string"}},
        },
        invocation=InvocationSpec(
            type="local_function",
            config=config if config is not None else {"function": "lookup_account"},
        ),
        metadata=metadata or {},
    )


@pytest.fixture
async def database_migration(tmp_path):
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'native-definition-migration.db'}",
    )
    await create_all_tables(settings)
    factory = create_session_factory(settings)
    return factory, NativeDefinitionMigrationService(factory)


async def _store_legacy(factory, definition: AgentDefinition) -> None:
    repository = DatabaseAgentDefinitionRepository(factory)
    await repository.upsert(definition)


async def _database_row(factory, agent_id: str) -> AgentDefinitionModel:
    async with factory() as session:
        row = await session.scalar(
            select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == agent_id)
        )
    assert row is not None
    return row


async def test_database_dry_run_reports_required_runtime_without_mutating_source(
    database_migration,
) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    before = await _database_row(factory, "legacy_lookup")

    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_database()

    assert plan.report.ready_to_migrate is True
    assert plan.report.migratable_definition_count == 1
    assert plan.report.already_v2_definition_count == 0
    assert plan.report.required_runtime_adapter_keys == ("local_function",)
    assert plan.report.required_executor_refs == ()
    after = await _database_row(factory, "legacy_lookup")
    assert after.type == before.type == "local_function"
    assert after.schema_version is None
    assert after.handling_text is None


async def test_disabled_legacy_definition_still_validates_without_a_live_binding(
    database_migration,
) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == "legacy_lookup")
        )
        assert row is not None
        row.enabled = False
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )

    plan = await migration.dry_run_database()

    assert plan.report.ready_to_migrate is True
    assert plan.report.invalid_definition_count == 0
    assert plan.report.migratable_definition_count == 1


async def test_file_prepare_installs_only_gate_support_and_does_not_create_registry_source(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gate-only.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    migration = NativeDefinitionMigrationService(factory)

    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )

    async with engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )
    await engine.dispose()
    assert "native_definition_migration_preparations" in tables
    assert "native_definition_migration_snapshots" in tables
    assert "agent_definitions" not in tables


async def test_database_migration_is_atomic_clears_legacy_fields_and_can_rollback(
    database_migration,
) -> None:
    factory, migration = database_migration
    secret_marker = "migration-secret-marker"
    await _store_legacy(
        factory,
        _legacy_agent(metadata={"api_token": secret_marker, "owner": "operations"}),
    )
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_database()

    result = await migration.migrate_database(plan, legacy_runtime_version="oir-legacy-0.1")

    assert result.applied is True
    assert result.snapshot is not None
    assert result.snapshot.source == "database"
    assert result.snapshot.migration_version == "oir-native-definition-v2-migration-v1"
    assert result.snapshot.legacy_runtime_version == "oir-legacy-0.1"
    row = await _database_row(factory, "legacy_lookup")
    assert row.schema_version == "oir-agent-v2"
    assert loads(row.handling_text, {}) == {
        "adapter_key": "local_function",
        "config": {"function": "lookup_account"},
        "kind": "invocation",
    }
    # Database creates the legacy row at revision 1; cutover advances it once.
    assert row.revision == 2
    assert row.type == ""
    assert row.invocation_text == "{}"
    assert row.ui_handoff_text == "{}"
    assert row.metadata_text == "{}"
    assert secret_marker not in "".join(
        [
            row.type,
            row.invocation_text,
            row.ui_handoff_text,
            row.metadata_text,
            row.handling_text or "",
        ]
    )

    with pytest.raises(NativeDefinitionMigrationError, match="legacy binary"):
        await migration.rollback_database(
            result.snapshot.snapshot_id,
            legacy_runtime_restored=False,
        )

    rollback = await migration.rollback_database(
        result.snapshot.snapshot_id,
        legacy_runtime_restored=True,
    )
    assert rollback.legacy_runtime_version == "oir-legacy-0.1"
    assert rollback.input_fingerprint == result.snapshot.input_fingerprint
    restored = await _database_row(factory, "legacy_lookup")
    assert restored.schema_version is None
    assert restored.handling_text is None
    assert restored.type == "local_function"
    assert loads(restored.invocation_text, {}) == {
        "config": {"function": "lookup_account"},
        "type": "local_function",
        "provider_config": {},
    }
    assert secret_marker in restored.metadata_text

    with pytest.raises(NativeDefinitionMigrationError) as unsafe_version:
        await migration.migrate_database(
            await migration.dry_run_database(),
            legacy_runtime_version="https://private.example/?token=version-secret",
        )
    assert unsafe_version.value.reason_code == "migration_legacy_runtime_version_required"


async def test_database_migration_failure_leaves_source_unchanged_and_snapshot_available(
    database_migration,
    monkeypatch,
) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_database()

    async def fail_verification(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated database write failure")

    monkeypatch.setattr(migration, "_verify_database_target", fail_verification)
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_database(plan, legacy_runtime_version="oir-legacy-0.1")

    assert raised.value.reason_code == "migration_apply_failed"
    row = await _database_row(factory, "legacy_lookup")
    assert row.schema_version is None
    assert row.type == "local_function"
    assert await migration.snapshot_count(source="database") == 1


async def test_database_rollback_refuses_source_drift_that_could_mix_releases(
    database_migration,
) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    result = await migration.migrate_database(
        await migration.dry_run_database(),
        legacy_runtime_version="oir-legacy-0.1",
    )
    assert result.snapshot is not None
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == "legacy_lookup")
        )
        assert row is not None
        row.description = "Changed after v2 cutover."

    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.rollback_database(
            result.snapshot.snapshot_id,
            legacy_runtime_restored=True,
        )

    assert raised.value.reason_code == "migration_rollback_source_changed"
    assert (await _database_row(factory, "legacy_lookup")).schema_version == "oir-agent-v2"


async def test_migration_rejects_active_legacy_plan_or_run_before_snapshot(
    database_migration,
) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    async with factory() as session:
        session.add(
            PlanModel(
                plan_id="legacy-plan",
                session_id="session-1",
                user_id="user-1",
                tenant_id="tenant-a",
                status="running",
                original_query="legacy work",
            )
        )
        session.add(
            PlanStepModel(
                plan_id="legacy-plan",
                step_id="legacy-step",
                agent_id="legacy_lookup",
                status="pending",
                description="Legacy step",
            )
        )
        session.add(
            AgentRunModel(
                run_id="legacy-run",
                session_id="session-1",
                agent_id="legacy_lookup",
                status="running",
                invoker_type="local_function",
                input_text="{}",
            )
        )
        await session.commit()

    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_database()

    assert plan.report.ready_to_migrate is False
    assert plan.report.active_legacy_plan_count == 1
    assert plan.report.active_legacy_run_count == 1
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_database(plan, legacy_runtime_version="oir-legacy-0.1")
    assert raised.value.reason_code == "migration_drain_required"
    assert await migration.snapshot_count(source="database") == 0
    assert (await _database_row(factory, "legacy_lookup")).schema_version is None


async def test_dry_run_redacts_invalid_legacy_configuration_and_has_no_side_effect(
    database_migration,
) -> None:
    factory, migration = database_migration
    secret_marker = "legacy-secret-marker"
    await _store_legacy(
        factory,
        _legacy_agent(config={"headers": {"authorization": secret_marker}}),
    )
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )

    plan = await migration.dry_run_database()

    assert plan.report.ready_to_migrate is False
    assert plan.report.invalid_definition_count == 1
    assert plan.report.issues[0].reason_code == "legacy_invocation_configuration_invalid"
    assert secret_marker not in json.dumps(plan.report.to_safe_payload())
    row = await _database_row(factory, "legacy_lookup")
    assert row.schema_version is None
    assert secret_marker in row.invocation_text


async def test_database_rerun_is_a_safe_noop_after_successful_migration(database_migration) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    first = await migration.migrate_database(
        await migration.dry_run_database(),
        legacy_runtime_version="oir-legacy-0.1",
    )
    second_plan = await migration.dry_run_database()
    second = await migration.migrate_database(
        second_plan,
        legacy_runtime_version="oir-legacy-0.1",
    )

    assert first.applied is True
    assert second.applied is False
    assert second.snapshot is None
    assert second_plan.report.already_v2_definition_count == 1
    assert second_plan.report.migratable_definition_count == 0
    assert (await _database_row(factory, "legacy_lookup")).revision == 2


def _legacy_file_document() -> dict[str, object]:
    return {
        "agents": [
            {
                "agent_id": "file_lookup",
                "name": "File lookup",
                "description": "Looks up a file-backed definition.",
                "version": "legacy-v1",
                "revision": 2,
                "enabled": True,
                "type": "local_function",
                "access_policy": {"allow_roles": ["operator"], "allow_tenants": ["tenant-a"]},
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object", "properties": {}},
                "invocation": {
                    "type": "local_function",
                    "config": {"function": "lookup_account"},
                    "provider_config": {},
                },
                "ui_handoff": {"mode": "none", "params": {}},
                "metadata": {"token": "file-secret-marker"},
            },
            {
                "schema_version": "oir-agent-v2",
                "agent_id": "external_work",
                "name": "External work",
                "description": "Already prepared for the Host executor.",
                "revision": 7,
                "enabled": True,
                "access_policy": {"allow_roles": ["operator"], "allow_tenants": ["tenant-a"]},
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object", "properties": {}},
                "handling": {
                    "kind": "external_execution",
                    "executor_ref": "host_executor",
                    "params": {"task": "execute"},
                },
            },
        ],
        "registry_label": "local-test",
    }


async def test_file_migration_is_atomic_permissioned_and_can_rollback(
    database_migration,
    tmp_path,
) -> None:
    _factory, migration = database_migration
    registry_file = tmp_path / "agents.yaml"
    registry_file.write_text(
        yaml.safe_dump(_legacy_file_document(), allow_unicode=True), encoding="utf-8"
    )
    original = registry_file.read_text(encoding="utf-8")
    snapshot_dir = tmp_path / "snapshots"

    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_file(registry_file)
    assert plan.report.ready_to_migrate is True
    assert plan.report.required_runtime_adapter_keys == ("local_function",)
    assert plan.report.required_executor_refs == ("host_executor",)
    assert registry_file.read_text(encoding="utf-8") == original

    result = await migration.migrate_file(
        plan,
        snapshot_directory=snapshot_dir,
        legacy_runtime_version="oir-legacy-0.1",
    )

    assert result.applied is True
    assert result.snapshot is not None
    assert result.snapshot.location is not None
    assert result.snapshot.location.stat().st_mode & 0o077 == 0
    migrated = yaml.safe_load(registry_file.read_text(encoding="utf-8"))
    first = migrated["agents"][0]
    assert first["schema_version"] == "oir-agent-v2"
    assert first["handling"] == {
        "adapter_key": "local_function",
        "config": {"function": "lookup_account"},
        "kind": "invocation",
    }
    assert "type" not in first
    assert "invocation" not in first
    assert "ui_handoff" not in first
    assert "metadata" not in first
    assert "file-secret-marker" not in registry_file.read_text(encoding="utf-8")

    with pytest.raises(NativeDefinitionMigrationError):
        await migration.rollback_file(result.snapshot.location, legacy_runtime_restored=False)
    await migration.rollback_file(result.snapshot.location, legacy_runtime_restored=True)
    assert registry_file.read_text(encoding="utf-8") == original


async def test_file_write_failure_does_not_replace_source(
    database_migration,
    monkeypatch,
    tmp_path,
) -> None:
    _factory, migration = database_migration
    registry_file = tmp_path / "agents.json"
    registry_file.write_text(json.dumps(_legacy_file_document()), encoding="utf-8")
    original = registry_file.read_text(encoding="utf-8")
    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_file(registry_file)

    def fail_source_replace(source: Path, *_args, **_kwargs) -> None:
        if source == registry_file:
            raise OSError("simulated file write failure")

    monkeypatch.setattr(migration, "_before_file_replace", fail_source_replace)
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_file(
            plan,
            snapshot_directory=tmp_path / "snapshots",
            legacy_runtime_version="oir-legacy-0.1",
        )

    assert raised.value.reason_code == "migration_apply_failed"
    assert registry_file.read_text(encoding="utf-8") == original


async def test_file_interruption_after_atomic_replace_restores_original_source(
    database_migration,
    monkeypatch,
    tmp_path,
) -> None:
    _factory, migration = database_migration
    registry_file = tmp_path / "agents.yaml"
    registry_file.write_text(
        yaml.safe_dump(_legacy_file_document(), allow_unicode=True), encoding="utf-8"
    )
    original = registry_file.read_text(encoding="utf-8")
    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_file(registry_file)
    write_atomically = migration._write_file_atomically

    def replace_then_interrupt(
        source: Path,
        contents: str,
        mode: int,
        *,
        invoke_before_replace: bool = True,
    ) -> None:
        write_atomically(
            source,
            contents,
            mode,
            invoke_before_replace=invoke_before_replace,
        )
        if source == registry_file and invoke_before_replace:
            raise OSError("simulated interruption after atomic replace")

    monkeypatch.setattr(migration, "_write_file_atomically", replace_then_interrupt)
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_file(
            plan,
            snapshot_directory=tmp_path / "snapshots",
            legacy_runtime_version="oir-legacy-0.1",
        )

    assert raised.value.reason_code == "migration_apply_failed"
    assert registry_file.read_text(encoding="utf-8") == original


async def test_file_rollback_refuses_source_drift_that_could_mix_releases(
    database_migration,
    tmp_path,
) -> None:
    _factory, migration = database_migration
    registry_file = tmp_path / "agents.yaml"
    registry_file.write_text(
        yaml.safe_dump(_legacy_file_document(), allow_unicode=True), encoding="utf-8"
    )
    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    result = await migration.migrate_file(
        await migration.dry_run_file(registry_file),
        snapshot_directory=tmp_path / "snapshots",
        legacy_runtime_version="oir-legacy-0.1",
    )
    assert result.snapshot is not None
    assert result.snapshot.location is not None
    migrated = registry_file.read_text(encoding="utf-8")
    registry_file.write_text(f"{migrated}# source drift\n", encoding="utf-8")

    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.rollback_file(
            result.snapshot.location,
            legacy_runtime_restored=True,
        )

    assert raised.value.reason_code == "migration_rollback_source_changed"
    assert registry_file.read_text(encoding="utf-8") == f"{migrated}# source drift\n"


async def test_cli_returns_only_safe_dry_run_evidence_for_invalid_source(
    database_migration,
) -> None:
    factory, _migration = database_migration
    secret_marker = "cli-secret-marker"
    await _store_legacy(
        factory,
        _legacy_agent(config={"headers": {"authorization": secret_marker}}),
    )
    database_url = str(factory.kw["bind"].url)
    root = Path(__file__).resolve().parents[1]
    command_prefix = [
        sys.executable,
        "scripts/migrate_native_definitions.py",
        "--database-url",
        database_url,
    ]

    prepared = subprocess.run(
        [
            *command_prefix,
            "prepare",
            "--source",
            "database",
            "--confirm-native-writes-frozen",
            "--confirm-new-execution-frozen",
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    dry_run = subprocess.run(
        [*command_prefix, "dry-run", "--source", "database"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert prepared.returncode == 0
    assert dry_run.returncode == 2
    payload = json.loads(dry_run.stdout)
    assert payload["status"] == "blocked"
    assert (
        payload["report"]["issues"][0]["reason_code"] == "legacy_invocation_configuration_invalid"
    )
    assert secret_marker not in dry_run.stdout
    assert secret_marker not in dry_run.stderr
