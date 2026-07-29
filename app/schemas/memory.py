import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, JsonValue, model_validator

from app.schemas.agent_context import MemoryContext, MemoryContextItem, MemoryScope
from app.schemas.common import JsonDict, StrictBaseModel, UserContext

MemoryWriteStatus = Literal["accepted", "rejected", "pending", "expired"]
MemoryVisibility = Literal["user", "agent", "tenant", "system"]
FormationIdentifier = Annotated[str, Field(min_length=1, max_length=128)]


class MemoryFormationTrigger(StrEnum):
    TURN_WINDOW = "turn_window"
    IDLE = "idle"
    STRUCTURED_EVENT = "structured_event"
    MANUAL = "manual"
    SWEEPER = "sweeper"
    CONSOLIDATION = "consolidation"


class MemoryFormationMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    ENFORCED = "enforced"


class MemoryFormationJobStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RETRY = "retry"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"
    SKIPPED = "skipped"


class MemoryOperation(StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    NOOP = "noop"
    REJECT = "reject"
    PENDING = "pending"
    IGNORE = "ignore"
    CONSOLIDATE = "consolidate"


class MemoryCandidateOperation(StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    IGNORE = "ignore"


class MemorySemanticTarget(StrEnum):
    USER_PROFILE = "user_profile"
    ASSISTANT_RESPONSE = "assistant_response"
    TASK = "task"
    ARTIFACT = "artifact"
    SESSION = "session"
    MEMORY = "memory"
    UNKNOWN = "unknown"


class MemoryTemporalScope(StrEnum):
    CURRENT_TURN = "current_turn"
    SESSION = "session"
    LONG_TERM = "long_term"
    CANONICAL = "canonical"
    UNKNOWN = "unknown"


class MemoryPolarity(StrEnum):
    AFFIRMED = "affirmed"
    NEGATED = "negated"
    UNKNOWN = "unknown"


class MemorySemanticCertainty(StrEnum):
    CERTAIN = "certain"
    UNCERTAIN = "uncertain"


class MemorySemanticVerifierVerdict(StrEnum):
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    UNCERTAIN = "uncertain"


class MemoryChangeIntent(StrEnum):
    SET = "set"
    REPLACE = "replace"
    DELETE = "delete"
    NONE = "none"
    UNKNOWN = "unknown"


class MemoryRevisionOperation(StrEnum):
    ADD = "add"
    UPDATE = "update"
    CONSOLIDATE = "consolidate"


class MemoryIndexOperationType(StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


class MemoryDecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING = "pending"
    NOOP = "noop"
    OBSERVED = "observed"
    RESOLVED = "resolved"


class MemoryLifecycleStatus(StrEnum):
    ACTIVE = "active"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


class MemoryIndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    OUT_OF_SYNC = "out_of_sync"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"
    DEAD_LETTER = "dead_letter"


class MemoryIndexOperationStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RETRY = "retry"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"


class MemoryFormationReasonCode(StrEnum):
    ACCEPTED_NEW = "accepted_new"
    ACCEPTED_UPDATE = "accepted_update"
    AUTHORIZED_DELETE = "authorized_delete"
    SAME_VALUE = "same_value"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    CONFIDENCE_PENDING = "confidence_pending"
    CONFIDENCE_LOW = "confidence_low"
    AMBIGUOUS_CONFLICT = "ambiguous_conflict"
    AMBIGUOUS_DELETE = "ambiguous_delete"
    CURRENT_TURN_OVERRIDE = "current_turn_override"
    ASSISTANT_ONLY_EVIDENCE = "assistant_only_evidence"
    RECALLED_MEMORY_REPETITION = "recalled_memory_repetition"
    SENSITIVE_CONTENT = "sensitive_content"
    INVALID_EVIDENCE = "invalid_evidence"
    INVALID_SCOPE = "invalid_scope"
    IDENTITY_MISMATCH = "identity_mismatch"
    TEMPORARY_REQUEST = "temporary_request"
    TTL_EXPIRED = "ttl_expired"
    PROVIDER_ERROR = "provider_error"


class MemoryEvidenceRef(StrictBaseModel):
    turn_id: FormationIdentifier | None = None
    event_id: FormationIdentifier | None = None
    role: Literal["user", "assistant", "agent", "system", "canonical"]
    quote: str | None = Field(default=None, max_length=500)
    content_hash: str | None = Field(default=None, max_length=128)


class MemoryCandidateSemantics(StrictBaseModel):
    target: MemorySemanticTarget
    slot: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.~-]*$")
    value: JsonValue
    temporal_scope: MemoryTemporalScope
    polarity: MemoryPolarity
    certainty: MemorySemanticCertainty
    change_intent: MemoryChangeIntent

    @model_validator(mode="after")
    def validate_value_is_bounded_strict_json(self) -> "MemoryCandidateSemantics":
        try:
            encoded = json.dumps(
                self.value,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("semantic value must be strict JSON") from exc
        if len(encoded) > 8192:
            raise ValueError("semantic value exceeds 8192 bytes")
        return self


class MemorySemanticVerification(StrictBaseModel):
    verdict: MemorySemanticVerifierVerdict
    confidence: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=64)
    evidence_refs: list[MemoryEvidenceRef] = Field(default_factory=list, max_length=20)


class MemoryFormationTurn(StrictBaseModel):
    turn_id: FormationIdentifier
    request_id: FormationIdentifier
    session_id: FormationIdentifier
    run_id: FormationIdentifier | None = None
    user_id: FormationIdentifier
    tenant_id: FormationIdentifier
    agent_id: FormationIdentifier | None = None
    user_text: str = Field(default="", max_length=20000)
    assistant_text: str = Field(default="", max_length=20000)
    result_status: str = Field(min_length=1, max_length=32)
    result_refs: list[FormationIdentifier] = Field(default_factory=list, max_length=50)
    plan_refs: list[FormationIdentifier] = Field(default_factory=list, max_length=50)
    artifact_refs: list[FormationIdentifier] = Field(default_factory=list, max_length=50)
    used_memory_ids: list[FormationIdentifier] = Field(default_factory=list, max_length=50)
    idle_deadline_at: datetime | None = None
    status: Literal["pending", "claimed", "formed", "skipped"] = "pending"
    claimed_job_id: str | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryFormationJob(StrictBaseModel):
    job_id: str = Field(default_factory=lambda: f"mfjob_{uuid4().hex}")
    trigger: MemoryFormationTrigger
    status: MemoryFormationJobStatus = MemoryFormationJobStatus.PENDING
    mode: MemoryFormationMode
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str | None = None
    first_turn_id: str | None = None
    last_turn_id: str | None = None
    source_refs: list[FormationIdentifier] = Field(default_factory=list, max_length=100)
    idempotency_key: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error_code: str | None = None
    trace_summary: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryFormationCandidate(StrictBaseModel):
    candidate_id: str = Field(
        default_factory=lambda: f"mfc_{uuid4().hex}", min_length=1, max_length=128
    )
    proposed_operation: MemoryCandidateOperation
    scope: MemoryScope | str = Field(min_length=1, max_length=64)
    content: str = Field(max_length=2000)
    structured_value: JsonDict = Field(default_factory=dict, max_length=50)
    semantic: MemoryCandidateSemantics | None = None
    subject_type: str = Field(default="user", min_length=1, max_length=64)
    subject_id_hint: str | None = Field(default=None, max_length=128)
    tenant_id_hint: str | None = Field(default=None, max_length=128)
    memory_key_hint: str | None = Field(default=None, max_length=512)
    target_memory_id: str | None = Field(default=None, max_length=128)
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    sensitivity: str = Field(default="normal", min_length=1, max_length=64)
    evidence_refs: list[MemoryEvidenceRef] = Field(min_length=1, max_length=20)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_structured_value_size(self) -> "MemoryFormationCandidate":
        if not self.content.strip():
            raise ValueError("candidate content must not be blank")
        try:
            encoded = json.dumps(
                self.structured_value,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("candidate structured_value must be strict JSON") from exc
        if len(encoded) > 8192:
            raise ValueError("candidate structured_value exceeds 8192 bytes")
        return self


class MemoryLifecycleOperation(StrictBaseModel):
    operation_id: str = Field(default_factory=lambda: f"mfop_{uuid4().hex}")
    operation: MemoryOperation
    decision_status: MemoryDecisionStatus
    reason_code: MemoryFormationReasonCode
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    agent_id: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=128)
    metadata: JsonDict = Field(default_factory=dict, max_length=50)
    memory_key: str = Field(min_length=1)
    candidate_hash: str = Field(min_length=1)
    memory_id: str | None = None
    revision_id: str | None = None
    formation_job_id: str | None = None
    canonical_refs: list[str] = Field(default_factory=list)


class MemoryFormationTrace(StrictBaseModel):
    job: MemoryFormationJob
    turn_ids: list[str] = Field(default_factory=list)
    request_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    agent_ids: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    operations: list[MemoryLifecycleOperation] = Field(default_factory=list)
    candidate_count: int = Field(default=0, ge=0)
    decision_counts: dict[str, int] = Field(default_factory=dict)
    semantic_contract_version: str | None = Field(default=None, max_length=64)
    semantic_validation_counts: dict[str, int] = Field(default_factory=dict, max_length=20)
    semantic_verifier_counts: dict[str, int] = Field(default_factory=dict, max_length=20)
    model_latency_ms: int | None = Field(default=None, ge=0)
    provider_latency_ms: int | None = Field(default=None, ge=0)
    usage: JsonDict = Field(default_factory=dict)


class MemoryTraceLinks(StrictBaseModel):
    request_ids: list[str] = Field(default_factory=list, max_length=100)
    session_id: str | None = Field(default=None, max_length=128)
    turn_ids: list[str] = Field(default_factory=list, max_length=100)
    run_ids: list[str] = Field(default_factory=list, max_length=100)
    memory_ids: list[str] = Field(default_factory=list, max_length=100)
    consumer: str | None = Field(default=None, max_length=128)
    projection_outcome: str | None = Field(default=None, max_length=64)
    relevance: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)


class MemoryFormationJobView(StrictBaseModel):
    job_id: str
    trigger: MemoryFormationTrigger
    status: MemoryFormationJobStatus
    mode: MemoryFormationMode
    first_turn_id: str | None = None
    last_turn_id: str | None = None
    source_refs: list[str] = Field(default_factory=list, max_length=100)
    model_version: str
    prompt_version: str
    policy_version: str
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    last_error_code: str | None = Field(default=None, max_length=128)
    created_at: datetime
    updated_at: datetime


class MemoryFormationDecisionView(StrictBaseModel):
    decision_id: str | None = None
    operation_id: str
    operation: MemoryOperation
    proposed_operation: MemoryCandidateOperation | None = None
    decision_status: MemoryDecisionStatus
    reason_code: MemoryFormationReasonCode
    scope: str | None = None
    memory_key: str
    memory_id: str | None = None
    revision_id: str | None = None
    canonical_refs: list[str] = Field(default_factory=list, max_length=100)
    content_preview: str | None = Field(default=None, max_length=320)
    content_redacted: bool = False
    index_status: MemoryIndexStatus | None = None
    provider_status: str | None = Field(default=None, max_length=64)


class MemoryFormationTraceView(StrictBaseModel):
    job: MemoryFormationJobView
    links: MemoryTraceLinks
    scopes: list[str] = Field(default_factory=list, max_length=50)
    decisions: list[MemoryFormationDecisionView] = Field(default_factory=list, max_length=100)
    candidate_count: int = Field(default=0, ge=0)
    decision_counts: dict[str, int] = Field(default_factory=dict)
    semantic_contract_version: str | None = Field(default=None, max_length=64)
    semantic_validation_counts: dict[str, int] = Field(default_factory=dict, max_length=20)
    semantic_verifier_counts: dict[str, int] = Field(default_factory=dict, max_length=20)
    revision_ids: list[str] = Field(default_factory=list, max_length=100)
    model_latency_ms: int | None = Field(default=None, ge=0)
    provider_latency_ms: int | None = Field(default=None, ge=0)
    usage: JsonDict = Field(default_factory=dict)


class MemoryRevisionView(StrictBaseModel):
    revision_id: str
    memory_id: str
    revision_no: int = Field(ge=1)
    memory_key: str
    operation: MemoryRevisionOperation
    content_preview: str | None = Field(default=None, max_length=320)
    content_redacted: bool = False
    confidence: float = Field(ge=0, le=1)
    policy_version: str
    supersedes_revision_id: str | None = None
    formation_job_id: str | None = None
    created_at: datetime


class MemoryRevision(StrictBaseModel):
    revision_id: str = Field(default_factory=lambda: f"mrev_{uuid4().hex}")
    memory_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)
    memory_key: str = Field(min_length=1)
    operation: MemoryRevisionOperation
    content: str
    structured_value: JsonDict = Field(default_factory=dict)
    evidence_refs: list[MemoryEvidenceRef] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)
    policy_version: str = Field(min_length=1)
    supersedes_revision_id: str | None = None
    formation_job_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryIndexOperation(StrictBaseModel):
    index_operation_id: str = Field(default_factory=lambda: f"midxop_{uuid4().hex}")
    idempotency_key: str = Field(min_length=1)
    operation: MemoryIndexOperationType
    memory_id: str = Field(min_length=1)
    revision_id: str | None = None
    tenant_id: str = Field(min_length=1)
    external_memory_id: str | None = None
    status: MemoryIndexOperationStatus = MemoryIndexOperationStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=128)
    last_error_metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryItem(StrictBaseModel):
    memory_id: str = Field(default_factory=lambda: f"mem_{uuid4().hex}")
    scope: MemoryScope | str
    subject_type: str = "user"
    subject_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    content: str
    structured_value: JsonDict = Field(default_factory=dict)
    source: str = "manual"
    confidence: float = Field(default=1.0, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    visibility: MemoryVisibility = "user"
    ttl_expires_at: datetime | None = None
    metadata: JsonDict = Field(default_factory=dict)
    memory_key: str | None = None
    candidate_hash: str | None = None
    current_revision_id: str | None = None
    current_revision_no: int | None = Field(default=None, ge=1)
    formation_job_id: str | None = None
    lifecycle_status: MemoryLifecycleStatus = MemoryLifecycleStatus.ACTIVE
    index_status: MemoryIndexStatus | None = None
    canonical_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_context_item(self, *, relevance: float = 0.5) -> MemoryContextItem:
        return MemoryContextItem(
            memory_id=self.memory_id,
            scope=self.scope,
            content=self.content,
            relevance=relevance,
            confidence=self.confidence,
            importance=self.importance,
            source=self.source,
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            ttl_expires_at=self.ttl_expires_at,
            structured_value=self.structured_value,
            current_revision_id=self.current_revision_id,
            current_revision_no=self.current_revision_no,
            canonical_refs=self.canonical_refs,
            metadata=self.metadata,
        )


class MemoryRecallRequest(StrictBaseModel):
    query: str
    user: UserContext
    scopes: list[MemoryScope | str] = Field(default_factory=list)
    agent_id: str | None = None
    subject_type: str = "user"
    subject_id: str | None = None
    max_items: int = Field(default=5, ge=0, le=50)
    metadata_filters: JsonDict = Field(default_factory=dict)


class MemoryRecallResponse(StrictBaseModel):
    context: MemoryContext = Field(default_factory=MemoryContext)
    denied_count: int = 0
    expired_count: int = 0
    metadata: JsonDict = Field(default_factory=dict)


class MemoryWriteCandidate(StrictBaseModel):
    scope: MemoryScope | str
    content: str
    source: str = "auto"
    subject_type: str = "user"
    subject_id: str | None = None
    agent_id: str | None = None
    confidence: float = Field(default=0.8, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    structured_value: JsonDict = Field(default_factory=dict)
    metadata: JsonDict = Field(default_factory=dict)


class MemoryWriteDecision(StrictBaseModel):
    candidate: MemoryWriteCandidate
    status: MemoryWriteStatus
    reason: str = ""
    memory_id: str | None = None
    memory_key: str | None = None
    candidate_hash: str | None = None
    current_revision_id: str | None = None
    formation_job_id: str | None = None
    lifecycle_status: MemoryLifecycleStatus | None = None
    index_status: MemoryIndexStatus | None = None
    canonical_refs: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


class MemoryEvent(StrictBaseModel):
    event_id: str = Field(default_factory=lambda: f"mevt_{uuid4().hex}")
    event_type: str
    memory_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    formation_job_id: str | None = None
    memory_key: str | None = None
    decision_status: str | None = None
    decision_id: str | None = None
    scope: str | None = None
    payload: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryCleanupResult(StrictBaseModel):
    expired_memory_ids: list[str] = Field(default_factory=list)
    expired_count: int = 0


class MemoryRequestTraceView(StrictBaseModel):
    request_id: str
    overall_stage: str = Field(min_length=1, max_length=64)
    terminal: bool = False
    retryable: bool = False
    reason_code: str | None = Field(default=None, max_length=128)
    turn_id: str | None = None
    turn_status: str | None = Field(default=None, max_length=32)
    run_ids: list[str] = Field(default_factory=list, max_length=100)
    result_ids: list[str] = Field(default_factory=list, max_length=100)
    outbox_ids: list[str] = Field(default_factory=list, max_length=100)
    formation_turn_ids: list[str] = Field(default_factory=list, max_length=100)
    formation_job_ids: list[str] = Field(default_factory=list, max_length=100)
    memory_ids: list[str] = Field(default_factory=list, max_length=100)
    revision_ids: list[str] = Field(default_factory=list, max_length=100)
    index_operation_ids: list[str] = Field(default_factory=list, max_length=100)
    updated_at: datetime | None = None


class MemoryDebugResponse(StrictBaseModel):
    items: list[MemoryItem] = Field(default_factory=list)
    revisions: list[MemoryRevisionView] = Field(default_factory=list)
    events: list[MemoryEvent] = Field(default_factory=list)
    formation_traces: list[MemoryFormationTraceView] = Field(default_factory=list)
    context_trace_links: list[MemoryTraceLinks] = Field(default_factory=list)
    request_trace: MemoryRequestTraceView | None = None
    metadata: JsonDict = Field(default_factory=dict)


class UserMemoryListItem(StrictBaseModel):
    content: str
    memory_type: Literal["user_preference", "stable_fact"]
    availability: Literal["available", "preparing", "unavailable"]
    updated_at: datetime
    target_token: str = Field(min_length=1, max_length=128)
    concurrency_token: str | None = Field(default=None, max_length=128)


class UserMemoryListResponse(StrictBaseModel):
    items: list[UserMemoryListItem] = Field(default_factory=list)
    page: int = Field(ge=1)
    page_size: Literal[20] = 20
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)


class UserMemoryDeleteRequest(StrictBaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    concurrency_token: str = Field(min_length=1, max_length=128)


class UserMemoryDeleteResponse(StrictBaseModel):
    accepted: Literal[True] = True
    idempotent_replay: bool = False


class MemoryGovernanceItem(StrictBaseModel):
    memory_id: str
    anomaly: Literal[
        "deletion_timeout",
        "deletion_dead_letter",
        "provider_residual",
        "canonical_not_closed",
    ]
    status: Literal["needs_attention", "blocked", "repairing"]
    version: str = Field(min_length=1, max_length=128)
    content: str | None = None
    content_state: Literal["present", "cleared"]
    safe_reason: str = Field(max_length=128)
    deletion_pending_seconds: float = Field(ge=0)
    updated_at: datetime


class MemoryGovernanceResponse(StrictBaseModel):
    items: list[MemoryGovernanceItem] = Field(default_factory=list)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    total: int = Field(default=0, ge=0)
    healthy: bool = True


class MemoryGovernanceRepairRequest(StrictBaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    expected_version: str = Field(min_length=1, max_length=128)
    expected_anomaly: Literal[
        "deletion_timeout",
        "deletion_dead_letter",
        "provider_residual",
        "canonical_not_closed",
    ]


class MemoryGovernanceRepairResponse(StrictBaseModel):
    memory_id: str
    accepted: bool
    status: Literal["repairing", "rejected"]
    action: Literal["cleanup_advance", "canonical_close"] | None = None
    reason: str
    idempotent_replay: bool = False


class MemoryDeleteRequest(StrictBaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=500)
    expected_revision_id: str | None = Field(default=None, max_length=128)


class MemoryPendingDecisionActionRequest(StrictBaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=500)
    expected_revision_id: str | None = Field(default=None, max_length=128)


class MemoryManagementOperationResponse(StrictBaseModel):
    operation_id: str
    operation: str
    status: str
    memory_id: str | None = None
    decision_id: str | None = None
    index_operation_id: str | None = None
    provider_status: str | None = None
    idempotent_replay: bool = False
    observation_status: Literal["complete", "incomplete"] = "complete"
    incomplete_reason_codes: list[str] = Field(default_factory=list, max_length=20)


class MemoryPendingDecisionEvidence(StrictBaseModel):
    decision_id: str = Field(min_length=1, max_length=128)
    previous_value: str | None = Field(default=None, max_length=300)
    proposed_value: str | None = Field(default=None, max_length=300)


class MemoryAdminActionRequest(StrictBaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    actor: str | None = Field(default=None, min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=256)
    expected_revision_id: str | None = Field(default=None, max_length=128)


class MemoryRuntimeHealth(StrictBaseModel):
    worker_state: str
    pending_turn_count: int = Field(default=0, ge=0)
    outbox_pending_count: int = Field(default=0, ge=0)
    outbox_oldest_pending_seconds: float | None = Field(default=None, ge=0)
    trace_missing_count: int = Field(default=0, ge=0)
    queue_depth: int = Field(default=0, ge=0)
    oldest_pending_seconds: float | None = Field(default=None, ge=0)
    dead_letter_count: int = Field(default=0, ge=0)
    index_out_of_sync_count: int = Field(default=0, ge=0)
    index_dead_letter_count: int = Field(default=0, ge=0)
    deletion_pending_count: int = Field(default=0, ge=0)
    last_safe_error: str | None = Field(default=None, max_length=128)


class MemoryFormationMetricSeries(StrictBaseModel):
    job_count: int = Field(default=0, ge=0)
    queue_latency_ms_total: int = Field(default=0, ge=0)
    model_latency_ms_total: int = Field(default=0, ge=0)
    model_usage_total: dict[str, int | float] = Field(default_factory=dict)
    decisions_by_operation_status: dict[str, int] = Field(default_factory=dict)
    decision_rates: dict[str, float] = Field(default_factory=dict)


class MemoryMetricsResponse(StrictBaseModel):
    aggregation_mode: Literal["bounded_snapshot", "full_database"] = "bounded_snapshot"
    snapshot_limit: int | None = Field(default=10000, ge=1)
    jobs_by_trigger_status: dict[str, int] = Field(default_factory=dict)
    jobs_by_trigger_scope_status: dict[str, int] = Field(default_factory=dict)
    formation_series: dict[str, MemoryFormationMetricSeries] = Field(default_factory=dict)
    decisions_by_operation_status: dict[str, int] = Field(default_factory=dict)
    decision_rates: dict[str, float] = Field(default_factory=dict)
    retries: int = Field(default=0, ge=0)
    dead_letters: int = Field(default=0, ge=0)
    pending_turn_count: int = Field(default=0, ge=0)
    outbox_pending_count: int = Field(default=0, ge=0)
    outbox_oldest_pending_seconds: float | None = Field(default=None, ge=0)
    trace_missing_count: int = Field(default=0, ge=0)
    queue_latency_ms_total: int = Field(default=0, ge=0)
    job_latency_ms_total: int = Field(default=0, ge=0)
    model_latency_ms_total: int = Field(default=0, ge=0)
    model_usage_total: dict[str, int | float] = Field(default_factory=dict)
    index_operations_by_status: dict[str, int] = Field(default_factory=dict)
    index_out_of_sync_count: int = Field(default=0, ge=0)
    index_operation_latency_ms_total: int = Field(default=0, ge=0)
    index_repairs: int = Field(default=0, ge=0)
    index_repaired_records: int = Field(default=0, ge=0)
    index_orphan_records: int = Field(default=0, ge=0)
    index_repair_latency_ms_total: int = Field(default=0, ge=0)
    deletion_completions: int = Field(default=0, ge=0)
    deletion_failures: int = Field(default=0, ge=0)
    deletion_dead_letters: int = Field(default=0, ge=0)
    deletion_oldest_pending_seconds: float | None = Field(default=None, ge=0)
    recall_used: int = Field(default=0, ge=0)
    oldest_pending_seconds: float | None = Field(default=None, ge=0)


def default_expiration_for_scope(
    scope: str,
    *,
    task_days: int,
    session_summary_days: int,
    artifact_reference_days: int,
    now: datetime | None = None,
) -> datetime | None:
    now = now or datetime.now(UTC)
    if scope == "task_memory":
        return now + timedelta(days=task_days)
    if scope == "session_summary":
        return now + timedelta(days=session_summary_days)
    if scope == "artifact_reference":
        return now + timedelta(days=artifact_reference_days)
    return None
