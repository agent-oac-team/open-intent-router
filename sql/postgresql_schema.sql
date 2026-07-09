-- OIR PostgreSQL schema snapshot.
-- Source of truth: app/db/models.py
-- Usage: psql postgresql://oac:oac@127.0.0.1:5432/oac -f sql/postgresql_schema.sql

BEGIN;

CREATE TABLE IF NOT EXISTS agent_definitions (
    id SERIAL NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    version VARCHAR(64),
    type VARCHAR(64) NOT NULL,
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

CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL NOT NULL,
    message_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128),
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
CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_messages_message_id ON chat_messages (message_id);
CREATE INDEX IF NOT EXISTS ix_chat_messages_session_id ON chat_messages (session_id);
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

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(128),
    session_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    invoker_type VARCHAR(64) NOT NULL,
    input_text TEXT NOT NULL,
    output_text TEXT,
    error_text TEXT,
    latency_ms INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (run_id)
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_agent_id ON agent_runs (agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_request_id ON agent_runs (request_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_session_id ON agent_runs (session_id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_status ON agent_runs (status);

CREATE TABLE IF NOT EXISTS agent_results (
    result_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    output_text TEXT,
    artifact_refs_text TEXT NOT NULL,
    error_text TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (result_id)
);
-- 兼容旧开发库：早期 agent_results 以 event/plan 字段为主，缺少当前模型字段。
ALTER TABLE IF EXISTS agent_results ADD COLUMN IF NOT EXISTS run_id VARCHAR(128);
ALTER TABLE IF EXISTS agent_results ADD COLUMN IF NOT EXISTS error_text TEXT;
CREATE INDEX IF NOT EXISTS ix_agent_results_agent_id ON agent_results (agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_run_id ON agent_results (run_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_session_id ON agent_results (session_id);
CREATE INDEX IF NOT EXISTS ix_agent_results_status ON agent_results (status);

CREATE TABLE IF NOT EXISTS agent_events (
    event_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128),
    request_id VARCHAR(128),
    session_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    agent_session_id VARCHAR(128),
    event_type VARCHAR(64) NOT NULL,
    status VARCHAR(32),
    plan_id VARCHAR(128),
    step_id VARCHAR(128),
    payload_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS ix_agent_events_agent_id ON agent_events (agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_event_type ON agent_events (event_type);
CREATE INDEX IF NOT EXISTS ix_agent_events_plan_id ON agent_events (plan_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_run_id ON agent_events (run_id);
CREATE INDEX IF NOT EXISTS ix_agent_events_session_id ON agent_events (session_id);

CREATE TABLE IF NOT EXISTS plans (
    plan_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    current_step_id VARCHAR(128),
    original_query TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (plan_id)
);
CREATE INDEX IF NOT EXISTS ix_plans_session_id ON plans (session_id);
CREATE INDEX IF NOT EXISTS ix_plans_status ON plans (status);

CREATE TABLE IF NOT EXISTS plan_steps (
    id SERIAL NOT NULL,
    step_id VARCHAR(128) NOT NULL,
    plan_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    description TEXT NOT NULL,
    depends_on_text TEXT NOT NULL,
    artifact_refs_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_plan_steps_agent_id ON plan_steps (agent_id);
CREATE INDEX IF NOT EXISTS ix_plan_steps_plan_id ON plan_steps (plan_id);
CREATE INDEX IF NOT EXISTS ix_plan_steps_status ON plan_steps (status);
CREATE INDEX IF NOT EXISTS ix_plan_steps_step_id ON plan_steps (step_id);
CREATE INDEX IF NOT EXISTS idx_plan_steps_plan_step ON plan_steps (plan_id, step_id);

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
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (memory_id)
);
CREATE INDEX IF NOT EXISTS ix_memory_items_agent_id ON memory_items (agent_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_scope ON memory_items (scope);
CREATE INDEX IF NOT EXISTS ix_memory_items_subject_id ON memory_items (subject_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_tenant_id ON memory_items (tenant_id);
CREATE INDEX IF NOT EXISTS ix_memory_items_user_id ON memory_items (user_id);
CREATE INDEX IF NOT EXISTS idx_memory_items_subject_scope ON memory_items (subject_type, subject_id, scope);
CREATE INDEX IF NOT EXISTS idx_memory_items_user_tenant ON memory_items (user_id, tenant_id);

CREATE TABLE IF NOT EXISTS memory_events (
    event_id VARCHAR(128) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    memory_id VARCHAR(128),
    user_id VARCHAR(128),
    tenant_id VARCHAR(128),
    agent_id VARCHAR(128),
    payload_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS ix_memory_events_agent_id ON memory_events (agent_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_event_type ON memory_events (event_type);
CREATE INDEX IF NOT EXISTS ix_memory_events_memory_id ON memory_events (memory_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_tenant_id ON memory_events (tenant_id);
CREATE INDEX IF NOT EXISTS ix_memory_events_user_id ON memory_events (user_id);

CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id VARCHAR(128) NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    allow_roles_text TEXT NOT NULL,
    allow_groups_text TEXT NOT NULL,
    allow_tenants_text TEXT NOT NULL,
    tags_text TEXT NOT NULL,
    metadata_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (source_id)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_sources_enabled ON knowledge_sources (enabled);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id VARCHAR(128) NOT NULL,
    source_id VARCHAR(128) NOT NULL,
    content TEXT NOT NULL,
    title VARCHAR(300),
    uri TEXT,
    tags_text TEXT NOT NULL,
    metadata_text TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (chunk_id)
);
-- 兼容旧开发库：早期 knowledge_chunks 以 asset/index 字段为主，当前模型按 source/chunk 简化。
ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS source_id VARCHAR(128);
ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS uri TEXT;
ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS tags_text TEXT;
CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_source_id ON knowledge_chunks (source_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_source ON knowledge_chunks (source_id);

CREATE TABLE IF NOT EXISTS knowledge_retrieval_logs (
    log_id VARCHAR(128) NOT NULL,
    query TEXT NOT NULL,
    caller_type VARCHAR(64) NOT NULL,
    caller_id VARCHAR(128),
    purpose VARCHAR(64) NOT NULL,
    user_id VARCHAR(128),
    tenant_id VARCHAR(128),
    selected_source_ids_text TEXT NOT NULL,
    denied_source_ids_text TEXT NOT NULL,
    hit_count INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    errors_text TEXT NOT NULL,
    metadata_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (log_id)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_retrieval_logs_caller_id ON knowledge_retrieval_logs (caller_id);
CREATE INDEX IF NOT EXISTS ix_knowledge_retrieval_logs_caller_type ON knowledge_retrieval_logs (caller_type);
CREATE INDEX IF NOT EXISTS ix_knowledge_retrieval_logs_purpose ON knowledge_retrieval_logs (purpose);
CREATE INDEX IF NOT EXISTS ix_knowledge_retrieval_logs_status ON knowledge_retrieval_logs (status);
CREATE INDEX IF NOT EXISTS ix_knowledge_retrieval_logs_tenant_id ON knowledge_retrieval_logs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_knowledge_retrieval_logs_user_id ON knowledge_retrieval_logs (user_id);

COMMIT;
