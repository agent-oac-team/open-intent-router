from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.db.models import (
    NativeDefinitionMigrationPreparationModel,
    NativeDefinitionMigrationSnapshotModel,
)


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    path_text = database_url.removeprefix(prefix)
    if path_text in {":memory:", ""}:
        return
    Path(path_text).parent.mkdir(parents=True, exist_ok=True)


async def ensure_native_definition_migration_schema(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Install only the expand-phase support required by the offline v2 gate.

    The migration command runs before the Runtime contract cutover, including
    against an existing database that may not have been started by this binary.
    It needs its own narrowly-scoped schema preparation rather than relying on
    a broad application startup or silently changing Registry source data.
    """

    async with session_factory() as session:
        connection = await session.connection()
        await connection.run_sync(_ensure_native_definition_migration_schema)
        await session.commit()


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
        _ensure_native_definition_migration_columns(sync_conn, inspector)
    if "native_definition_migration_snapshots" in tables:
        _ensure_native_definition_migration_snapshot_columns(sync_conn, inspector)
    if "native_definition_migration_preparations" in tables:
        _ensure_native_definition_migration_preparation_columns(sync_conn, inspector)
    if "memory_items" in tables:
        _ensure_memory_item_columns(sync_conn, inspector)
        _backfill_legacy_memories(sync_conn, dialect=dialect)
    if "memory_events" in tables:
        _ensure_memory_event_columns(sync_conn, inspector, dialect=dialect)
    if "memory_formation_turns" in tables:
        _ensure_formation_turn_request_scope(sync_conn, inspector, dialect=dialect)
    if "plans" in tables:
        _ensure_plan_ownership(sync_conn, inspector, tables, dialect=dialect)
    if "plan_steps" in tables:
        _ensure_plan_step_binding_columns(sync_conn, inspector, dialect=dialect)
    _ensure_context_owner_columns(sync_conn, inspector, tables, dialect=dialect)
    _ensure_execution_ticket_columns(sync_conn, inspector, tables, dialect=dialect)
    _ensure_canonical_pipeline_indexes(sync_conn, tables)


def _ensure_native_definition_migration_schema(sync_conn) -> None:
    """Add the migration-only schema without mutating Registry source rows."""

    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    if "agent_definitions" in tables:
        _ensure_native_definition_migration_columns(sync_conn, inspector)
    NativeDefinitionMigrationPreparationModel.__table__.create(sync_conn, checkfirst=True)
    NativeDefinitionMigrationSnapshotModel.__table__.create(sync_conn, checkfirst=True)
    refreshed_inspector = inspect(sync_conn)
    _ensure_native_definition_migration_preparation_columns(sync_conn, refreshed_inspector)
    _ensure_native_definition_migration_global_fence(sync_conn)
    _ensure_native_definition_migration_snapshot_columns(sync_conn, refreshed_inspector)


def _ensure_native_definition_migration_columns(sync_conn, inspector) -> None:
    columns = _column_names(inspector, "agent_definitions")
    definitions = {
        "schema_version": "VARCHAR(32)",
        "handling_text": "TEXT",
    }
    for name, definition in definitions.items():
        if name not in columns:
            sync_conn.execute(text(f"ALTER TABLE agent_definitions ADD COLUMN {name} {definition}"))
    sync_conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_definitions_schema_version "
            "ON agent_definitions (schema_version)"
        )
    )


def _ensure_native_definition_migration_snapshot_columns(sync_conn, inspector) -> None:
    snapshot_columns = _column_names(inspector, "native_definition_migration_snapshots")
    if "input_fingerprint" not in snapshot_columns:
        sync_conn.execute(
            text(
                "ALTER TABLE native_definition_migration_snapshots "
                "ADD COLUMN input_fingerprint VARCHAR(64)"
            )
        )


def _ensure_native_definition_migration_preparation_columns(sync_conn, inspector) -> None:
    preparation_columns = _column_names(inspector, "native_definition_migration_preparations")
    if "active" not in preparation_columns:
        sync_conn.execute(
            text(
                "ALTER TABLE native_definition_migration_preparations "
                "ADD COLUMN active BOOLEAN DEFAULT false NOT NULL"
            )
        )
    if "target_fingerprint" not in preparation_columns:
        sync_conn.execute(
            text(
                "ALTER TABLE native_definition_migration_preparations "
                "ADD COLUMN target_fingerprint VARCHAR(64)"
            )
        )


def _ensure_native_definition_migration_global_fence(sync_conn) -> None:
    """Seed the one row that serializes prepare/rollback activation."""

    # This path runs during an offline command's schema preflight, so two
    # operators may arrive before either has observed the row.  Use the
    # portable PostgreSQL/SQLite upsert spelling instead of a select-then-
    # insert race; the actual migration lock is taken later by ``prepare``.
    sync_conn.execute(
        text(
            "INSERT INTO native_definition_migration_preparations "
            "(source, native_writes_frozen, new_execution_frozen, active) "
            "VALUES (:source, false, false, false) "
            "ON CONFLICT (source) DO NOTHING"
        ),
        {"source": "__native_definition_global__"},
    )


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
            "agent_revision": "INTEGER",
            "handling_kind": "VARCHAR(32)",
            "external_ticket_issuance_state": "VARCHAR(16)",
            "binding_snapshot_text": "TEXT",
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


def _ensure_execution_ticket_columns(
    sync_conn, inspector, tables: set[str], *, dialect: str
) -> None:
    if "execution_tickets" not in tables:
        return
    columns = _column_names(inspector, "execution_tickets")
    if "canonical_reuse" not in columns:
        sync_conn.execute(
            text(
                "ALTER TABLE execution_tickets "
                "ADD COLUMN canonical_reuse BOOLEAN DEFAULT FALSE NOT NULL"
            )
        )
    predicate = (
        "canonical_reuse AND status IN ('issued', 'claimed')"
        if dialect == "postgresql"
        else "canonical_reuse = 1 AND status IN ('issued', 'claimed')"
    )
    sync_conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_execution_tickets_reusable_active_run_purpose "
            "ON execution_tickets (run_id, purpose) "
            f"WHERE {predicate}"
        )
    )


def _ensure_canonical_pipeline_indexes(sync_conn, tables: set[str]) -> None:
    statements = {
        "canonical_turns": (
            {"tenant_id", "user_id", "status", "updated_at"},
            "CREATE INDEX IF NOT EXISTS idx_canonical_turns_owner_status_updated "
            "ON canonical_turns (tenant_id, user_id, status, updated_at)",
        ),
        "agent_runs": (
            {"tenant_id", "user_id", "request_id", "status"},
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_owner_request_status "
            "ON agent_runs (tenant_id, user_id, request_id, status)",
        ),
        "agent_results": (
            {"tenant_id", "user_id", "run_id", "status"},
            "CREATE INDEX IF NOT EXISTS idx_agent_results_owner_run_status "
            "ON agent_results (tenant_id, user_id, run_id, status)",
        ),
    }
    for table, (required_columns, statement) in statements.items():
        if table in tables and required_columns <= _column_names(inspect(sync_conn), table):
            sync_conn.execute(text(statement))


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


def _ensure_plan_step_binding_columns(sync_conn, inspector, *, dialect: str) -> None:
    columns = _column_names(inspector, "plan_steps")
    definitions = {
        "agent_revision": "INTEGER",
        "binding_requirement_text": "TEXT",
    }
    for name, column_type in definitions.items():
        if name not in columns:
            sync_conn.execute(text(f"ALTER TABLE plan_steps ADD COLUMN {name} {column_type}"))
    # A half-written frozen Binding cannot be interpreted safely.  Old rows
    # predate the fence, so repair a malformed historical pair into the legacy
    # (both-null) form before enforcing the canonical invariant.
    sync_conn.execute(
        text(
            """
            UPDATE plan_steps
            SET agent_revision = NULL, binding_requirement_text = NULL
            WHERE (agent_revision IS NULL AND binding_requirement_text IS NOT NULL)
               OR (agent_revision IS NOT NULL AND binding_requirement_text IS NULL)
            """
        )
    )
    pair_condition = (
        "(agent_revision IS NULL AND binding_requirement_text IS NULL) "
        "OR (agent_revision IS NOT NULL AND binding_requirement_text IS NOT NULL)"
    )
    if dialect == "postgresql":
        constraints = inspector.get_check_constraints("plan_steps")
        if "ck_plan_steps_binding_pair" not in {
            constraint.get("name") for constraint in constraints
        }:
            sync_conn.execute(
                text(
                    "ALTER TABLE plan_steps ADD CONSTRAINT ck_plan_steps_binding_pair "
                    f"CHECK ({pair_condition})"
                )
            )
        return
    if dialect == "sqlite":
        for name, event in (
            ("trg_plan_steps_binding_pair_insert", "INSERT"),
            (
                "trg_plan_steps_binding_pair_update",
                "UPDATE OF agent_revision, binding_requirement_text",
            ),
        ):
            sync_conn.execute(
                text(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {name}
                    BEFORE {event} ON plan_steps
                    FOR EACH ROW
                    WHEN (NEW.agent_revision IS NULL AND NEW.binding_requirement_text IS NOT NULL)
                      OR (NEW.agent_revision IS NOT NULL AND NEW.binding_requirement_text IS NULL)
                    BEGIN
                        SELECT RAISE(ABORT, 'plan step binding fields must be paired');
                    END
                    """
                )
            )
        return
    raise RuntimeError(f"Unsupported database dialect for Plan Step migration: {dialect}")


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
