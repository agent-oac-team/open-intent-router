-- OIR PostgreSQL bootstrap and schema snapshot.
-- Source of truth: app/db/models.py
--
-- Usage:
--   psql postgresql://<admin-user>@127.0.0.1:5432/postgres -f sql/postgresql_schema.sql
--
-- Optional variables:
--   psql postgresql://<admin-user>@127.0.0.1:5432/postgres \
--     -v oir_db=oir \
--     -v oir_user=oir \
--     -v oir_password=oir \
--     -f sql/postgresql_schema.sql
--
-- This file creates an OIR-only database by default. Do not run OIR tables in the
-- legacy IRS/OAC `oac` database; keeping `oir` separate makes later IRS->OIR
-- replacement and data migration audits much clearer.

\set ON_ERROR_STOP on

\if :{?oir_db}
\else
\set oir_db oir
\endif

\if :{?oir_user}
\else
\set oir_user oir
\endif

\if :{?oir_password}
\else
\set oir_password oir
\endif

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'oir_user', :'oir_password')
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = :'oir_user'
)
\gexec

SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L', :'oir_db', :'oir_user', 'UTF8')
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_database
    WHERE datname = :'oir_db'
)
\gexec

GRANT CONNECT ON DATABASE :"oir_db" TO :"oir_user";

SELECT format('GRANT %I TO CURRENT_USER', :'oir_user')
WHERE current_user <> :'oir_user'
\gexec

SELECT format('ALTER DATABASE %I OWNER TO %I', :'oir_db', :'oir_user')
WHERE EXISTS (
    SELECT 1
    FROM pg_database
    WHERE datname = :'oir_db'
      AND pg_catalog.pg_get_userbyid(datdba) <> :'oir_user'
)
\gexec

\connect :oir_db

SELECT format('REASSIGN OWNED BY CURRENT_USER TO %I', :'oir_user')
WHERE current_user <> :'oir_user'
\gexec

ALTER SCHEMA public OWNER TO :"oir_user";
GRANT USAGE, CREATE ON SCHEMA public TO :"oir_user";

SET ROLE :"oir_user";

BEGIN;

CREATE TABLE IF NOT EXISTS agent_definitions (
    id SERIAL NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    version VARCHAR(64),
    revision INTEGER DEFAULT 0 NOT NULL,
    type VARCHAR(64) NOT NULL,
    schema_version VARCHAR(32),
    handling_text TEXT,
    enabled BOOLEAN NOT NULL,
    domain VARCHAR(200),
    capabilities_text TEXT NOT NULL,
    tags_text TEXT NOT NULL,
    trigger_text TEXT NOT NULL,
    access_policy_text TEXT NOT NULL,
    required_inputs_text TEXT NOT NULL,
    optional_inputs_text TEXT NOT NULL,
    input_schema_text TEXT NOT NULL,
    output_schema_text TEXT NOT NULL,
    invocation_text TEXT NOT NULL,
    ui_handoff_text TEXT NOT NULL,
    context_text TEXT NOT NULL,
    priority INTEGER NOT NULL,
    metadata_text TEXT NOT NULL,
    source VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_definitions_agent_id ON agent_definitions (agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_definitions_enabled ON agent_definitions (enabled);
CREATE INDEX IF NOT EXISTS ix_agent_definitions_type ON agent_definitions (type);
CREATE INDEX IF NOT EXISTS ix_agent_definitions_schema_version ON agent_definitions (schema_version);
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS revision INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS schema_version VARCHAR(32);
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS handling_text TEXT;

CREATE TABLE IF NOT EXISTS registry_revisions (
    revision_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    revision INTEGER NOT NULL,
    operation VARCHAR(32) NOT NULL,
    operator_id VARCHAR(128) NOT NULL,
    source VARCHAR(64) NOT NULL,
    before_text TEXT,
    after_text TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (revision_id)
);
CREATE INDEX IF NOT EXISTS ix_registry_revisions_agent_id ON registry_revisions (agent_id);
CREATE INDEX IF NOT EXISTS ix_registry_revisions_operation ON registry_revisions (operation);
CREATE INDEX IF NOT EXISTS ix_registry_revisions_operator_id ON registry_revisions (operator_id);
CREATE INDEX IF NOT EXISTS ix_registry_revisions_source ON registry_revisions (source);

-- Offline Native Definition v1 -> v2 migration support. These tables are not
-- served by an API and retain private rollback material only for the controlled
-- maintenance window. They are not a Runtime feature flag or a dual-read path.
CREATE TABLE IF NOT EXISTS native_definition_migration_preparations (
    source VARCHAR(32) NOT NULL,
    native_writes_frozen BOOLEAN NOT NULL,
    new_execution_frozen BOOLEAN NOT NULL,
    active BOOLEAN DEFAULT false NOT NULL,
    target_fingerprint VARCHAR(64),
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (source)
);
ALTER TABLE native_definition_migration_preparations
    ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT false NOT NULL;
ALTER TABLE native_definition_migration_preparations
    ADD COLUMN IF NOT EXISTS target_fingerprint VARCHAR(64);
INSERT INTO native_definition_migration_preparations (
    source,
    native_writes_frozen,
    new_execution_frozen,
    active
) VALUES ('__native_definition_global__', false, false, false)
ON CONFLICT (source) DO NOTHING;

CREATE TABLE IF NOT EXISTS native_definition_migration_snapshots (
    snapshot_id VARCHAR(128) NOT NULL,
    source VARCHAR(32) NOT NULL,
    migration_version VARCHAR(128) NOT NULL,
    legacy_runtime_version VARCHAR(128) NOT NULL,
    input_digest VARCHAR(64) NOT NULL,
    input_fingerprint VARCHAR(64) NOT NULL,
    payload_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (snapshot_id)
);
ALTER TABLE native_definition_migration_snapshots
    ADD COLUMN IF NOT EXISTS input_fingerprint VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_native_definition_migration_snapshots_source
    ON native_definition_migration_snapshots (source);

CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL NOT NULL,
    message_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128),
    tenant_id VARCHAR(128),
    plan_id VARCHAR(128),
    step_id VARCHAR(128),
    source VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    agent_id VARCHAR(128),
    agent_session_id VARCHAR(128),
    request_id VARCHAR(128),
    event_id VARCHAR(128),
    metadata_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(128);
CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_messages_message_id ON chat_messages (message_id);
CREATE INDEX IF NOT EXISTS ix_chat_messages_session_id ON chat_messages (session_id);
CREATE INDEX IF NOT EXISTS ix_chat_messages_tenant_id ON chat_messages (tenant_id);
CREATE INDEX IF NOT EXISTS ix_chat_messages_source ON chat_messages (source);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created ON chat_messages (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_source_created ON chat_messages (session_id, source, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_agent_context_created ON chat_messages (
    session_id,
    source,
    agent_id,
    agent_session_id,
    created_at
);

CREATE TABLE IF NOT EXISTS conversation_events (
    id SERIAL NOT NULL,
    event_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128),
    user_id VARCHAR(128),
    event_type VARCHAR(64) NOT NULL,
    source VARCHAR(64),
    agent_id VARCHAR(128),
    payload_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_conversation_events_event_id ON conversation_events (event_id);
CREATE INDEX IF NOT EXISTS ix_conversation_events_event_type ON conversation_events (event_type);
CREATE INDEX IF NOT EXISTS ix_conversation_events_session_id ON conversation_events (session_id);

CREATE TABLE IF NOT EXISTS canonical_turns (
    turn_id VARCHAR(128) NOT NULL,
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    source VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    state_version INTEGER DEFAULT 1 NOT NULL,
    user_input_text TEXT NOT NULL,
    references_text TEXT DEFAULT '{}' NOT NULL,
    final_response_text TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (turn_id),
    CONSTRAINT uq_canonical_turns_owner_request UNIQUE (tenant_id, user_id, request_id)
);
CREATE INDEX IF NOT EXISTS ix_canonical_turns_tenant_id ON canonical_turns (tenant_id);
CREATE INDEX IF NOT EXISTS ix_canonical_turns_user_id ON canonical_turns (user_id);
CREATE INDEX IF NOT EXISTS ix_canonical_turns_session_id ON canonical_turns (session_id);
CREATE INDEX IF NOT EXISTS ix_canonical_turns_request_id ON canonical_turns (request_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_canonical_turns_request_id ON canonical_turns (request_id);
CREATE INDEX IF NOT EXISTS ix_canonical_turns_status ON canonical_turns (status);
CREATE INDEX IF NOT EXISTS idx_canonical_turns_owner_session_status
    ON canonical_turns (tenant_id, user_id, session_id, status);
CREATE INDEX IF NOT EXISTS idx_canonical_turns_status_updated
    ON canonical_turns (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_canonical_turns_owner_status_updated
ON canonical_turns (tenant_id, user_id, status, updated_at);

CREATE TABLE IF NOT EXISTS execution_trace_events (
    event_offset BIGINT GENERATED BY DEFAULT AS IDENTITY,
    trace_id VARCHAR(134) NOT NULL,
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    turn_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128),
    event_type VARCHAR(64) NOT NULL,
    stage VARCHAR(128) NOT NULL,
    status VARCHAR(64) NOT NULL,
    source VARCHAR(128) NOT NULL,
    source_event_id VARCHAR(256) NOT NULL,
    source_version INTEGER NOT NULL,
    reason_code VARCHAR(128),
    facts_text TEXT NOT NULL DEFAULT '{}',
    evidence_refs_text TEXT NOT NULL DEFAULT '[]',
    visibility VARCHAR(32) NOT NULL DEFAULT 'business_runtime',
    schema_version INTEGER NOT NULL DEFAULT 1,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    recorded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (event_offset),
    CONSTRAINT uq_execution_trace_events_source
        UNIQUE (source, source_event_id, source_version)
);
CREATE INDEX IF NOT EXISTS idx_execution_trace_events_owner_turn_offset
ON execution_trace_events (tenant_id, user_id, session_id, turn_id, event_offset);
CREATE INDEX IF NOT EXISTS idx_execution_trace_events_trace_offset
ON execution_trace_events (trace_id, event_offset);

CREATE TABLE IF NOT EXISTS turn_outbox (
    outbox_id VARCHAR(128) NOT NULL,
    turn_id VARCHAR(128) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(512) NOT NULL,
    payload_text TEXT DEFAULT '{}' NOT NULL,
    status VARCHAR(32) DEFAULT 'pending' NOT NULL,
    attempt_count INTEGER DEFAULT 0 NOT NULL,
    max_attempts INTEGER DEFAULT 5 NOT NULL,
    lease_owner VARCHAR(128),
    lease_token VARCHAR(128),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(128),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (outbox_id),
    CONSTRAINT uq_turn_outbox_idempotency UNIQUE (idempotency_key),
    CONSTRAINT fk_turn_outbox_turn
        FOREIGN KEY (turn_id) REFERENCES canonical_turns (turn_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS ix_turn_outbox_turn_id ON turn_outbox (turn_id);
CREATE INDEX IF NOT EXISTS ix_turn_outbox_event_type ON turn_outbox (event_type);
CREATE INDEX IF NOT EXISTS ix_turn_outbox_status ON turn_outbox (status);
CREATE INDEX IF NOT EXISTS ix_turn_outbox_lease_expires_at ON turn_outbox (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_turn_outbox_available_at ON turn_outbox (available_at);
CREATE INDEX IF NOT EXISTS idx_turn_outbox_claim
    ON turn_outbox (status, available_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_turn_outbox_turn_status ON turn_outbox (turn_id, status);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128),
    session_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128),
    tenant_id VARCHAR(128),
    turn_id VARCHAR(128),
    plan_id VARCHAR(128),
    step_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    invoker_type VARCHAR(64) NOT NULL,
    agent_revision INTEGER,
    handling_kind VARCHAR(32),
    external_ticket_issuance_state VARCHAR(16),
    binding_snapshot_text TEXT,
    delegated BOOLEAN DEFAULT FALSE NOT NULL,
    delegation_key VARCHAR(128),
    state_version INTEGER DEFAULT 1 NOT NULL,
    event_sequence INTEGER DEFAULT 0 NOT NULL,
    deadline_at TIMESTAMP WITH TIME ZONE,
    heartbeat_at TIMESTAMP WITH TIME ZONE,
    claim_owner VARCHAR(128),
    claim_token VARCHAR(128),
    claim_expires_at TIMESTAMP WITH TIME ZONE,
    terminal_event_id VARCHAR(128),
    input_text TEXT NOT NULL,
    output_text TEXT,
    error_text TEXT,
    latency_ms INTEGER,
    formation_suppressed BOOLEAN DEFAULT FALSE NOT NULL,
    formation_published_order INTEGER DEFAULT 0 NOT NULL,
    used_memory_ids_text TEXT DEFAULT '[]' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (run_id)
);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS user_id VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS plan_id VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS step_id VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS agent_revision INTEGER;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS handling_kind VARCHAR(32);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS external_ticket_issuance_state VARCHAR(16);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS binding_snapshot_text TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS turn_id VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS delegated BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS delegation_key VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS state_version INTEGER DEFAULT 1 NOT NULL;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS event_sequence INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS claim_owner VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS claim_token VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS terminal_event_id VARCHAR(128);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS formation_suppressed BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS formation_published_order INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS used_memory_ids_text TEXT DEFAULT '[]' NOT NULL;
CREATE INDEX IF NOT EXISTS ix_agent_runs_agent_id ON agent_runs (agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_request_id ON agent_runs (request_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_session_id ON agent_runs (session_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_status ON agent_runs (status);
CREATE INDEX IF NOT EXISTS ix_agent_runs_user_id ON agent_runs (user_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_tenant_id ON agent_runs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_turn_id ON agent_runs (turn_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_delegated ON agent_runs (delegated);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_delegation_key
    ON agent_runs (delegation_key) WHERE delegation_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_agent_runs_deadline_at ON agent_runs (deadline_at);
CREATE INDEX IF NOT EXISTS ix_agent_runs_claim_expires_at ON agent_runs (claim_expires_at);
CREATE INDEX IF NOT EXISTS ix_agent_runs_terminal_event_id ON agent_runs (terminal_event_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_owner_request_status
    ON agent_runs (tenant_id, user_id, request_id, status);

CREATE TABLE IF NOT EXISTS execution_tickets (
    ticket_hash VARCHAR(64) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    turn_id VARCHAR(128) NOT NULL,
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    plan_id VARCHAR(128),
    step_id VARCHAR(128),
    purpose VARCHAR(64) NOT NULL,
    claims_text TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    run_state_version INTEGER DEFAULT 1 NOT NULL,
    event_sequence INTEGER DEFAULT 0 NOT NULL,
    lease_owner VARCHAR(128),
    lease_token VARCHAR(128),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    consumed_event_id VARCHAR(128),
    consumed_at TIMESTAMP WITH TIME ZONE,
    canonical_reuse BOOLEAN DEFAULT FALSE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (ticket_hash)
);
ALTER TABLE execution_tickets ADD COLUMN IF NOT EXISTS canonical_reuse BOOLEAN DEFAULT FALSE NOT NULL;
CREATE INDEX IF NOT EXISTS ix_execution_tickets_request_id ON execution_tickets (request_id);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_run_id ON execution_tickets (run_id);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_turn_id ON execution_tickets (turn_id);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_tenant_id ON execution_tickets (tenant_id);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_user_id ON execution_tickets (user_id);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_agent_id ON execution_tickets (agent_id);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_plan_id ON execution_tickets (plan_id);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_step_id ON execution_tickets (step_id);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_purpose ON execution_tickets (purpose);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_status ON execution_tickets (status);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_lease_expires_at
    ON execution_tickets (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_execution_tickets_consumed_event_id
    ON execution_tickets (consumed_event_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_tickets_reusable_active_run_purpose
    ON execution_tickets (run_id, purpose)
    WHERE canonical_reuse AND status IN ('issued', 'claimed');

CREATE TABLE IF NOT EXISTS external_execution_acceptances (
    acceptance_id VARCHAR(128) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    executor_ref VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (acceptance_id)
);
CREATE INDEX IF NOT EXISTS ix_external_execution_acceptances_executor_ref
    ON external_execution_acceptances (executor_ref);

CREATE TABLE IF NOT EXISTS agent_results (
    result_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128),
    tenant_id VARCHAR(128),
    turn_id VARCHAR(128),
    plan_id VARCHAR(128),
    step_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    run_state_version INTEGER,
    message TEXT DEFAULT '' NOT NULL,
    formation_suppressed BOOLEAN DEFAULT FALSE NOT NULL,
    formation_published BOOLEAN DEFAULT FALSE NOT NULL,
    turn_captured BOOLEAN DEFAULT FALSE NOT NULL,
    output_text TEXT,
    artifact_refs_text TEXT NOT NULL,
    error_text TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (result_id)
);
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS user_id VARCHAR(128);
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(128);
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS plan_id VARCHAR(128);
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS step_id VARCHAR(128);
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS turn_id VARCHAR(128);
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS run_state_version INTEGER;
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS message TEXT DEFAULT '' NOT NULL;
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS formation_suppressed BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS formation_published BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE agent_results ADD COLUMN IF NOT EXISTS turn_captured BOOLEAN DEFAULT FALSE NOT NULL;
CREATE INDEX IF NOT EXISTS ix_agent_results_agent_id ON agent_results (agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_run_id ON agent_results (run_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_session_id ON agent_results (session_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_status ON agent_results (status);
CREATE INDEX IF NOT EXISTS ix_agent_results_user_id ON agent_results (user_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_tenant_id ON agent_results (tenant_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_plan_id ON agent_results (plan_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_turn_id ON agent_results (turn_id);
CREATE INDEX IF NOT EXISTS idx_agent_results_owner_run_status
    ON agent_results (tenant_id, user_id, run_id, status);

CREATE TABLE IF NOT EXISTS agent_events (
    event_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128),
    request_id VARCHAR(128),
    session_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128),
    tenant_id VARCHAR(128),
    turn_id VARCHAR(128),
    agent_session_id VARCHAR(128),
    event_type VARCHAR(64) NOT NULL,
    status VARCHAR(32),
    plan_id VARCHAR(128),
    step_id VARCHAR(128),
    sequence INTEGER,
    run_state_version INTEGER,
    payload_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (event_id)
);
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS user_id VARCHAR(128);
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(128);
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS turn_id VARCHAR(128);
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS sequence INTEGER;
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS run_state_version INTEGER;
CREATE INDEX IF NOT EXISTS ix_agent_events_agent_id ON agent_events (agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_event_type ON agent_events (event_type);
CREATE INDEX IF NOT EXISTS ix_agent_events_plan_id ON agent_events (plan_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_run_id ON agent_events (run_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_session_id ON agent_events (session_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_user_id ON agent_events (user_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_tenant_id ON agent_events (tenant_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_turn_id ON agent_events (turn_id);

CREATE TABLE IF NOT EXISTS plans (
    plan_id VARCHAR(128) NOT NULL,
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
    execution_claim_expires_at TIMESTAMP WITH TIME ZONE,
    original_query TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (plan_id)
);
ALTER TABLE plans ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(128);
ALTER TABLE plans ADD COLUMN IF NOT EXISTS state_version INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS formation_published_version INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS execution_claim_id VARCHAR(128);
ALTER TABLE plans ADD COLUMN IF NOT EXISTS execution_claim_step_id VARCHAR(128);
ALTER TABLE plans ADD COLUMN IF NOT EXISTS execution_claim_state_version INTEGER;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS execution_claim_key VARCHAR(128);
ALTER TABLE plans ADD COLUMN IF NOT EXISTS execution_attempt INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS execution_claim_expires_at TIMESTAMP WITH TIME ZONE;
DELETE FROM plans
WHERE user_id IS NULL OR btrim(user_id) = '' OR tenant_id IS NULL OR btrim(tenant_id) = '';
ALTER TABLE plans ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE plans ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_plans_session_id ON plans (session_id);
CREATE INDEX IF NOT EXISTS ix_plans_status ON plans (status);
CREATE INDEX IF NOT EXISTS ix_plans_user_id ON plans (user_id);
CREATE INDEX IF NOT EXISTS ix_plans_tenant_id ON plans (tenant_id);
CREATE INDEX IF NOT EXISTS ix_plans_execution_claim_id ON plans (execution_claim_id);
CREATE INDEX IF NOT EXISTS ix_plans_execution_claim_expires_at ON plans (execution_claim_expires_at);

CREATE TABLE IF NOT EXISTS plan_steps (
    id SERIAL NOT NULL,
    step_id VARCHAR(128) NOT NULL,
    plan_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    description TEXT NOT NULL,
    depends_on_text TEXT NOT NULL,
    artifact_refs_text TEXT NOT NULL,
    agent_revision INTEGER,
    binding_requirement_text TEXT,
    CONSTRAINT ck_plan_steps_binding_pair CHECK (
        (agent_revision IS NULL AND binding_requirement_text IS NULL)
        OR (agent_revision IS NOT NULL AND binding_requirement_text IS NOT NULL)
    ),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_plan_steps_agent_id ON plan_steps (agent_id);
CREATE INDEX IF NOT EXISTS ix_plan_steps_plan_id ON plan_steps (plan_id);
CREATE INDEX IF NOT EXISTS ix_plan_steps_status ON plan_steps (status);
CREATE INDEX IF NOT EXISTS ix_plan_steps_step_id ON plan_steps (step_id);
CREATE INDEX IF NOT EXISTS idx_plan_steps_plan_step ON plan_steps (plan_id, step_id);
ALTER TABLE plan_steps ADD COLUMN IF NOT EXISTS agent_revision INTEGER;
ALTER TABLE plan_steps ADD COLUMN IF NOT EXISTS binding_requirement_text TEXT;
UPDATE plan_steps
SET agent_revision = NULL, binding_requirement_text = NULL
WHERE (agent_revision IS NULL AND binding_requirement_text IS NOT NULL)
   OR (agent_revision IS NOT NULL AND binding_requirement_text IS NULL);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_plan_steps_binding_pair'
          AND conrelid = 'plan_steps'::regclass
    ) THEN
        ALTER TABLE plan_steps
        ADD CONSTRAINT ck_plan_steps_binding_pair CHECK (
            (agent_revision IS NULL AND binding_requirement_text IS NULL)
            OR (agent_revision IS NOT NULL AND binding_requirement_text IS NOT NULL)
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS route_logs (
    id SERIAL NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    model_name VARCHAR(128) NOT NULL,
    candidate_agent_ids_text TEXT NOT NULL,
    prompt_summary TEXT NOT NULL,
    evidence_text TEXT NOT NULL,
    raw_output_text TEXT,
    parsed_output_text TEXT,
    validation_status VARCHAR(32) NOT NULL,
    error_text TEXT,
    latency_ms INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_route_logs_request_id ON route_logs (request_id);
CREATE INDEX IF NOT EXISTS ix_route_logs_session_id ON route_logs (session_id);

CREATE TABLE IF NOT EXISTS memory_items (
    memory_id VARCHAR(128) NOT NULL,
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
    memory_key VARCHAR(512),
    candidate_hash VARCHAR(128),
    current_revision_id VARCHAR(128),
    formation_job_id VARCHAR(128),
    lifecycle_status VARCHAR(32),
    index_status VARCHAR(32),
    canonical_refs_text TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (memory_id)
);
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS memory_key VARCHAR(512);
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS candidate_hash VARCHAR(128);
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS current_revision_id VARCHAR(128);
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS formation_job_id VARCHAR(128);
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(32);
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS index_status VARCHAR(32);
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS canonical_refs_text TEXT DEFAULT '[]';
CREATE INDEX IF NOT EXISTS ix_memory_items_agent_id ON memory_items (agent_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_scope ON memory_items (scope);
CREATE INDEX IF NOT EXISTS ix_memory_items_subject_id ON memory_items (subject_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_tenant_id ON memory_items (tenant_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_user_id ON memory_items (user_id);
CREATE INDEX IF NOT EXISTS idx_memory_items_subject_scope ON memory_items (subject_type, subject_id, scope);
CREATE INDEX IF NOT EXISTS idx_memory_items_user_tenant ON memory_items (user_id, tenant_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_memory_key ON memory_items (memory_key);
CREATE INDEX IF NOT EXISTS ix_memory_items_candidate_hash ON memory_items (candidate_hash);
CREATE INDEX IF NOT EXISTS ix_memory_items_current_revision_id ON memory_items (current_revision_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_formation_job_id ON memory_items (formation_job_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_lifecycle_status ON memory_items (lifecycle_status);
CREATE INDEX IF NOT EXISTS ix_memory_items_index_status ON memory_items (index_status);
CREATE INDEX IF NOT EXISTS idx_memory_items_lifecycle_index ON memory_items (lifecycle_status, index_status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_items_active_key
ON memory_items (tenant_id, subject_type, subject_id, scope, memory_key)
WHERE lifecycle_status = 'active';

CREATE TABLE IF NOT EXISTS memory_revisions (
    revision_id VARCHAR(128) NOT NULL,
    memory_id VARCHAR(128) NOT NULL,
    revision_no INTEGER NOT NULL,
    memory_key VARCHAR(512) NOT NULL,
    operation VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    structured_value_text TEXT NOT NULL,
    evidence_refs_text TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    policy_version VARCHAR(128) NOT NULL,
    supersedes_revision_id VARCHAR(128),
    formation_job_id VARCHAR(128),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (revision_id),
    CONSTRAINT uq_memory_revisions_number UNIQUE (memory_id, revision_no)
);
CREATE INDEX IF NOT EXISTS ix_memory_revisions_memory_id ON memory_revisions (memory_id);
CREATE INDEX IF NOT EXISTS ix_memory_revisions_memory_key ON memory_revisions (memory_key);
CREATE INDEX IF NOT EXISTS ix_memory_revisions_formation_job_id ON memory_revisions (formation_job_id);
CREATE INDEX IF NOT EXISTS idx_memory_revisions_key_created ON memory_revisions (memory_key, created_at);

CREATE TABLE IF NOT EXISTS memory_formation_turns (
    turn_id VARCHAR(128) NOT NULL,
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
    idle_deadline_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (turn_id),
    CONSTRAINT uq_memory_formation_turns_owner_request
        UNIQUE (tenant_id, user_id, session_id, request_id)
);
ALTER TABLE memory_formation_turns
    DROP CONSTRAINT IF EXISTS uq_memory_formation_turns_request;
ALTER TABLE memory_formation_turns
    DROP CONSTRAINT IF EXISTS memory_formation_turns_request_id_key;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_memory_formation_turns_owner_request'
          AND conrelid = 'memory_formation_turns'::regclass
    ) THEN
        ALTER TABLE memory_formation_turns
            ADD CONSTRAINT uq_memory_formation_turns_owner_request
            UNIQUE (tenant_id, user_id, session_id, request_id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_memory_formation_turns_pending_idle
ON memory_formation_turns (tenant_id, user_id, session_id, status, idle_deadline_at);
CREATE INDEX IF NOT EXISTS ix_memory_formation_turns_claimed_job_id ON memory_formation_turns (claimed_job_id);

CREATE TABLE IF NOT EXISTS memory_formation_jobs (
    job_id VARCHAR(128) NOT NULL,
    trigger VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    mode VARCHAR(32) NOT NULL,
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128),
    first_turn_id VARCHAR(128),
    last_turn_id VARCHAR(128),
    source_refs_text TEXT NOT NULL,
    idempotency_key VARCHAR(512) NOT NULL,
    model_version VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(128) NOT NULL,
    policy_version VARCHAR(128) NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    lease_owner VARCHAR(128),
    lease_token VARCHAR(128),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    next_attempt_at TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(128),
    trace_summary_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (job_id),
    CONSTRAINT uq_memory_formation_jobs_idempotency UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_formation_jobs_claim
ON memory_formation_jobs (status, next_attempt_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_memory_formation_jobs_range
ON memory_formation_jobs (tenant_id, user_id, session_id, first_turn_id, last_turn_id);
ALTER TABLE memory_formation_jobs ADD COLUMN IF NOT EXISTS lease_token VARCHAR(128);

CREATE TABLE IF NOT EXISTS memory_index_operations (
    index_operation_id VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(512) NOT NULL,
    operation VARCHAR(32) NOT NULL,
    memory_id VARCHAR(128) NOT NULL,
    revision_id VARCHAR(128),
    tenant_id VARCHAR(128) NOT NULL,
    external_memory_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    lease_owner VARCHAR(128),
    lease_token VARCHAR(128),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    next_attempt_at TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(128),
    last_error_metadata_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (index_operation_id),
    CONSTRAINT uq_memory_index_operations_idempotency UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_index_operations_claim
ON memory_index_operations (status, next_attempt_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_memory_index_operations_repair
ON memory_index_operations (tenant_id, memory_id, status);
ALTER TABLE memory_index_operations ADD COLUMN IF NOT EXISTS lease_token VARCHAR(128);

UPDATE memory_items
SET memory_key = 'legacy:' || COALESCE(tenant_id, 'none') || ':' || memory_id,
    lifecycle_status = COALESCE(lifecycle_status, 'active'),
    index_status = COALESCE(
        index_status,
        CASE WHEN metadata_text LIKE '%mem0_memory_id%' THEN 'ready' ELSE 'pending' END
    ),
    canonical_refs_text = COALESCE(canonical_refs_text, '[]')
WHERE memory_key IS NULL OR lifecycle_status IS NULL OR index_status IS NULL;

INSERT INTO memory_revisions (
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
ON CONFLICT DO NOTHING;

UPDATE memory_items
SET current_revision_id = 'mrev_legacy_' || memory_id
WHERE current_revision_id IS NULL AND lifecycle_status = 'active';

CREATE TABLE IF NOT EXISTS memory_events (
    event_id VARCHAR(128) NOT NULL,
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
    decision_id VARCHAR(128),
    scope VARCHAR(64),
    payload_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (event_id)
);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS request_id VARCHAR(128);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS session_id VARCHAR(128);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS turn_id VARCHAR(128);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS run_id VARCHAR(128);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS formation_job_id VARCHAR(128);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS memory_key VARCHAR(512);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS decision_status VARCHAR(32);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS decision_id VARCHAR(128);
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS scope VARCHAR(64);
UPDATE memory_events
SET decision_id = payload_text::jsonb ->> 'decision_id'
WHERE decision_id IS NULL
  AND event_type IN (
      'memory_pending_confirm',
      'memory_pending_reject',
      'memory_pending_conflict'
  )
  AND jsonb_typeof(payload_text::jsonb -> 'decision_id') = 'string'
  AND length(payload_text::jsonb ->> 'decision_id') BETWEEN 1 AND 128;
CREATE INDEX IF NOT EXISTS ix_memory_events_agent_id ON memory_events (agent_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_event_type ON memory_events (event_type);
CREATE INDEX IF NOT EXISTS ix_memory_events_memory_id ON memory_events (memory_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_tenant_id ON memory_events (tenant_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_user_id ON memory_events (user_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_request_id ON memory_events (request_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_session_id ON memory_events (session_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_turn_id ON memory_events (turn_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_run_id ON memory_events (run_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_formation_job_id ON memory_events (formation_job_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_memory_key ON memory_events (memory_key);
CREATE INDEX IF NOT EXISTS ix_memory_events_decision_status ON memory_events (decision_status);
CREATE INDEX IF NOT EXISTS ix_memory_events_decision_id ON memory_events (decision_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_scope ON memory_events (scope);

COMMIT;

RESET ROLE;
