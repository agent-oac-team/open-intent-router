import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.models import (
    Base,
    MemoryFormationJobModel,
    MemoryFormationTurnModel,
    MemoryIndexOperationModel,
    MemoryItemModel,
    MemoryRevisionModel,
)
from app.db.session import _ensure_compatible_columns, create_all_tables, create_session_factory
from app.repositories.context_stores import DatabaseMemoryItemRepository
from app.repositories.memory_formation import DatabaseMemoryFormationTurnJobRepository
from app.repositories.memory_index_operations import DatabaseMemoryIndexOutboxRepository
from app.repositories.memory_revisions import DatabaseMemoryRevisionLedgerRepository
from app.schemas.memory import MemoryFormationTurn, MemoryIndexOperation, MemoryItem, MemoryRevision
from app.services.memory_formation import (
    FormationIdleSweeper,
    FormationTriggerCoordinator,
)


def _postgresql_url() -> str:
    value = os.getenv("OIR_TEST_POSTGRESQL_URL") or dotenv_values(".env").get("DATABASE_URL")
    if not isinstance(value, str) or not value.startswith(
        ("postgresql://", "postgresql+asyncpg://")
    ):
        pytest.skip("real PostgreSQL integration URL is not configured")
    return value


async def test_real_postgresql_legacy_memory_migration_is_rollback_safe_and_idempotent() -> None:
    database_url = _postgresql_url().replace("postgresql://", "postgresql+asyncpg://", 1)
    schema_name = f"oir_memory_migration_{uuid4().hex}"
    quoted_schema = f'"{schema_name}"'
    admin_engine = create_async_engine(database_url)
    scoped_engine = None

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        scoped_engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema_name}},
        )
        async with scoped_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    CREATE TABLE memory_items (
                        memory_id VARCHAR(128) PRIMARY KEY,
                        scope VARCHAR(64) NOT NULL,
                        subject_type VARCHAR(64) NOT NULL,
                        subject_id VARCHAR(128) NOT NULL,
                        user_id VARCHAR(128),
                        tenant_id VARCHAR(128),
                        agent_id VARCHAR(128),
                        content TEXT NOT NULL,
                        structured_value_text TEXT NOT NULL,
                        source VARCHAR(64) NOT NULL,
                        confidence INTEGER NOT NULL,
                        importance INTEGER NOT NULL,
                        visibility VARCHAR(32) NOT NULL,
                        ttl_expires_at TIMESTAMP WITH TIME ZONE,
                        metadata_text TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO memory_items (
                        memory_id, scope, subject_type, subject_id, user_id, tenant_id,
                        content, structured_value_text, source, confidence, importance,
                        visibility, metadata_text, created_at, updated_at
                    ) VALUES (
                        'legacy_ready', 'user_preference', 'user', 'legacy_user',
                        'legacy_user', 'legacy_tenant', 'prefers concise answers', '{}',
                        'manual', 95, 50, 'user', '{"mem0_memory_id":"mem0_legacy"}',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )

        with pytest.raises(RuntimeError, match="forced migration failure"):
            async with scoped_engine.begin() as conn:
                await conn.run_sync(_run_schema_migration)
                raise RuntimeError("forced migration failure")

        async with scoped_engine.connect() as conn:
            rolled_back = await conn.run_sync(
                lambda sync_conn: {
                    "tables": set(inspect(sync_conn).get_table_names()),
                    "columns": {
                        column["name"] for column in inspect(sync_conn).get_columns("memory_items")
                    },
                }
            )
        assert "memory_revisions" not in rolled_back["tables"]
        assert "lifecycle_status" not in rolled_back["columns"]
        assert "current_revision_id" not in rolled_back["columns"]

        for _ in range(3):
            async with scoped_engine.begin() as conn:
                await conn.run_sync(_run_schema_migration)

        async with scoped_engine.connect() as conn:
            migrated_columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]: column
                    for column in inspect(sync_conn).get_columns("memory_items")
                }
            )
            legacy = (
                (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_key, current_revision_id, lifecycle_status, index_status
                            FROM memory_items
                            WHERE memory_id = 'legacy_ready'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )
            legacy_revision_count = await conn.scalar(
                text("SELECT count(*) FROM memory_revisions WHERE memory_id = 'legacy_ready'")
            )

        lifecycle_columns = {
            "memory_key",
            "candidate_hash",
            "current_revision_id",
            "formation_job_id",
            "lifecycle_status",
            "index_status",
            "canonical_refs_text",
        }
        assert lifecycle_columns <= migrated_columns.keys()
        assert all(migrated_columns[name]["nullable"] for name in lifecycle_columns)
        assert legacy == {
            "memory_key": "legacy:legacy_tenant:legacy_ready",
            "current_revision_id": "mrev_legacy_legacy_ready",
            "lifecycle_status": "active",
            "index_status": "ready",
        }
        assert legacy_revision_count == 1

        async with scoped_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO memory_items (
                        memory_id, scope, subject_type, subject_id, user_id, tenant_id,
                        content, structured_value_text, source, confidence, importance,
                        visibility, metadata_text, created_at, updated_at
                    ) VALUES (
                        'old_writer_pending', 'stable_fact', 'user', 'legacy_user',
                        'legacy_user', 'legacy_tenant', 'uses the compact layout', '{}',
                        'manual', 90, 50, 'user', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        async with scoped_engine.connect() as conn:
            before_recovery = (
                (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_key, current_revision_id, lifecycle_status, index_status
                            FROM memory_items
                            WHERE memory_id = 'old_writer_pending'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert all(value is None for value in before_recovery.values())

        for _ in range(2):
            async with scoped_engine.begin() as conn:
                await conn.run_sync(_run_schema_migration)

        async with scoped_engine.connect() as conn:
            recovered = (
                (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_key, current_revision_id, lifecycle_status, index_status
                            FROM memory_items
                            WHERE memory_id = 'old_writer_pending'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )
            revision_counts = dict(
                (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_id, count(*) AS revision_count
                            FROM memory_revisions
                            WHERE memory_id IN ('legacy_ready', 'old_writer_pending')
                            GROUP BY memory_id
                            """
                        )
                    )
                ).all()
            )
        assert recovered == {
            "memory_key": "legacy:legacy_tenant:old_writer_pending",
            "current_revision_id": "mrev_legacy_old_writer_pending",
            "lifecycle_status": "active",
            "index_status": "pending",
        }
        assert revision_counts == {"legacy_ready": 1, "old_writer_pending": 1}
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        await admin_engine.dispose()


def _run_schema_migration(sync_conn) -> None:
    Base.metadata.create_all(sync_conn)
    _ensure_compatible_columns(sync_conn)


async def test_real_postgresql_multi_worker_formation_revision_and_outbox() -> None:
    suffix = uuid4().hex
    tenant_id = f"pg_acceptance_tenant_{suffix}"
    user_id = f"pg_acceptance_user_{suffix}"
    session_id = f"pg_acceptance_session_{suffix}"
    settings = Settings(
        storage_backend="database",
        database_url=_postgresql_url(),
        memory_formation_mode="observe",
        memory_formation_window_turns=5,
        memory_formation_idle_seconds=30,
        memory_formation_model_timeout_seconds=1,
        memory_formation_lease_seconds=5,
    )
    await create_all_tables(settings)
    first_factory = create_session_factory(settings)
    second_factory = create_session_factory(settings)
    first = DatabaseMemoryFormationTurnJobRepository(first_factory)
    second = DatabaseMemoryFormationTurnJobRepository(second_factory)
    coordinator = FormationTriggerCoordinator(settings=settings, repository=first)
    base = datetime(2026, 7, 14, 5, tzinfo=UTC)

    try:
        for index in range(1, 5):
            await coordinator.append_and_check(
                _turn(
                    index,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    completed_at=base + timedelta(seconds=index),
                )
            )
        old_deadline = base + timedelta(seconds=34)
        await asyncio.gather(
            coordinator.append_and_check(
                _turn(
                    5,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    completed_at=base + timedelta(seconds=5),
                )
            ),
            FormationIdleSweeper(settings=settings, repository=second).run_once(now=old_deadline),
        )
        claims = await asyncio.gather(
            first.claim_job(owner="pg-worker-a", now=old_deadline, lease_seconds=5),
            second.claim_job(owner="pg-worker-b", now=old_deadline, lease_seconds=5),
        )
        assert sum(claim is not None for claim in claims) == 1
        async with first_factory() as session:
            job_count = await session.scalar(
                select(func.count())
                .select_from(MemoryFormationJobModel)
                .where(MemoryFormationJobModel.tenant_id == tenant_id)
            )
        assert job_count == 1

        items = DatabaseMemoryItemRepository(first_factory)
        revisions = DatabaseMemoryRevisionLedgerRepository(first_factory)
        memory_id = f"pg_memory_{suffix}"
        item = MemoryItem(
            memory_id=memory_id,
            memory_key=f"tenant:{tenant_id}:user:{user_id}:preference:response_style",
            scope="user_preference",
            subject_id=user_id,
            user_id=user_id,
            tenant_id=tenant_id,
            content="initial",
        )
        await items.add(item)
        first_revision, _ = await revisions.append_revision_and_set_current(
            _revision(memory_id, item.memory_key or "", "initial", f"pg_rev_1_{suffix}"),
            tenant_id=tenant_id,
            user_id=user_id,
            subject_type="user",
            subject_id=user_id,
            expected_current_revision_id=None,
        )
        revision_attempts = await asyncio.gather(
            revisions.append_revision_and_set_current(
                _revision(memory_id, item.memory_key or "", "value-a", f"pg_rev_2a_{suffix}"),
                tenant_id=tenant_id,
                user_id=user_id,
                subject_type="user",
                subject_id=user_id,
                expected_current_revision_id=first_revision.revision_id,
            ),
            revisions.append_revision_and_set_current(
                _revision(memory_id, item.memory_key or "", "value-b", f"pg_rev_2b_{suffix}"),
                tenant_id=tenant_id,
                user_id=user_id,
                subject_type="user",
                subject_id=user_id,
                expected_current_revision_id=first_revision.revision_id,
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(value, Exception) for value in revision_attempts) == 1
        assert sum(isinstance(value, ValueError) for value in revision_attempts) == 1
        assert [
            value.revision_no
            for value in await revisions.list_for_memory(
                memory_id,
                tenant_id=tenant_id,
                user_id=user_id,
                subject_type="user",
                subject_id=user_id,
            )
        ] == [1, 2]

        outbox_a = DatabaseMemoryIndexOutboxRepository(first_factory)
        outbox_b = DatabaseMemoryIndexOutboxRepository(second_factory)
        for index in (1, 2):
            await outbox_a.add(
                MemoryIndexOperation(
                    index_operation_id=f"pg_outbox_{index}_{suffix}",
                    idempotency_key=f"pg-outbox:{suffix}:{index}",
                    operation="add",
                    memory_id=f"pg_outbox_memory_{index}_{suffix}",
                    revision_id=f"pg_outbox_revision_{index}_{suffix}",
                    tenant_id=tenant_id,
                )
            )
        outbox_claims = await asyncio.gather(
            outbox_a.claim(
                owner="pg-outbox-a",
                now=old_deadline,
                lease_seconds=5,
                tenant_id=tenant_id,
            ),
            outbox_b.claim(
                owner="pg-outbox-b",
                now=old_deadline,
                lease_seconds=5,
                tenant_id=tenant_id,
            ),
        )
        assert all(claim is not None for claim in outbox_claims)
        assert len({claim.index_operation_id for claim in outbox_claims if claim}) == 2
    finally:
        await _cleanup(first_factory, tenant_id=tenant_id)
        await first_factory.kw["bind"].dispose()
        await second_factory.kw["bind"].dispose()


def _turn(
    index: int,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    completed_at: datetime,
) -> MemoryFormationTurn:
    return MemoryFormationTurn(
        turn_id=f"pg_turn_{index}_{session_id}",
        request_id=f"pg_request_{index}_{session_id}",
        session_id=session_id,
        run_id=f"pg_run_{index}_{session_id}",
        user_id=user_id,
        tenant_id=tenant_id,
        user_text=f"user {index}",
        assistant_text=f"assistant {index}",
        result_status="completed",
        completed_at=completed_at,
    )


def _revision(memory_id: str, memory_key: str, content: str, revision_id: str) -> MemoryRevision:
    return MemoryRevision(
        revision_id=revision_id,
        memory_id=memory_id,
        revision_no=1,
        memory_key=memory_key,
        operation="add" if content == "initial" else "update",
        content=content,
        policy_version="pg-acceptance-v1",
    )


async def _cleanup(session_factory, *, tenant_id: str) -> None:
    async with session_factory() as session:
        memory_ids = select(MemoryItemModel.memory_id).where(MemoryItemModel.tenant_id == tenant_id)
        await session.execute(
            delete(MemoryRevisionModel).where(MemoryRevisionModel.memory_id.in_(memory_ids))
        )
        for model in (
            MemoryIndexOperationModel,
            MemoryFormationJobModel,
            MemoryFormationTurnModel,
            MemoryItemModel,
        ):
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.commit()
