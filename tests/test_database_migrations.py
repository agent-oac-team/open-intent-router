from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.models import CanonicalTurnModel, MemoryFormationJobModel, TurnOutboxModel
from app.db.session import (
    _context_owner_column_definitions,
    create_all_tables,
    create_engine,
    create_session_factory,
)
from app.repositories.context_stores import (
    DatabaseKnowledgeRepository,
    DatabaseMemoryItemRepository,
)
from app.repositories.json_utils import dumps
from app.repositories.memory_formation import DatabaseMemoryFormationTurnJobRepository
from app.repositories.memory_traces import DatabaseMemoryFormationTraceRepository
from app.schemas.knowledge import KnowledgeChunk, KnowledgeRetrievalLog, KnowledgeSource
from app.schemas.memory import (
    MemoryDecisionStatus,
    MemoryEvent,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryOperation,
)


def test_postgresql_schema_targets_oir_database() -> None:
    schema_sql = Path("sql/postgresql_schema.sql").read_text(encoding="utf-8")

    assert "\\set oir_db oir" in schema_sql
    assert "\\set oir_user oir" in schema_sql
    assert "DATABASE %I OWNER %I" in schema_sql
    assert "ALTER DATABASE %I OWNER TO %I" in schema_sql
    assert "REASSIGN OWNED BY CURRENT_USER TO %I" in schema_sql
    assert "127.0.0.1:5432/oac" not in schema_sql
    assert "postgresql://oac:oac" not in schema_sql


def test_postgresql_memory_migration_is_rerunnable_and_plan_ownership_is_strict() -> None:
    schema_sql = Path("sql/postgresql_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS memory_revisions" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS memory_formation_turns" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS memory_formation_jobs" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS memory_index_operations" in schema_sql
    assert "ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS lifecycle_status" in schema_sql
    assert "ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS index_status" in schema_sql
    assert "ON CONFLICT DO NOTHING" in schema_sql
    assert "'mrev_legacy_' || memory_id" in schema_sql
    assert "ALTER TABLE plans ALTER COLUMN user_id SET NOT NULL" in schema_sql
    assert "ALTER TABLE plans ALTER COLUMN tenant_id SET NOT NULL" in schema_sql
    assert "DELETE FROM plans" in schema_sql


def test_postgresql_schema_contains_canonical_turn_and_outbox_contract() -> None:
    schema_sql = Path("sql/postgresql_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS canonical_turns" in schema_sql
    assert "CONSTRAINT uq_canonical_turns_owner_request" in schema_sql
    assert "UNIQUE (tenant_id, user_id, request_id)" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS turn_outbox" in schema_sql
    assert "CONSTRAINT uq_turn_outbox_idempotency" in schema_sql
    assert "REFERENCES canonical_turns (turn_id) ON DELETE RESTRICT" in schema_sql
    assert "idx_turn_outbox_claim" in schema_sql


def test_postgresql_schema_contains_hashed_execution_ticket_contract() -> None:
    schema_sql = Path("sql/postgresql_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS execution_tickets" in schema_sql
    assert "ticket_hash VARCHAR(64) NOT NULL" in schema_sql
    assert "claims_text TEXT NOT NULL" in schema_sql
    assert "lease_expires_at TIMESTAMP WITH TIME ZONE" in schema_sql
    assert "consumed_event_id VARCHAR(128)" in schema_sql
    assert "execution_ticket TEXT" not in schema_sql


def test_postgresql_schema_contains_registry_revision_and_audit_contract() -> None:
    schema_sql = Path("sql/postgresql_schema.sql").read_text(encoding="utf-8")

    assert "revision INTEGER DEFAULT 0 NOT NULL" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS registry_revisions" in schema_sql
    assert "operator_id VARCHAR(128) NOT NULL" in schema_sql
    assert "before_text TEXT" in schema_sql
    assert "after_text TEXT" in schema_sql


def test_postgresql_schema_contains_canonical_knowledge_asset_contract() -> None:
    schema_sql = Path("sql/postgresql_schema.sql").read_text(encoding="utf-8")

    for table in (
        "knowledge_asset_groups",
        "knowledge_assets",
        "knowledge_asset_chunks",
        "knowledge_import_jobs",
        "knowledge_migration_manifests",
        "knowledge_operation_traces",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema_sql
    assert "ON DELETE RESTRICT" in schema_sql
    assert "uq_knowledge_manifest_pipeline" in schema_sql
    assert "idx_knowledge_assets_tenant_status" in schema_sql


def test_legacy_runtime_datetime_columns_use_postgresql_type() -> None:
    postgresql = _context_owner_column_definitions("postgresql")["agent_runs"]
    sqlite = _context_owner_column_definitions("sqlite")["agent_runs"]

    for column in ("deadline_at", "heartbeat_at", "claim_expires_at"):
        assert postgresql[column] == "TIMESTAMP WITH TIME ZONE"
        assert sqlite[column] == "DATETIME"


async def test_create_all_tables_builds_canonical_turn_and_outbox_constraints(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'canonical-turn.db'}"
    settings = Settings(storage_backend="database", database_url=database_url)

    await create_all_tables(settings)

    engine = create_engine(settings)
    async with engine.begin() as conn:
        schema = await conn.run_sync(_canonical_turn_schema_snapshot)
    await engine.dispose()

    assert schema["turn_columns"] >= {
        "turn_id",
        "tenant_id",
        "user_id",
        "session_id",
        "request_id",
        "status",
        "state_version",
        "user_input_text",
        "references_text",
        "final_response_text",
        "completed_at",
    }
    assert ["tenant_id", "user_id", "request_id"] in schema["turn_unique_constraints"]
    assert ["request_id"] in schema["turn_unique_indexes"]
    assert {
        "idx_canonical_turns_owner_session_status",
        "idx_canonical_turns_status_updated",
    } <= schema["turn_indexes"]
    assert ["idempotency_key"] in schema["outbox_unique_constraints"]
    assert {"idx_turn_outbox_claim", "idx_turn_outbox_turn_status"} <= schema["outbox_indexes"]
    assert any(
        foreign_key["referred_table"] == "canonical_turns"
        and foreign_key["constrained_columns"] == ["turn_id"]
        for foreign_key in schema["outbox_foreign_keys"]
    )
    assert CanonicalTurnModel.__tablename__ == "canonical_turns"
    assert TurnOutboxModel.__tablename__ == "turn_outbox"


def _canonical_turn_schema_snapshot(sync_conn) -> dict:
    inspector = inspect(sync_conn)
    return {
        "turn_columns": {column["name"] for column in inspector.get_columns("canonical_turns")},
        "turn_unique_constraints": [
            constraint["column_names"]
            for constraint in inspector.get_unique_constraints("canonical_turns")
        ],
        "turn_indexes": {index["name"] for index in inspector.get_indexes("canonical_turns")},
        "turn_unique_indexes": [
            index["column_names"]
            for index in inspector.get_indexes("canonical_turns")
            if index.get("unique")
        ],
        "outbox_unique_constraints": [
            constraint["column_names"]
            for constraint in inspector.get_unique_constraints("turn_outbox")
        ],
        "outbox_indexes": {index["name"] for index in inspector.get_indexes("turn_outbox")},
        "outbox_foreign_keys": inspector.get_foreign_keys("turn_outbox"),
    }


async def test_create_all_tables_adds_agent_context_column_to_existing_database(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    settings = Settings(storage_backend="database", database_url=database_url)
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE agent_definitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id VARCHAR(128) UNIQUE,
                    name VARCHAR(200),
                    description TEXT,
                    type VARCHAR(64),
                    enabled BOOLEAN
                )
                """
            )
        )
    await engine.dispose()

    await create_all_tables(settings)

    engine = create_engine(settings)
    async with engine.begin() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"] for column in inspect(sync_conn).get_columns("agent_definitions")
            }
        )
    await engine.dispose()
    assert "context_text" in columns


async def test_create_all_tables_adds_delegated_run_columns_to_legacy_tables(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy-delegated.db'}"
    settings = Settings(storage_backend="database", database_url=database_url)
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE agent_runs (run_id VARCHAR(128) PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE agent_results (result_id VARCHAR(128) PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE agent_events (event_id VARCHAR(128) PRIMARY KEY)"))
    await engine.dispose()

    await create_all_tables(settings)
    await create_all_tables(settings)

    engine = create_engine(settings)
    async with engine.begin() as conn:
        schema = await conn.run_sync(
            lambda sync_conn: {
                table: {column["name"] for column in inspect(sync_conn).get_columns(table)}
                for table in ("agent_runs", "agent_results", "agent_events")
            }
        )
    await engine.dispose()

    assert schema["agent_runs"] >= {
        "turn_id",
        "delegated",
        "state_version",
        "deadline_at",
        "heartbeat_at",
        "claim_owner",
        "claim_token",
        "claim_expires_at",
        "terminal_event_id",
    }
    assert schema["agent_results"] >= {"turn_id", "run_state_version"}
    assert schema["agent_events"] >= {"turn_id", "sequence", "run_state_version"}


async def test_memory_event_decision_id_backfill_is_idempotent_and_effective(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy-decision-events.db'}"
    settings = Settings(storage_backend="database", database_url=database_url)
    job_id = "legacy_resolution_job"
    decision_id = "legacy_pending_decision"
    operation = MemoryLifecycleOperation(
        operation_id="legacy_pending_operation",
        operation=MemoryOperation.PENDING,
        decision_status=MemoryDecisionStatus.PENDING,
        reason_code=MemoryFormationReasonCode.CONFIDENCE_PENDING,
        tenant_id="t1",
        user_id="u1",
        subject_type="user",
        subject_id="u1",
        memory_key="tenant:t1:user:u1:fact:legacy",
        candidate_hash="sha256:legacy-pending",
        formation_job_id=job_id,
    )
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE memory_events (
                    event_id VARCHAR(128) PRIMARY KEY,
                    event_type VARCHAR(64) NOT NULL,
                    memory_id VARCHAR(128),
                    user_id VARCHAR(128),
                    tenant_id VARCHAR(128),
                    agent_id VARCHAR(128),
                    request_id VARCHAR(128),
                    session_id VARCHAR(128),
                    turn_id VARCHAR(128),
                    run_id VARCHAR(128),
                    formation_job_id VARCHAR(128),
                    memory_key VARCHAR(512),
                    decision_status VARCHAR(32),
                    scope VARCHAR(64),
                    payload_text TEXT NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        common = {
            "user_id": "u1",
            "tenant_id": "t1",
            "job_id": job_id,
            "scope": "stable_fact",
        }
        await conn.execute(
            text(
                """
                INSERT INTO memory_events (
                    event_id, event_type, user_id, tenant_id, formation_job_id,
                    decision_status, scope, payload_text, created_at
                ) VALUES (
                    :event_id, 'memory_decision_pending', :user_id, :tenant_id, :job_id,
                    'pending', :scope, :payload, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                **common,
                "event_id": decision_id,
                "payload": dumps({"operation": operation.model_dump(mode="json")}),
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO memory_events (
                    event_id, event_type, user_id, tenant_id, formation_job_id,
                    decision_status, scope, payload_text, created_at
                ) VALUES (
                    'legacy_completion', 'memory_pending_confirm', :user_id, :tenant_id,
                    :job_id, 'resolved', :scope, :payload, CURRENT_TIMESTAMP
                )
                """
            ),
            {**common, "payload": dumps({"decision_id": decision_id, "action": "confirm"})},
        )
    await engine.dispose()

    await create_all_tables(settings)
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    await DatabaseMemoryFormationTurnJobRepository(session_factory).add_job(
        MemoryFormationJob(
            job_id=job_id,
            trigger="structured_event",
            mode="observe",
            tenant_id="t1",
            user_id="u1",
            idempotency_key="legacy-resolution-job",
            model_version="model-v1",
            prompt_version="prompt-v1",
            policy_version="policy-v1",
        )
    )
    async with session_factory() as session:
        backfilled = await session.scalar(
            text("SELECT decision_id FROM memory_events WHERE event_id='legacy_completion'")
        )
    traces = DatabaseMemoryFormationTraceRepository(session_factory)
    assert backfilled == decision_id
    assert await traces.list_traces(tenant_id="t1", user_id="u1", decision_status="pending") == []
    resolved = await traces.list_traces(tenant_id="t1", user_id="u1", decision_status="resolved")
    assert len(resolved) == 1


async def test_database_context_repositories_round_trip(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'context.db'}"
    settings = Settings(storage_backend="database", database_url=database_url)
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    memory_repository = DatabaseMemoryItemRepository(session_factory)
    knowledge_repository = DatabaseKnowledgeRepository(session_factory)

    memory = await memory_repository.add(
        MemoryItem(
            scope="user_preference",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            agent_id="agent_a",
            content="prefers concise answers",
            metadata={"source_trace": "test"},
        )
    )
    await memory_repository.add_event(
        MemoryEvent(
            event_type="memory_written",
            memory_id=memory.memory_id,
            user_id="u1",
            tenant_id="t1",
            agent_id="agent_a",
            payload={"scope": "user_preference"},
        )
    )
    active = await memory_repository.list_active(
        user_id="u1",
        tenant_id="t1",
        scopes=["user_preference"],
        subject_id="u1",
        agent_id="agent_a",
    )
    events = await memory_repository.list_events(user_id="u1", tenant_id="t1")

    assert active[0].content == "prefers concise answers"
    assert active[0].metadata["source_trace"] == "test"
    assert events[0].event_type == "memory_written"

    source = await knowledge_repository.upsert_source(
        KnowledgeSource(
            source_id="docs",
            name="Docs",
            allow_tenants=["t1"],
            tags=["product"],
            metadata={"owner": "qa"},
        )
    )
    chunk = await knowledge_repository.add_chunk(
        KnowledgeChunk(
            source_id=source.source_id,
            content="risk rating guide",
            title="Risk Guide",
            uri="https://example.test/risk",
            tags=["product"],
            metadata={"version": "1"},
        )
    )
    await knowledge_repository.add_log(
        KnowledgeRetrievalLog(
            query="risk",
            caller_type="agent",
            caller_id="agent_a",
            purpose="agent_execution",
            user_id="u1",
            tenant_id="t1",
            selected_source_ids=["docs"],
            denied_source_ids=["secret"],
            hit_count=1,
            status="ok",
            errors=[],
            metadata={"hit_count": 1},
        )
    )
    sources = await knowledge_repository.get_sources(["docs"])
    chunks = await knowledge_repository.list_chunks(source_ids=["docs"])
    hits = await knowledge_repository.search_chunks(
        query="risk guide", source_ids=["docs"], limit=5
    )
    logs = await knowledge_repository.list_logs(caller_id="agent_a", tenant_id="t1")

    assert sources[0].metadata["owner"] == "qa"
    assert chunks[0].chunk_id == chunk.chunk_id
    assert hits[0][0].content == "risk rating guide"
    assert logs[0].denied_source_ids == ["secret"]


async def test_memory_formation_schema_and_uniqueness(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation.db'}",
    )
    await create_all_tables(settings)
    engine = create_engine(settings)
    async with engine.begin() as conn:
        schema = await conn.run_sync(
            lambda sync_conn: {
                "tables": set(inspect(sync_conn).get_table_names()),
                "memory_columns": {
                    column["name"] for column in inspect(sync_conn).get_columns("memory_items")
                },
                "job_indexes": {
                    index["name"]
                    for index in inspect(sync_conn).get_indexes("memory_formation_jobs")
                },
            }
        )
    await engine.dispose()

    assert {
        "memory_revisions",
        "memory_formation_turns",
        "memory_formation_jobs",
        "memory_index_operations",
    } <= schema["tables"]
    assert {
        "memory_key",
        "current_revision_id",
        "formation_job_id",
        "lifecycle_status",
        "index_status",
    } <= schema["memory_columns"]
    assert "idx_memory_formation_jobs_claim" in schema["job_indexes"]

    session_factory = create_session_factory(settings)
    values = {
        "trigger": "idle",
        "status": "pending",
        "mode": "observe",
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "idempotency_key": "same-range",
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
        "policy_version": "policy-v1",
    }
    async with session_factory() as session:
        session.add(MemoryFormationJobModel(job_id="job_1", **values))
        await session.commit()
    async with session_factory() as session:
        session.add(MemoryFormationJobModel(job_id="job_2", **values))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_legacy_global_formation_request_constraint_becomes_owner_scoped(
    tmp_path,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy-formation-turns.db'}",
    )
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE memory_formation_turns (
                    turn_id VARCHAR(128) NOT NULL PRIMARY KEY,
                    request_id VARCHAR(128) NOT NULL UNIQUE,
                    session_id VARCHAR(128) NOT NULL,
                    run_id VARCHAR(128),
                    user_id VARCHAR(128) NOT NULL,
                    tenant_id VARCHAR(128) NOT NULL,
                    agent_id VARCHAR(128),
                    user_text TEXT NOT NULL DEFAULT '',
                    assistant_text TEXT NOT NULL DEFAULT '',
                    result_status VARCHAR(32) NOT NULL,
                    source_refs_text TEXT NOT NULL DEFAULT '[]',
                    used_memory_ids_text TEXT NOT NULL DEFAULT '[]',
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    claimed_job_id VARCHAR(128),
                    idle_deadline_at DATETIME,
                    completed_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO memory_formation_turns (
                    turn_id, request_id, session_id, user_id, tenant_id,
                    result_status, completed_at, created_at, updated_at
                ) VALUES (
                    'turn_1', 'shared-request', 'session_1', 'u1', 't1',
                    'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
    await engine.dispose()

    await create_all_tables(settings)
    await create_all_tables(settings)

    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO memory_formation_turns (
                    turn_id, request_id, session_id, user_id, tenant_id,
                    user_text, assistant_text, result_status, source_refs_text,
                    used_memory_ids_text, status, completed_at, created_at, updated_at
                ) VALUES (
                    'turn_2', 'shared-request', 'session_2', 'u2', 't2',
                    '', '', 'completed', '[]', '[]', 'pending',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        count = await conn.scalar(text("SELECT count(*) FROM memory_formation_turns"))
        constraints = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_unique_constraints("memory_formation_turns")
        )
    await engine.dispose()

    assert count == 2
    assert any(
        constraint["column_names"] == ["tenant_id", "user_id", "session_id", "request_id"]
        for constraint in constraints
    )


async def test_legacy_memory_backfill_and_plan_cleanup_are_idempotent(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy-memory.db'}",
    )
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE memory_items (
                    memory_id VARCHAR(128) PRIMARY KEY,
                    scope VARCHAR(64) NOT NULL,
                    subject_type VARCHAR(64) NOT NULL,
                    subject_id VARCHAR(128) NOT NULL,
                    user_id VARCHAR(128), tenant_id VARCHAR(128), agent_id VARCHAR(128),
                    content TEXT NOT NULL, structured_value_text TEXT NOT NULL,
                    source VARCHAR(64) NOT NULL, confidence INTEGER NOT NULL,
                    importance INTEGER NOT NULL, visibility VARCHAR(32) NOT NULL,
                    ttl_expires_at DATETIME, metadata_text TEXT NOT NULL,
                    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO memory_items VALUES (
                    'mem_legacy', 'user_preference', 'user', 'u1', 'u1', 't1', NULL,
                    'prefers concise answers', '{}', 'manual', 95, 50, 'user', NULL,
                    '{"mem0_memory_id":"mem0_legacy"}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE plans (
                    plan_id VARCHAR(128) PRIMARY KEY, session_id VARCHAR(128) NOT NULL,
                    user_id VARCHAR(128), status VARCHAR(32) NOT NULL,
                    current_step_id VARCHAR(128), original_query TEXT NOT NULL,
                    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO plans VALUES (
                    'unowned', 's1', NULL, 'pending', NULL, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
    await engine.dispose()

    await create_all_tables(settings)
    await create_all_tables(settings)

    engine = create_engine(settings)
    async with engine.begin() as conn:
        memory = (
            (
                await conn.execute(
                    text(
                        "SELECT memory_id, memory_key, current_revision_id, lifecycle_status, "
                        "index_status, metadata_text FROM memory_items"
                    )
                )
            )
            .mappings()
            .one()
        )
        revisions = (
            (
                await conn.execute(
                    text(
                        "SELECT revision_id, memory_id, revision_no, content "
                        "FROM memory_revisions WHERE memory_id = 'mem_legacy'"
                    )
                )
            )
            .mappings()
            .all()
        )
        plan_count = await conn.scalar(text("SELECT count(*) FROM plans"))
    await engine.dispose()

    assert memory["memory_id"] == "mem_legacy"
    assert memory["memory_key"] == "legacy:t1:mem_legacy"
    assert memory["current_revision_id"] == "mrev_legacy_mem_legacy"
    assert memory["lifecycle_status"] == "active"
    assert memory["index_status"] == "ready"
    assert "mem0_legacy" in memory["metadata_text"]
    assert len(revisions) == 1
    assert revisions[0]["revision_no"] == 1
    assert revisions[0]["content"] == "prefers concise answers"
    assert plan_count == 0

    engine = create_engine(settings)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO plans (
                        plan_id, session_id, user_id, tenant_id, status, current_step_id,
                        original_query, created_at, updated_at
                    ) VALUES (
                        'still-unowned', 's1', NULL, NULL, 'pending', NULL, '',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
    await engine.dispose()
