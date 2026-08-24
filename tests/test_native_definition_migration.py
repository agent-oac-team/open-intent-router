import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSessionTransaction

import app.services.native_definition_migration as native_definition_migration
from app.core.config import Settings
from app.core.errors import NativeDefinitionMigrationFrozenError
from app.db.models import (
    AgentDefinitionModel,
    AgentRunModel,
    NativeDefinitionMigrationPreparationModel,
    PlanModel,
    PlanStepModel,
)
from app.repositories.database import DatabaseAgentDefinitionRepository, DatabaseRunRepository
from app.repositories.json_utils import loads
from app.schemas.agents import AccessPolicy, AgentDefinition, InvocationSpec
from app.schemas.logs import AgentRun
from app.services.native_definition_migration import (
    NativeDefinitionMigrationError,
    NativeDefinitionMigrationService,
    NativeDefinitionMigrationTargetCapabilities,
)
from tests.support.database import raw_engine_scope


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
async def database_migration(tmp_path, managed_database):
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'native-definition-migration.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    return factory, NativeDefinitionMigrationService(
        factory,
        target_capabilities=_target_capabilities(),
    )


def _target_capabilities(
    *,
    runtime_adapters: list[dict[str, object]] | None = None,
    executor_refs: list[str] | None = None,
) -> NativeDefinitionMigrationTargetCapabilities:
    return NativeDefinitionMigrationTargetCapabilities.from_manifest(
        {
            "contract": "oir-native-definition-target-capabilities-v1",
            "runtime_adapters": runtime_adapters
            if runtime_adapters is not None
            else [
                {
                    "adapter_key": "local_function",
                    "invocation": True,
                    "v2_invocation": True,
                    "config_schema": {"type": "object"},
                }
            ],
            "executor_refs": executor_refs if executor_refs is not None else ["host_executor"],
        }
    )


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


async def test_enabled_binding_requires_a_verified_target_capability_manifest(
    database_migration,
) -> None:
    factory, _migration = database_migration
    migration = NativeDefinitionMigrationService(factory)
    await _store_legacy(factory, _legacy_agent())
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )

    plan = await migration.dry_run_database()

    assert plan.report.ready_to_migrate is False
    assert plan.report.required_runtime_adapter_keys == ("local_function",)
    assert plan.report.issues[0].reason_code == "migration_target_capabilities_required"


async def test_capability_manifest_rejects_incompatible_or_unsupported_target_binding(
    database_migration,
) -> None:
    factory, _migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    migration = NativeDefinitionMigrationService(
        factory,
        target_capabilities=_target_capabilities(
            runtime_adapters=[
                {
                    "adapter_key": "local_function",
                    "invocation": True,
                    "v2_invocation": False,
                    "config_schema": {"type": "object"},
                }
            ]
        ),
    )
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )

    plan = await migration.dry_run_database()

    assert plan.report.ready_to_migrate is False
    assert plan.report.issues[0].reason_code == "runtime_adapter_capability_incompatible"


async def test_prepare_enforces_database_native_write_and_new_execution_fence(
    database_migration,
) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )

    with pytest.raises(NativeDefinitionMigrationFrozenError):
        await DatabaseAgentDefinitionRepository(factory).set_enabled("legacy_lookup", False)
    with pytest.raises(NativeDefinitionMigrationFrozenError):
        await DatabaseRunRepository(factory).add_run(
            AgentRun(
                run_id="fenced-run",
                session_id="session-1",
                agent_id="legacy_lookup",
                status="running",
                invoker_type="local_function",
            )
        )

    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "database")
    assert preparation is not None and preparation.active is True
    with pytest.raises(NativeDefinitionMigrationError) as overlapping_window:
        await migration.prepare(
            source="file",
            native_writes_frozen=True,
            new_execution_frozen=True,
        )
    assert overlapping_window.value.reason_code == "migration_preparation_conflict"


async def test_file_prepare_installs_only_gate_support_and_does_not_create_registry_source(
    tmp_path,
) -> None:
    async with raw_engine_scope(f"sqlite+aiosqlite:///{tmp_path / 'gate-only.db'}") as engine:
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
            legacy_runtime_version=f"ghp_{'a' * 36}",
        )
    assert unsafe_version.value.reason_code == "migration_legacy_runtime_version_required"
    with pytest.raises(NativeDefinitionMigrationError) as arbitrary_version:
        await migration.migrate_database(
            await migration.dry_run_database(),
            legacy_runtime_version="oir-legacy-password-secret-marker",
        )
    assert arbitrary_version.value.reason_code == "migration_legacy_runtime_version_required"

    script_path = Path(__file__).resolve().parents[1] / "scripts/migrate_native_definitions.py"
    spec = importlib.util.spec_from_file_location("native_definition_migration_cli", script_path)
    assert spec is not None
    assert spec.loader is not None
    cli_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_module)
    assert "legacy_runtime_version" not in cli_module._snapshot_payload(result.snapshot)


async def test_database_rollback_transaction_exit_ack_loss_recovers_from_legacy_state(
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
    migrated = await migration.migrate_database(
        await migration.dry_run_database(),
        legacy_runtime_version="oir-legacy-0.1",
    )
    assert migrated.snapshot is not None

    original_deactivate = migration._deactivate_preparation
    original_exit = AsyncSessionTransaction.__aexit__
    ready_to_lose_ack = False

    def mark_rollback_ready(preparation) -> None:
        nonlocal ready_to_lose_ack
        original_deactivate(preparation)
        if preparation.source == "database":
            ready_to_lose_ack = True

    async def lose_ack_after_real_commit(transaction, exc_type, exc_value, traceback) -> None:
        nonlocal ready_to_lose_ack
        await original_exit(transaction, exc_type, exc_value, traceback)
        if ready_to_lose_ack:
            ready_to_lose_ack = False
            raise OSError("simulated rollback commit acknowledgement loss")

    monkeypatch.setattr(migration, "_deactivate_preparation", mark_rollback_ready)
    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", lose_ack_after_real_commit)

    rollback = await migration.rollback_database(
        migrated.snapshot.snapshot_id,
        legacy_runtime_restored=True,
    )

    assert rollback.snapshot_id == migrated.snapshot.snapshot_id
    assert (await _database_row(factory, "legacy_lookup")).schema_version is None
    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "database")
    assert preparation is not None and preparation.active is False


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


async def test_database_apply_rechecks_source_after_dry_run_before_mutating(
    database_migration,
) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_database()
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == "legacy_lookup")
        )
        assert row is not None
        row.description = "Changed by a concurrent registry writer."

    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_database(plan, legacy_runtime_version="oir-legacy-0.1")

    assert raised.value.reason_code == "migration_source_changed"
    row = await _database_row(factory, "legacy_lookup")
    assert row.schema_version is None
    assert row.description == "Changed by a concurrent registry writer."


async def test_database_commit_acknowledgement_loss_recovers_from_durable_target_state(
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

    async def lose_commit_acknowledgement(*_args, **_kwargs) -> None:
        raise OSError("simulated commit acknowledgement loss")

    monkeypatch.setattr(migration, "_after_database_migration_commit", lose_commit_acknowledgement)
    result = await migration.migrate_database(
        await migration.dry_run_database(),
        legacy_runtime_version="oir-legacy-0.1",
    )

    assert result.applied is True
    assert (await _database_row(factory, "legacy_lookup")).schema_version == "oir-agent-v2"
    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "database")
    assert preparation is not None and preparation.active is False


async def test_database_transaction_exit_ack_loss_recovers_from_durable_target_state(
    database_migration,
    monkeypatch,
) -> None:
    """Exercise the real ``session.begin().__aexit__`` acknowledgement boundary."""

    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    original_verify = migration._verify_database_target
    original_exit = AsyncSessionTransaction.__aexit__
    ready_to_lose_ack = False

    async def mark_verified(*args, **kwargs) -> None:
        nonlocal ready_to_lose_ack
        await original_verify(*args, **kwargs)
        ready_to_lose_ack = True

    async def lose_ack_after_real_commit(transaction, exc_type, exc_value, traceback) -> None:
        nonlocal ready_to_lose_ack
        await original_exit(transaction, exc_type, exc_value, traceback)
        if ready_to_lose_ack:
            ready_to_lose_ack = False
            raise OSError("simulated transaction commit acknowledgement loss")

    monkeypatch.setattr(migration, "_verify_database_target", mark_verified)
    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", lose_ack_after_real_commit)

    result = await migration.migrate_database(
        await migration.dry_run_database(),
        legacy_runtime_version="oir-legacy-0.1",
    )

    assert result.applied is True
    assert (await _database_row(factory, "legacy_lookup")).schema_version == "oir-agent-v2"
    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "database")
    assert preparation is not None and preparation.active is False


async def test_unknown_database_commit_outcome_reengages_the_recovery_fence(
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

    async def lose_acknowledgement_after_target_drift(*_args, **_kwargs) -> None:
        async with factory() as session, session.begin():
            row = await session.scalar(
                select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == "legacy_lookup")
            )
            assert row is not None
            row.description = "Uncertain post-commit source state."
        raise OSError("simulated acknowledgement loss after target drift")

    monkeypatch.setattr(
        migration,
        "_after_database_migration_commit",
        lose_acknowledgement_after_target_drift,
    )
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_database(
            await migration.dry_run_database(),
            legacy_runtime_version="oir-legacy-0.1",
        )

    assert raised.value.reason_code == "migration_commit_outcome_unknown"
    fresh_plan = await migration.dry_run_database()
    assert fresh_plan.report.migratable_definition_count == 0
    with pytest.raises(NativeDefinitionMigrationError) as fresh_rerun:
        await migration.migrate_database(
            fresh_plan,
            legacy_runtime_version="oir-legacy-0.1",
        )
    assert fresh_rerun.value.reason_code == "migration_recovery_target_mismatch"
    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "database")
    assert preparation is not None and preparation.active is True


async def test_database_restart_recovers_snapshot_and_can_rollback(
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

    restarted = NativeDefinitionMigrationService(
        factory,
        target_capabilities=_target_capabilities(),
    )
    restored = await restarted.rollback_database(
        result.snapshot.snapshot_id,
        legacy_runtime_restored=True,
    )

    assert restored.snapshot_id == result.snapshot.snapshot_id
    assert (await _database_row(factory, "legacy_lookup")).schema_version is None


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


async def test_database_dry_run_reports_malformed_source_json_with_a_safe_code(
    database_migration,
) -> None:
    factory, migration = database_migration
    secret_marker = "database-json-secret-marker"
    await _store_legacy(factory, _legacy_agent())
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == "legacy_lookup")
        )
        assert row is not None
        row.invocation_text = f'{{"authorization":"{secret_marker}"'
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )

    plan = await migration.dry_run_database()

    assert plan.report.ready_to_migrate is False
    assert plan.report.issues[0].reason_code == "database_definition_serialization_invalid"
    assert secret_marker not in json.dumps(plan.report.to_safe_payload())
    assert (await _database_row(factory, "legacy_lookup")).invocation_text.endswith('"')


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


async def test_database_noop_rejects_invalid_v2_source_and_keeps_maintenance_fence(
    database_migration,
) -> None:
    factory, migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    await migration.migrate_database(
        await migration.dry_run_database(),
        legacy_runtime_version="oir-legacy-0.1",
    )
    await migration.prepare(
        source="database",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    # Simulate an out-of-band corruption after a release was prepared.  This
    # is intentionally a direct model write: the application repository is
    # correctly fenced while the maintenance window is active.
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == "legacy_lookup")
        )
        assert row is not None
        row.handling_text = "{}"

    plan = await migration.dry_run_database()

    assert plan.report.migratable_definition_count == 0
    assert plan.report.issues
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_database(
            plan,
            legacy_runtime_version="oir-legacy-0.1",
        )

    assert raised.value.reason_code == "migration_validation_failed"
    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "database")
    assert preparation is not None and preparation.active is True


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


async def test_file_migration_transaction_exit_ack_loss_recovers_from_durable_target_state(
    database_migration,
    monkeypatch,
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
    plan = await migration.dry_run_file(registry_file)
    original_deactivate = migration._deactivate_preparation
    original_exit = AsyncSessionTransaction.__aexit__
    ready_to_lose_ack = False

    def mark_migration_ready(preparation) -> None:
        nonlocal ready_to_lose_ack
        original_deactivate(preparation)
        if preparation.source == "file":
            ready_to_lose_ack = True

    async def lose_ack_after_real_commit(transaction, exc_type, exc_value, traceback) -> None:
        nonlocal ready_to_lose_ack
        await original_exit(transaction, exc_type, exc_value, traceback)
        if ready_to_lose_ack:
            ready_to_lose_ack = False
            raise OSError("simulated file migration acknowledgement loss")

    monkeypatch.setattr(migration, "_deactivate_preparation", mark_migration_ready)
    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", lose_ack_after_real_commit)

    result = await migration.migrate_file(
        plan,
        snapshot_directory=tmp_path / "snapshots",
        legacy_runtime_version="oir-legacy-0.1",
    )

    assert result.applied is True
    assert (
        yaml.safe_load(registry_file.read_text(encoding="utf-8"))["agents"][0]["schema_version"]
        == "oir-agent-v2"
    )


async def test_file_rollback_transaction_exit_ack_loss_recovers_from_legacy_state(
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
    migrated = await migration.migrate_file(
        await migration.dry_run_file(registry_file),
        snapshot_directory=tmp_path / "snapshots",
        legacy_runtime_version="oir-legacy-0.1",
    )
    assert migrated.snapshot is not None
    assert migrated.snapshot.location is not None

    original_deactivate = migration._deactivate_preparation
    original_exit = AsyncSessionTransaction.__aexit__
    ready_to_lose_ack = False

    def mark_rollback_ready(preparation) -> None:
        nonlocal ready_to_lose_ack
        original_deactivate(preparation)
        if preparation.source == "file":
            ready_to_lose_ack = True

    async def lose_ack_after_real_commit(transaction, exc_type, exc_value, traceback) -> None:
        nonlocal ready_to_lose_ack
        await original_exit(transaction, exc_type, exc_value, traceback)
        if ready_to_lose_ack:
            ready_to_lose_ack = False
            raise OSError("simulated file rollback acknowledgement loss")

    monkeypatch.setattr(migration, "_deactivate_preparation", mark_rollback_ready)
    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", lose_ack_after_real_commit)

    rollback = await migration.rollback_file(
        migrated.snapshot.location,
        legacy_runtime_restored=True,
    )

    assert rollback.snapshot_id == migrated.snapshot.snapshot_id
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


async def test_file_replace_directory_sync_failure_restores_source_before_reporting_failure(
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
    snapshot_directory = tmp_path / "snapshots"
    snapshot_directory.mkdir()
    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_file(registry_file)
    original_fsync_directory = native_definition_migration._fsync_directory
    failed_once = False

    def fail_once_after_registry_replace(directory: Path) -> None:
        nonlocal failed_once
        if directory == registry_file.parent and not failed_once:
            failed_once = True
            raise NativeDefinitionMigrationError("migration_file_sync_failed")
        original_fsync_directory(directory)

    monkeypatch.setattr(
        native_definition_migration,
        "_fsync_directory",
        fail_once_after_registry_replace,
    )
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_file(
            plan,
            snapshot_directory=snapshot_directory,
            legacy_runtime_version="oir-legacy-0.1",
        )

    assert raised.value.reason_code == "migration_file_sync_failed"
    assert registry_file.read_text(encoding="utf-8") == original
    assert failed_once is True


async def test_file_noop_restart_recovery_syncs_parent_after_replace_interrupt(
    database_migration,
    monkeypatch,
    tmp_path,
) -> None:
    factory, migration = database_migration
    registry_file = tmp_path / "agents.yaml"
    registry_file.write_text(
        yaml.safe_dump(_legacy_file_document(), allow_unicode=True), encoding="utf-8"
    )
    snapshot_directory = tmp_path / "snapshots"
    snapshot_directory.mkdir()
    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_file(registry_file)
    original_fsync_directory = native_definition_migration._fsync_directory

    class SimulatedProcessInterruption(BaseException):
        pass

    def interrupt_after_replace(directory: Path) -> None:
        if directory == registry_file.parent:
            raise SimulatedProcessInterruption()
        original_fsync_directory(directory)

    monkeypatch.setattr(
        native_definition_migration,
        "_fsync_directory",
        interrupt_after_replace,
    )
    with pytest.raises(SimulatedProcessInterruption):
        await migration.migrate_file(
            plan,
            snapshot_directory=snapshot_directory,
            legacy_runtime_version="oir-legacy-0.1",
        )

    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "file")
    assert preparation is not None and preparation.active is True
    assert "schema_version: oir-agent-v2" in registry_file.read_text(encoding="utf-8")

    parent_synced = False

    def record_restart_recovery_sync(directory: Path) -> None:
        nonlocal parent_synced
        if directory == registry_file.parent:
            parent_synced = True
        original_fsync_directory(directory)

    monkeypatch.setattr(
        native_definition_migration,
        "_fsync_directory",
        record_restart_recovery_sync,
    )
    restarted = NativeDefinitionMigrationService(
        factory,
        target_capabilities=_target_capabilities(),
    )
    rerun = await restarted.migrate_file(
        await restarted.dry_run_file(registry_file),
        snapshot_directory=snapshot_directory,
        legacy_runtime_version="oir-legacy-0.1",
    )

    assert rerun.applied is False
    assert parent_synced is True
    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "file")
    assert preparation is not None and preparation.active is False


async def test_file_snapshot_syncs_each_new_nested_directory_ancestor(
    database_migration,
    monkeypatch,
    tmp_path,
) -> None:
    _factory, migration = database_migration
    registry_file = tmp_path / "agents.yaml"
    registry_file.write_text(
        yaml.safe_dump(_legacy_file_document(), allow_unicode=True), encoding="utf-8"
    )
    snapshot_directory = tmp_path / "private" / "rollback" / "snapshots"
    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    fsynced: list[Path] = []
    original_fsync_directory = native_definition_migration._fsync_directory

    def record_directory_sync(directory: Path) -> None:
        fsynced.append(directory)
        original_fsync_directory(directory)

    monkeypatch.setattr(
        native_definition_migration,
        "_fsync_directory",
        record_directory_sync,
    )
    result = await migration.migrate_file(
        await migration.dry_run_file(registry_file),
        snapshot_directory=snapshot_directory,
        legacy_runtime_version="oir-legacy-0.1",
    )

    assert result.snapshot is not None
    assert {
        snapshot_directory,
        snapshot_directory.parent,
        snapshot_directory.parent.parent,
    }.issubset(fsynced)


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
        expected_source_digest: str | None = None,
    ) -> None:
        write_atomically(
            source,
            contents,
            mode,
            invoke_before_replace=invoke_before_replace,
            expected_source_digest=expected_source_digest,
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


async def test_file_apply_refuses_an_edit_at_the_final_pre_replace_check(
    database_migration,
    monkeypatch,
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
    plan = await migration.dry_run_file(registry_file)

    def concurrent_edit(source: Path, _temporary_path: Path) -> None:
        source.write_text(
            f"{source.read_text(encoding='utf-8')}# concurrent source edit\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(migration, "_before_file_replace", concurrent_edit)
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_file(
            plan,
            snapshot_directory=tmp_path / "snapshots",
            legacy_runtime_version="oir-legacy-0.1",
        )

    assert raised.value.reason_code == "migration_source_changed"
    assert registry_file.read_text(encoding="utf-8").endswith("# concurrent source edit\n")
    async with _factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "file")
    assert preparation is not None and preparation.active is True


async def test_file_apply_refuses_post_replace_valid_v2_drift_and_keeps_recovery_fence(
    database_migration,
    monkeypatch,
    tmp_path,
) -> None:
    factory, migration = database_migration
    registry_file = tmp_path / "agents.yaml"
    registry_file.write_text(
        yaml.safe_dump(_legacy_file_document(), allow_unicode=True), encoding="utf-8"
    )
    await migration.prepare(
        source="file",
        native_writes_frozen=True,
        new_execution_frozen=True,
    )
    plan = await migration.dry_run_file(registry_file)

    def replace_with_other_valid_v2(source: Path) -> None:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        document["registry_label"] = "externally-replaced-v2"
        source.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    monkeypatch.setattr(migration, "_after_file_replace", replace_with_other_valid_v2)
    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.migrate_file(
            plan,
            snapshot_directory=tmp_path / "snapshots",
            legacy_runtime_version="oir-legacy-0.1",
        )

    assert raised.value.reason_code == "migration_target_validation_failed"
    assert "externally-replaced-v2" in registry_file.read_text(encoding="utf-8")
    fresh_plan = await migration.dry_run_file(registry_file)
    assert fresh_plan.report.migratable_definition_count == 0
    with pytest.raises(NativeDefinitionMigrationError) as fresh_rerun:
        await migration.migrate_file(
            fresh_plan,
            snapshot_directory=tmp_path / "snapshots",
            legacy_runtime_version="oir-legacy-0.1",
        )
    assert fresh_rerun.value.reason_code == "migration_recovery_target_mismatch"
    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "file")
    assert preparation is not None and preparation.active is True


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


async def test_file_rollback_recovers_after_replace_directory_sync_ack_loss(
    database_migration,
    monkeypatch,
    tmp_path,
) -> None:
    factory, migration = database_migration
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
    migrated = await migration.migrate_file(
        await migration.dry_run_file(registry_file),
        snapshot_directory=tmp_path / "snapshots",
        legacy_runtime_version="oir-legacy-0.1",
    )
    assert migrated.snapshot is not None
    assert migrated.snapshot.location is not None
    original_fsync_directory = native_definition_migration._fsync_directory
    failed_once = False

    def lose_replace_directory_ack_once(directory: Path) -> None:
        nonlocal failed_once
        if directory == registry_file.parent and not failed_once:
            failed_once = True
            raise NativeDefinitionMigrationError("migration_file_sync_failed")
        original_fsync_directory(directory)

    monkeypatch.setattr(
        native_definition_migration,
        "_fsync_directory",
        lose_replace_directory_ack_once,
    )

    rollback = await migration.rollback_file(
        migrated.snapshot.location,
        legacy_runtime_restored=True,
    )

    assert rollback.snapshot_id == migrated.snapshot.snapshot_id
    assert registry_file.read_text(encoding="utf-8") == original
    assert failed_once is True
    async with factory() as session:
        preparation = await session.get(NativeDefinitionMigrationPreparationModel, "file")
    assert preparation is not None and preparation.active is False


async def test_file_rollback_validates_private_snapshot_before_writing_source(
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
    snapshot_payload = json.loads(result.snapshot.location.read_text(encoding="utf-8"))
    snapshot_payload["migration_version"] = "untrusted-snapshot-version"
    result.snapshot.location.write_text(json.dumps(snapshot_payload), encoding="utf-8")

    with pytest.raises(NativeDefinitionMigrationError) as raised:
        await migration.rollback_file(
            result.snapshot.location,
            legacy_runtime_restored=True,
        )

    assert raised.value.reason_code == "migration_snapshot_invalid"
    assert registry_file.read_text(encoding="utf-8") == migrated

    snapshot_payload["migration_version"] = "oir-native-definition-v2-migration-v1"
    snapshot_payload["input_digest"] = "0" * 64
    result.snapshot.location.write_text(json.dumps(snapshot_payload), encoding="utf-8")
    with pytest.raises(NativeDefinitionMigrationError) as bad_digest:
        await migration.rollback_file(
            result.snapshot.location,
            legacy_runtime_restored=True,
        )

    assert bad_digest.value.reason_code == "migration_snapshot_invalid"
    assert registry_file.read_text(encoding="utf-8") == migrated


async def test_cli_returns_only_safe_dry_run_evidence_for_invalid_source(
    database_migration,
    tmp_path,
) -> None:
    factory, _migration = database_migration
    secret_marker = "cli-secret-marker"
    await _store_legacy(
        factory,
        _legacy_agent(config={"headers": {"authorization": secret_marker}}),
    )
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'native-definition-migration.db'}"
    root = Path(__file__).resolve().parents[1]
    command_prefix = [
        sys.executable,
        "scripts/migrate_native_definitions.py",
        "--database-url",
        database_url,
    ]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(value for value in (str(root), existing_pythonpath) if value),
    }

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
        env=environment,
        text=True,
    )
    dry_run = subprocess.run(
        [*command_prefix, "dry-run", "--source", "database"],
        cwd=root,
        capture_output=True,
        check=False,
        env=environment,
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


async def test_cli_uses_target_capability_manifest_for_a_valid_binding(
    database_migration,
    tmp_path,
) -> None:
    factory, _migration = database_migration
    await _store_legacy(factory, _legacy_agent())
    database_url = str(factory.kw["bind"].url)
    manifest = tmp_path / "target-capabilities.json"
    manifest.write_text(
        json.dumps(
            {
                "contract": "oir-native-definition-target-capabilities-v1",
                "runtime_adapters": [
                    {
                        "adapter_key": "local_function",
                        "invocation": True,
                        "v2_invocation": True,
                        "config_schema": {"type": "object"},
                    }
                ],
                "executor_refs": [],
            }
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    command_prefix = [
        sys.executable,
        "scripts/migrate_native_definitions.py",
        "--database-url",
        database_url,
    ]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(value for value in (str(root), existing_pythonpath) if value),
    }

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
        env=environment,
        text=True,
    )
    dry_run = subprocess.run(
        [
            *command_prefix,
            "dry-run",
            "--source",
            "database",
            "--target-capability-manifest",
            str(manifest),
        ],
        cwd=root,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert prepared.returncode == 0
    assert dry_run.returncode == 0
    assert json.loads(dry_run.stdout)["status"] == "ready"
