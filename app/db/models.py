from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AgentDefinitionModel(Base):
    __tablename__ = "agent_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    type: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    capabilities_text: Mapped[str] = mapped_column(Text, default="[]")
    tags_text: Mapped[str] = mapped_column(Text, default="[]")
    trigger_text: Mapped[str] = mapped_column(Text, default="{}")
    access_policy_text: Mapped[str] = mapped_column(Text, default="{}")
    required_inputs_text: Mapped[str] = mapped_column(Text, default="[]")
    optional_inputs_text: Mapped[str] = mapped_column(Text, default="[]")
    input_schema_text: Mapped[str] = mapped_column(
        Text, default='{"type":"object","properties":{}}'
    )
    output_schema_text: Mapped[str] = mapped_column(
        Text, default='{"type":"object","properties":{}}'
    )
    invocation_text: Mapped[str] = mapped_column(Text, default="{}")
    ui_handoff_text: Mapped[str] = mapped_column(Text, default="{}")
    context_text: Mapped[str] = mapped_column(Text, default="{}")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    metadata_text: Mapped[str] = mapped_column(Text, default="{}")
    source: Mapped[str] = mapped_column(String(64), default="database")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class RegistryRevisionModel(Base):
    __tablename__ = "registry_revisions"

    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    operator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    before_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMessageModel(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("idx_chat_messages_session_created", "session_id", "created_at"),
        Index("idx_chat_messages_session_source_created", "session_id", "source", "created_at"),
        Index(
            "idx_chat_messages_agent_context_created",
            "session_id",
            "source",
            "agent_id",
            "agent_session_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_text: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationEventModel(Base):
    __tablename__ = "conversation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_text: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CanonicalTurnModel(Base):
    __tablename__ = "canonical_turns"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "request_id",
            name="uq_canonical_turns_owner_request",
        ),
        Index(
            "idx_canonical_turns_owner_session_status",
            "tenant_id",
            "user_id",
            "session_id",
            "status",
        ),
        Index("idx_canonical_turns_status_updated", "status", "updated_at"),
        Index(
            "idx_canonical_turns_owner_status_updated",
            "tenant_id",
            "user_id",
            "status",
            "updated_at",
        ),
    )

    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    user_input_text: Mapped[str] = mapped_column(Text, nullable=False)
    references_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    final_response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TurnOutboxModel(Base):
    __tablename__ = "turn_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_turn_outbox_idempotency"),
        Index("idx_turn_outbox_claim", "status", "available_at", "lease_expires_at"),
        Index("idx_turn_outbox_turn_status", "turn_id", "status"),
    )

    outbox_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    turn_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("canonical_turns.turn_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    payload_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentRunModel(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index(
            "idx_agent_runs_owner_request_status",
            "tenant_id",
            "user_id",
            "request_id",
            "status",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    plan_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    invoker_type: Mapped[str] = mapped_column(String(64))
    delegated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    delegation_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True, index=True
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    terminal_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    input_text: Mapped[str] = mapped_column(Text, default="{}")
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    formation_suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    formation_published_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_memory_ids_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExecutionTicketModel(Base):
    __tablename__ = "execution_tickets"

    ticket_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    turn_id: Mapped[str] = mapped_column(String(128), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    step_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(64), index=True)
    claims_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    run_state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    consumed_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentResultModel(Base):
    __tablename__ = "agent_results"
    __table_args__ = (
        Index(
            "idx_agent_results_owner_run_status",
            "tenant_id",
            "user_id",
            "run_id",
            "status",
        ),
    )

    result_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    plan_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    run_state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    formation_suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    formation_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    turn_captured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_refs_text: Mapped[str] = mapped_column(Text, default="[]")
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentEventModel(Base):
    __tablename__ = "agent_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_text: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanModel(Base):
    __tablename__ = "plans"

    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    formation_published_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_claim_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    execution_claim_step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_claim_state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_claim_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    original_query: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class PlanStepModel(Base):
    __tablename__ = "plan_steps"
    __table_args__ = (Index("idx_plan_steps_plan_step", "plan_id", "step_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    step_id: Mapped[str] = mapped_column(String(128), index=True)
    plan_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str] = mapped_column(Text)
    depends_on_text: Mapped[str] = mapped_column(Text, default="[]")
    artifact_refs_text: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class RouteLogModel(Base):
    __tablename__ = "route_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    model_name: Mapped[str] = mapped_column(String(128))
    candidate_agent_ids_text: Mapped[str] = mapped_column(Text, default="[]")
    prompt_summary: Mapped[str] = mapped_column(Text, default="")
    evidence_text: Mapped[str] = mapped_column(Text, default="[]")
    raw_output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(32), default="ok")
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryItemModel(Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        Index("idx_memory_items_subject_scope", "subject_type", "subject_id", "scope"),
        Index("idx_memory_items_user_tenant", "user_id", "tenant_id"),
        Index("idx_memory_items_lifecycle_index", "lifecycle_status", "index_status"),
        UniqueConstraint(
            "tenant_id",
            "subject_type",
            "subject_id",
            "scope",
            "memory_key",
            "lifecycle_status",
            name="uq_memory_items_current_key_lifecycle",
        ),
    )

    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), default="user")
    subject_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    structured_value_text: Mapped[str] = mapped_column(Text, default="{}")
    source: Mapped[str] = mapped_column(String(64), default="manual")
    confidence: Mapped[int] = mapped_column(Integer, default=100)
    importance: Mapped[int] = mapped_column(Integer, default=50)
    visibility: Mapped[str] = mapped_column(String(32), default="user")
    ttl_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_text: Mapped[str] = mapped_column(Text, default="{}")
    memory_key: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    candidate_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    current_revision_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    formation_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lifecycle_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default="active", index=True
    )
    index_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    canonical_refs_text: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class MemoryEventModel(Base):
    __tablename__ = "memory_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    memory_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    formation_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    memory_key: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    decision_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    scope: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload_text: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryRevisionModel(Base):
    __tablename__ = "memory_revisions"
    __table_args__ = (
        UniqueConstraint("memory_id", "revision_no", name="uq_memory_revisions_number"),
        Index("idx_memory_revisions_key_created", "memory_key", "created_at"),
    )

    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(128), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    memory_key: Mapped[str] = mapped_column(String(512), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    structured_value_text: Mapped[str] = mapped_column(Text, default="{}")
    evidence_refs_text: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[int] = mapped_column(Integer, default=100)
    policy_version: Mapped[str] = mapped_column(String(128))
    supersedes_revision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    formation_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryFormationTurnModel(Base):
    __tablename__ = "memory_formation_turns"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "session_id",
            "request_id",
            name="uq_memory_formation_turns_owner_request",
        ),
        Index(
            "idx_memory_formation_turns_pending_idle",
            "tenant_id",
            "user_id",
            "session_id",
            "status",
            "idle_deadline_at",
        ),
    )

    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    user_text: Mapped[str] = mapped_column(Text, default="")
    assistant_text: Mapped[str] = mapped_column(Text, default="")
    result_status: Mapped[str] = mapped_column(String(32))
    source_refs_text: Mapped[str] = mapped_column(Text, default="[]")
    used_memory_ids_text: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    claimed_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    idle_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MemoryFormationJobModel(Base):
    __tablename__ = "memory_formation_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_memory_formation_jobs_idempotency"),
        Index(
            "idx_memory_formation_jobs_claim",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        Index(
            "idx_memory_formation_jobs_range",
            "tenant_id",
            "user_id",
            "session_id",
            "first_turn_id",
            "last_turn_id",
        ),
    )

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    mode: Mapped[str] = mapped_column(String(32))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    first_turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_refs_text: Mapped[str] = mapped_column(Text, default="[]")
    idempotency_key: Mapped[str] = mapped_column(String(512))
    model_version: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[str] = mapped_column(String(128))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_summary_text: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MemoryIndexOperationModel(Base):
    __tablename__ = "memory_index_operations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_memory_index_operations_idempotency"),
        Index(
            "idx_memory_index_operations_claim",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        Index("idx_memory_index_operations_repair", "tenant_id", "memory_id", "status"),
    )

    index_operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(512))
    operation: Mapped[str] = mapped_column(String(32))
    memory_id: Mapped[str] = mapped_column(String(128), index=True)
    revision_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    external_memory_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_metadata_text: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeSourceModel(Base):
    __tablename__ = "knowledge_sources"

    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    allow_roles_text: Mapped[str] = mapped_column(Text, default="[]")
    allow_groups_text: Mapped[str] = mapped_column(Text, default="[]")
    allow_tenants_text: Mapped[str] = mapped_column(Text, default="[]")
    tags_text: Mapped[str] = mapped_column(Text, default="[]")
    metadata_text: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class KnowledgeChunkModel(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("idx_knowledge_chunks_source", "source_id"),)

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    content: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_text: Mapped[str] = mapped_column(Text, default="[]")
    metadata_text: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeRetrievalLogModel(Base):
    __tablename__ = "knowledge_retrieval_logs"

    log_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    caller_type: Mapped[str] = mapped_column(String(64), index=True)
    caller_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    selected_source_ids_text: Mapped[str] = mapped_column(Text, default="[]")
    denied_source_ids_text: Mapped[str] = mapped_column(Text, default="[]")
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="ok", index=True)
    errors_text: Mapped[str] = mapped_column(Text, default="[]")
    metadata_text: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeAssetGroupModel(Base):
    __tablename__ = "knowledge_asset_groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_asset_groups_tenant_name"),
    )

    group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stable_asset_keys_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class KnowledgeAssetModel(Base):
    __tablename__ = "knowledge_assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "stable_key", name="uq_knowledge_assets_tenant_key"),
        Index("idx_knowledge_assets_tenant_status", "tenant_id", "status"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stable_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    group_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("knowledge_asset_groups.group_id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    access_policy_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    tags_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    parser_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chunking_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeAssetChunkModel(Base):
    __tablename__ = "knowledge_asset_chunks"
    __table_args__ = (
        UniqueConstraint("asset_id", "ordinal", name="uq_knowledge_asset_chunks_ordinal"),
        Index("idx_knowledge_asset_chunks_tenant_status", "tenant_id", "status"),
    )

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("knowledge_assets.asset_id", ondelete="RESTRICT"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ref_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    citation_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    metadata_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeImportJobModel(Base):
    __tablename__ = "knowledge_import_jobs"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("knowledge_assets.asset_id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage_history_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    warnings_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeMigrationManifestModel(Base):
    __tablename__ = "knowledge_migration_manifests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "file_hash",
            "pipeline_version",
            name="uq_knowledge_manifest_pipeline",
        ),
    )

    manifest_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    pipeline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chunk_ids_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    collection_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    migration_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_refs_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeOperationTraceModel(Base):
    __tablename__ = "knowledge_operation_traces"

    trace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    caller: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    policy_outcome: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence_ids_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    warnings_text: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
