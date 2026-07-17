from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.db.models import Base


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    path_text = database_url.removeprefix(prefix)
    if path_text in {":memory:", ""}:
        return
    Path(path_text).parent.mkdir(parents=True, exist_ok=True)


def create_engine(settings: Settings) -> AsyncEngine:
    _ensure_sqlite_parent(settings.database_url)
    return create_async_engine(settings.database_url, future=True)


def create_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(create_engine(settings), expire_on_commit=False)


async def create_all_tables(settings: Settings) -> None:
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_compatible_columns)
    await engine.dispose()


def _ensure_compatible_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    dialect = sync_conn.dialect.name
    tables = set(inspector.get_table_names())
    if "agent_definitions" in tables:
        agent_columns = _column_names(inspector, "agent_definitions")
        if "context_text" not in agent_columns:
            sync_conn.execute(
                text("ALTER TABLE agent_definitions ADD COLUMN context_text TEXT DEFAULT '{}'")
            )
        if "revision" not in agent_columns:
            sync_conn.execute(
                text("ALTER TABLE agent_definitions ADD COLUMN revision INTEGER DEFAULT 0 NOT NULL")
            )
    if "memory_items" in tables:
        _ensure_memory_item_columns(sync_conn, inspector)
        _backfill_legacy_memories(sync_conn, dialect=dialect)
    if "memory_events" in tables:
        _ensure_memory_event_columns(sync_conn, inspector, dialect=dialect)
    if "memory_formation_turns" in tables:
        _ensure_formation_turn_request_scope(sync_conn, inspector, dialect=dialect)
    if "plans" in tables:
        _ensure_plan_ownership(sync_conn, inspector, tables, dialect=dialect)
    _ensure_context_owner_columns(sync_conn, inspector, tables, dialect=dialect)


def _column_names(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _ensure_context_owner_columns(sync_conn, inspector, tables: set[str], *, dialect: str) -> None:
    definitions = _context_owner_column_definitions(dialect)
    for table, columns_to_add in definitions.items():
        if table not in tables:
            continue
        existing = _column_names(inspector, table)
        for name, column_type in columns_to_add.items():
            if name not in existing:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"))
            sync_conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{name} ON {table} ({name})")
            )
        if table == "agent_runs":
            sync_conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_delegation_key "
                    "ON agent_runs (delegation_key) WHERE delegation_key IS NOT NULL"
                )
            )


def _context_owner_column_definitions(dialect: str) -> dict[str, dict[str, str]]:
    datetime_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    return {
        "chat_messages": {"tenant_id": "VARCHAR(128)"},
        "agent_runs": {
            "user_id": "VARCHAR(128)",
            "tenant_id": "VARCHAR(128)",
            "plan_id": "VARCHAR(128)",
            "step_id": "VARCHAR(128)",
            "formation_suppressed": "BOOLEAN DEFAULT FALSE NOT NULL",
            "formation_published_order": "INTEGER DEFAULT 0 NOT NULL",
            "used_memory_ids_text": "TEXT DEFAULT '[]' NOT NULL",
            "turn_id": "VARCHAR(128)",
            "delegated": "BOOLEAN DEFAULT FALSE NOT NULL",
            "delegation_key": "VARCHAR(128)",
            "state_version": "INTEGER DEFAULT 1 NOT NULL",
            "event_sequence": "INTEGER DEFAULT 0 NOT NULL",
            "deadline_at": datetime_type,
            "heartbeat_at": datetime_type,
            "claim_owner": "VARCHAR(128)",
            "claim_token": "VARCHAR(128)",
            "claim_expires_at": datetime_type,
            "terminal_event_id": "VARCHAR(128)",
        },
        "agent_results": {
            "user_id": "VARCHAR(128)",
            "tenant_id": "VARCHAR(128)",
            "plan_id": "VARCHAR(128)",
            "step_id": "VARCHAR(128)",
            "message": "TEXT DEFAULT '' NOT NULL",
            "formation_suppressed": "BOOLEAN DEFAULT FALSE NOT NULL",
            "formation_published": "BOOLEAN DEFAULT FALSE NOT NULL",
            "turn_captured": "BOOLEAN DEFAULT FALSE NOT NULL",
            "turn_id": "VARCHAR(128)",
            "run_state_version": "INTEGER",
        },
        "agent_events": {
            "user_id": "VARCHAR(128)",
            "tenant_id": "VARCHAR(128)",
            "turn_id": "VARCHAR(128)",
            "sequence": "INTEGER",
            "run_state_version": "INTEGER",
        },
    }


def _ensure_memory_item_columns(sync_conn, inspector) -> None:
    columns = _column_names(inspector, "memory_items")
    definitions = {
        "memory_key": "VARCHAR(512)",
        "candidate_hash": "VARCHAR(128)",
        "current_revision_id": "VARCHAR(128)",
        "formation_job_id": "VARCHAR(128)",
        "lifecycle_status": "VARCHAR(32)",
        "index_status": "VARCHAR(32)",
        "canonical_refs_text": "TEXT DEFAULT '[]'",
    }
    for name, definition in definitions.items():
        if name not in columns:
            sync_conn.execute(text(f"ALTER TABLE memory_items ADD COLUMN {name} {definition}"))


def _ensure_memory_event_columns(sync_conn, inspector, *, dialect: str) -> None:
    columns = _column_names(inspector, "memory_events")
    definitions = {
        "request_id": "VARCHAR(128)",
        "session_id": "VARCHAR(128)",
        "turn_id": "VARCHAR(128)",
        "run_id": "VARCHAR(128)",
        "formation_job_id": "VARCHAR(128)",
        "memory_key": "VARCHAR(512)",
        "decision_status": "VARCHAR(32)",
        "decision_id": "VARCHAR(128)",
        "scope": "VARCHAR(64)",
    }
    for name, definition in definitions.items():
        if name not in columns:
            sync_conn.execute(text(f"ALTER TABLE memory_events ADD COLUMN {name} {definition}"))
        sync_conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS ix_memory_events_{name} ON memory_events ({name})")
        )
    _backfill_memory_event_decision_ids(sync_conn, dialect=dialect)


def _backfill_memory_event_decision_ids(sync_conn, *, dialect: str) -> None:
    terminal_types = "'memory_pending_confirm','memory_pending_reject','memory_pending_conflict'"
    if dialect == "sqlite":
        sync_conn.execute(
            text(
                f"""
                UPDATE memory_events
                SET decision_id = json_extract(payload_text, '$.decision_id')
                WHERE decision_id IS NULL
                  AND event_type IN ({terminal_types})
                  AND json_valid(payload_text)
                  AND json_type(payload_text, '$.decision_id') = 'text'
                  AND length(json_extract(payload_text, '$.decision_id')) BETWEEN 1 AND 128
                """
            )
        )
        return
    if dialect == "postgresql":
        sync_conn.execute(
            text(
                f"""
                UPDATE memory_events
                SET decision_id = payload_text::jsonb ->> 'decision_id'
                WHERE decision_id IS NULL
                  AND event_type IN ({terminal_types})
                  AND jsonb_typeof(payload_text::jsonb -> 'decision_id') = 'string'
                  AND length(payload_text::jsonb ->> 'decision_id') BETWEEN 1 AND 128
                """
            )
        )
        return
    raise RuntimeError(f"Unsupported database dialect for memory event migration: {dialect}")


def _ensure_formation_turn_request_scope(sync_conn, inspector, *, dialect: str) -> None:
    expected = ["tenant_id", "user_id", "session_id", "request_id"]
    constraints = inspector.get_unique_constraints("memory_formation_turns")
    has_expected = any(constraint.get("column_names") == expected for constraint in constraints)
    has_global_request = any(
        constraint.get("column_names") == ["request_id"] for constraint in constraints
    )
    if has_expected and not has_global_request:
        return
    if dialect == "sqlite":
        _rebuild_scoped_formation_turns_table(sync_conn)
        return
    if dialect != "postgresql":
        raise RuntimeError(f"Unsupported database dialect for formation turn migration: {dialect}")
    preparer = sync_conn.dialect.identifier_preparer
    for constraint in constraints:
        if constraint.get("column_names") != ["request_id"] or not constraint.get("name"):
            continue
        name = preparer.quote(constraint["name"])
        sync_conn.execute(text(f"ALTER TABLE memory_formation_turns DROP CONSTRAINT {name}"))
    if not has_expected:
        sync_conn.execute(
            text(
                """
                ALTER TABLE memory_formation_turns
                ADD CONSTRAINT uq_memory_formation_turns_owner_request
                UNIQUE (tenant_id, user_id, session_id, request_id)
                """
            )
        )


def _rebuild_scoped_formation_turns_table(sync_conn) -> None:
    sync_conn.execute(
        text(
            """
            CREATE TABLE memory_formation_turns_scoped_migration (
                turn_id VARCHAR(128) NOT NULL PRIMARY KEY,
                request_id VARCHAR(128) NOT NULL,
                session_id VARCHAR(128) NOT NULL,
                run_id VARCHAR(128),
                user_id VARCHAR(128) NOT NULL,
                tenant_id VARCHAR(128) NOT NULL,
                agent_id VARCHAR(128),
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                result_status VARCHAR(32) NOT NULL,
                source_refs_text TEXT NOT NULL,
                used_memory_ids_text TEXT NOT NULL,
                status VARCHAR(32) NOT NULL,
                claimed_job_id VARCHAR(128),
                idle_deadline_at DATETIME,
                completed_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_memory_formation_turns_owner_request
                    UNIQUE (tenant_id, user_id, session_id, request_id)
            )
            """
        )
    )
    sync_conn.execute(
        text(
            """
            INSERT INTO memory_formation_turns_scoped_migration (
                turn_id, request_id, session_id, run_id, user_id, tenant_id, agent_id,
                user_text, assistant_text, result_status, source_refs_text,
                used_memory_ids_text, status, claimed_job_id, idle_deadline_at,
                completed_at, created_at, updated_at
            )
            SELECT
                turn_id, request_id, session_id, run_id, user_id, tenant_id, agent_id,
                user_text, assistant_text, result_status, source_refs_text,
                used_memory_ids_text, status, claimed_job_id, idle_deadline_at,
                completed_at, created_at, updated_at
            FROM memory_formation_turns
            """
        )
    )
    sync_conn.execute(text("DROP TABLE memory_formation_turns"))
    sync_conn.execute(
        text("ALTER TABLE memory_formation_turns_scoped_migration RENAME TO memory_formation_turns")
    )
    indexes = (
        "CREATE INDEX ix_memory_formation_turns_request_id ON memory_formation_turns (request_id)",
        "CREATE INDEX ix_memory_formation_turns_session_id ON memory_formation_turns (session_id)",
        "CREATE INDEX ix_memory_formation_turns_run_id ON memory_formation_turns (run_id)",
        "CREATE INDEX ix_memory_formation_turns_user_id ON memory_formation_turns (user_id)",
        "CREATE INDEX ix_memory_formation_turns_tenant_id ON memory_formation_turns (tenant_id)",
        "CREATE INDEX ix_memory_formation_turns_agent_id ON memory_formation_turns (agent_id)",
        "CREATE INDEX ix_memory_formation_turns_status ON memory_formation_turns (status)",
        "CREATE INDEX ix_memory_formation_turns_claimed_job_id ON memory_formation_turns (claimed_job_id)",
        "CREATE INDEX ix_memory_formation_turns_idle_deadline_at ON memory_formation_turns (idle_deadline_at)",
        "CREATE INDEX idx_memory_formation_turns_pending_idle ON memory_formation_turns (tenant_id, user_id, session_id, status, idle_deadline_at)",
    )
    for statement in indexes:
        sync_conn.execute(text(statement))


def _backfill_legacy_memories(sync_conn, *, dialect: str) -> None:
    sync_conn.execute(
        text(
            """
            UPDATE memory_items
            SET memory_key = 'legacy:' || COALESCE(tenant_id, 'none') || ':' || memory_id,
                lifecycle_status = COALESCE(lifecycle_status, 'active'),
                index_status = COALESCE(
                    index_status,
                    CASE
                        WHEN metadata_text LIKE '%mem0_memory_id%' THEN 'ready'
                        ELSE 'pending'
                    END
                ),
                canonical_refs_text = COALESCE(canonical_refs_text, '[]')
            WHERE memory_key IS NULL OR lifecycle_status IS NULL OR index_status IS NULL
            """
        )
    )
    insert_prefix = "INSERT OR IGNORE INTO" if dialect == "sqlite" else "INSERT INTO"
    conflict_suffix = "" if dialect == "sqlite" else "ON CONFLICT DO NOTHING"
    sync_conn.execute(
        text(
            f"""
            {insert_prefix} memory_revisions (
                revision_id, memory_id, revision_no, memory_key, operation, content,
                structured_value_text, evidence_refs_text, confidence, policy_version,
                supersedes_revision_id, formation_job_id, created_at
            )
            SELECT
                'mrev_legacy_' || memory_id, memory_id, 1, memory_key, 'add', content,
                structured_value_text, '[]', confidence, 'legacy-backfill-v1',
                NULL, formation_job_id, created_at
            FROM memory_items
            WHERE lifecycle_status = 'active'
            {conflict_suffix}
            """
        )
    )
    sync_conn.execute(
        text(
            """
            UPDATE memory_items
            SET current_revision_id = 'mrev_legacy_' || memory_id
            WHERE current_revision_id IS NULL AND lifecycle_status = 'active'
            """
        )
    )
    index_statements = (
        "CREATE INDEX IF NOT EXISTS ix_memory_items_memory_key ON memory_items (memory_key)",
        "CREATE INDEX IF NOT EXISTS ix_memory_items_current_revision_id ON memory_items (current_revision_id)",
        "CREATE INDEX IF NOT EXISTS ix_memory_items_formation_job_id ON memory_items (formation_job_id)",
        "CREATE INDEX IF NOT EXISTS ix_memory_items_lifecycle_status ON memory_items (lifecycle_status)",
        "CREATE INDEX IF NOT EXISTS ix_memory_items_index_status ON memory_items (index_status)",
        "CREATE INDEX IF NOT EXISTS idx_memory_items_lifecycle_index ON memory_items (lifecycle_status, index_status)",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_items_active_key
        ON memory_items (tenant_id, subject_type, subject_id, scope, memory_key)
        WHERE lifecycle_status = 'active'""",
    )
    for statement in index_statements:
        sync_conn.execute(text(statement))


def _ensure_plan_ownership(sync_conn, inspector, tables: set[str], *, dialect: str) -> None:
    columns = _column_names(inspector, "plans")
    if "tenant_id" not in columns:
        sync_conn.execute(text("ALTER TABLE plans ADD COLUMN tenant_id VARCHAR(128)"))
    claim_columns = {
        "state_version": "INTEGER DEFAULT 0 NOT NULL",
        "formation_published_version": "INTEGER DEFAULT 0 NOT NULL",
        "execution_claim_id": "VARCHAR(128)",
        "execution_claim_step_id": "VARCHAR(128)",
        "execution_claim_state_version": "INTEGER",
        "execution_claim_key": "VARCHAR(128)",
        "execution_attempt": "INTEGER DEFAULT 0 NOT NULL",
        "execution_claim_expires_at": "TIMESTAMP",
    }
    for name, column_type in claim_columns.items():
        if name not in columns:
            sync_conn.execute(text(f"ALTER TABLE plans ADD COLUMN {name} {column_type}"))
    invalid_filter = (
        "user_id IS NULL OR trim(user_id) = '' OR tenant_id IS NULL OR trim(tenant_id) = ''"
    )
    if "plan_steps" in tables:
        sync_conn.execute(
            text(
                f"DELETE FROM plan_steps WHERE plan_id IN "
                f"(SELECT plan_id FROM plans WHERE {invalid_filter})"
            )
        )
    sync_conn.execute(text(f"DELETE FROM plans WHERE {invalid_filter}"))
    ownership_columns = {
        column["name"]: column for column in inspect(sync_conn).get_columns("plans")
    }
    if ownership_columns["user_id"]["nullable"] or ownership_columns["tenant_id"]["nullable"]:
        if dialect == "sqlite":
            _rebuild_owned_plans_table(sync_conn)
        elif dialect == "postgresql":
            sync_conn.execute(text("ALTER TABLE plans ALTER COLUMN user_id SET NOT NULL"))
            sync_conn.execute(text("ALTER TABLE plans ALTER COLUMN tenant_id SET NOT NULL"))
        else:
            raise RuntimeError(f"Unsupported database dialect for Plan migration: {dialect}")
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_plans_user_id ON plans (user_id)"))
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_plans_tenant_id ON plans (tenant_id)"))
    sync_conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_plans_execution_claim_id ON plans (execution_claim_id)")
    )
    sync_conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_plans_execution_claim_expires_at "
            "ON plans (execution_claim_expires_at)"
        )
    )


def _rebuild_owned_plans_table(sync_conn) -> None:
    sync_conn.execute(
        text(
            """
            CREATE TABLE plans_owned_migration (
                plan_id VARCHAR(128) NOT NULL PRIMARY KEY,
                session_id VARCHAR(128) NOT NULL,
                user_id VARCHAR(128) NOT NULL,
                tenant_id VARCHAR(128) NOT NULL,
                status VARCHAR(32) NOT NULL,
                current_step_id VARCHAR(128),
                state_version INTEGER DEFAULT 0 NOT NULL,
                formation_published_version INTEGER DEFAULT 0 NOT NULL,
                execution_claim_id VARCHAR(128),
                execution_claim_step_id VARCHAR(128),
                execution_claim_state_version INTEGER,
                execution_claim_key VARCHAR(128),
                execution_attempt INTEGER DEFAULT 0 NOT NULL,
                execution_claim_expires_at TIMESTAMP,
                original_query TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    sync_conn.execute(
        text(
            """
            INSERT INTO plans_owned_migration (
                plan_id, session_id, user_id, tenant_id, status, current_step_id,
                state_version, formation_published_version,
                execution_claim_id, execution_claim_step_id, execution_claim_state_version,
                execution_claim_key, execution_attempt,
                execution_claim_expires_at,
                original_query, created_at, updated_at
            )
            SELECT
                plan_id, session_id, user_id, tenant_id, status, current_step_id,
                state_version, formation_published_version,
                execution_claim_id, execution_claim_step_id, execution_claim_state_version,
                execution_claim_key, execution_attempt,
                execution_claim_expires_at,
                original_query, created_at, updated_at
            FROM plans
            """
        )
    )
    sync_conn.execute(text("DROP TABLE plans"))
    sync_conn.execute(text("ALTER TABLE plans_owned_migration RENAME TO plans"))
    sync_conn.execute(text("CREATE INDEX ix_plans_session_id ON plans (session_id)"))
    sync_conn.execute(text("CREATE INDEX ix_plans_status ON plans (status)"))


async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
