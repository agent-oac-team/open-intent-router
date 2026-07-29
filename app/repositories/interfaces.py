from datetime import datetime
from typing import Protocol

from app.schemas.agents import AgentDefinition
from app.schemas.events import AgentEvent, ConversationEvent
from app.schemas.logs import AgentResult, AgentRun, RouteLog
from app.schemas.memory import (
    MemoryEvent,
    MemoryFormationJob,
    MemoryFormationTrace,
    MemoryFormationTrigger,
    MemoryFormationTurn,
    MemoryIndexOperation,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryRevision,
)
from app.schemas.plans import Plan
from app.schemas.registry_mutation import RegistryMutationCommand, RegistryMutationResult
from app.schemas.sessions import ChatMessage
from app.schemas.turns import CanonicalTurn, FormationEligibilitySnapshot


class TurnRepository(Protocol):
    async def create_idempotent(self, turn: CanonicalTurn) -> tuple[CanonicalTurn, bool]: ...

    async def get(self, turn_id: str, *, tenant_id: str, user_id: str) -> CanonicalTurn | None: ...

    async def get_by_request(
        self, *, tenant_id: str, user_id: str, request_id: str
    ) -> CanonicalTurn | None: ...

    async def find_by_request_id(self, request_id: str) -> CanonicalTurn | None: ...

    async def get_internal(self, turn_id: str) -> CanonicalTurn | None: ...

    async def update_if_version(
        self, turn: CanonicalTurn, *, expected_version: int
    ) -> CanonicalTurn | None: ...


class AgentDefinitionRepository(Protocol):
    async def list(self, *, enabled_only: bool = False) -> list[AgentDefinition]: ...

    async def get(self, agent_id: str) -> AgentDefinition | None: ...

    async def upsert(
        self, definition: AgentDefinition, *, expected_revision: int | None = None
    ) -> AgentDefinition: ...

    async def set_enabled(
        self, agent_id: str, enabled: bool, *, expected_revision: int | None = None
    ) -> AgentDefinition | None: ...

    async def delete(self, agent_id: str, *, expected_revision: int | None = None) -> bool: ...

    async def mutate(self, command: RegistryMutationCommand) -> RegistryMutationResult: ...


class MessageRepository(Protocol):
    async def add(self, message: ChatMessage) -> ChatMessage: ...

    async def list_by_session(
        self,
        session_id: str,
        *,
        tenant_id: str,
        user_id: str,
        source: str | None = None,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[ChatMessage]: ...


class EventRepository(Protocol):
    async def add_conversation_event(self, event: ConversationEvent) -> ConversationEvent: ...

    async def add_agent_event(self, event: AgentEvent) -> tuple[AgentEvent, bool]: ...

    async def get_event(
        self, event_id: str, *, tenant_id: str | None, user_id: str | None
    ) -> AgentEvent | None: ...

    async def list_recent_events(
        self, session_id: str, *, tenant_id: str, user_id: str, limit: int = 10
    ) -> list[AgentEvent]: ...


class RunRepository(Protocol):
    async def add_run(self, run: AgentRun) -> AgentRun: ...

    async def update_run(self, run: AgentRun) -> AgentRun: ...

    async def get_run(self, run_id: str) -> AgentRun | None: ...

    async def list_formation_pending(self, *, limit: int = 100) -> list[AgentRun]: ...

    async def mark_formation_published(self, run_id: str, *, source_order: int) -> None: ...

    async def get_formation_published_order(self, run_id: str) -> int: ...


class ResultRepository(Protocol):
    async def add_result(self, result: AgentResult) -> AgentResult: ...

    async def list_recent(
        self, session_id: str, *, tenant_id: str, user_id: str, limit: int = 5
    ) -> list[AgentResult]: ...

    async def list_formation_pending(self, *, limit: int = 100) -> list[AgentResult]: ...

    async def mark_formation_published(self, result_id: str) -> None: ...

    async def mark_turn_captured(self, result_id: str) -> None: ...


class CanonicalInvocationStore(Protocol):
    async def start_run(
        self, run: AgentRun
    ) -> tuple[AgentRun, CanonicalTurn, AgentResult | None]: ...

    async def complete_run(
        self,
        *,
        run: AgentRun,
        result: AgentResult,
        response_text: str,
        eligibility: FormationEligibilitySnapshot,
    ) -> tuple[AgentRun, AgentResult, CanonicalTurn]: ...


class PlanRepository(Protocol):
    async def save(self, plan: Plan, *, formation_suppressed: bool = False) -> Plan: ...

    async def save_if_version(
        self,
        plan: Plan,
        *,
        expected_version: int,
        formation_suppressed: bool = False,
    ) -> Plan | None: ...

    async def get(self, plan_id: str, *, tenant_id: str, user_id: str) -> Plan | None: ...

    async def get_active_by_session(
        self, session_id: str, *, tenant_id: str, user_id: str
    ) -> Plan | None: ...

    async def claim_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        tenant_id: str,
        user_id: str,
        claim_id: str,
        lease_expires_at: datetime,
        now: datetime,
        formation_suppressed: bool = False,
    ) -> Plan | None: ...

    async def save_claimed_step(
        self,
        plan: Plan,
        step_id: str,
        *,
        claim_id: str,
        formation_suppressed: bool = False,
    ) -> Plan | None: ...

    async def list_formation_pending(self, *, limit: int = 100) -> list[Plan]: ...

    async def mark_formation_published(self, plan_id: str, *, state_version: int) -> None: ...

    async def get_execution_claim_key(self, plan_id: str, *, claim_id: str) -> str | None: ...

    async def renew_step_claim(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
        claim_id: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> bool: ...


class RouteLogRepository(Protocol):
    async def add(self, log: RouteLog) -> RouteLog: ...


class MemoryFormationRepository(Protocol):
    async def append_turn(self, turn: MemoryFormationTurn) -> MemoryFormationTurn: ...

    async def append_turn_and_maybe_create_window_job(
        self,
        turn: MemoryFormationTurn,
        *,
        window_turns: int,
        mode: str,
        model_version: str,
        prompt_version: str,
        policy_version: str,
        max_attempts: int = 5,
    ) -> tuple[MemoryFormationTurn, MemoryFormationJob | None]: ...

    async def list_pending_turns(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> list[MemoryFormationTurn]: ...

    async def create_job_for_pending(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        trigger: MemoryFormationTrigger,
        mode: str,
        model_version: str,
        prompt_version: str,
        policy_version: str,
        max_turns: int | None = None,
        max_attempts: int = 5,
        idle_due_at: datetime | None = None,
    ) -> MemoryFormationJob | None: ...

    async def claim_job(
        self, *, owner: str, now: datetime, lease_seconds: float
    ) -> MemoryFormationJob | None: ...

    async def complete_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        trace_summary: dict | None = None,
    ) -> MemoryFormationJob: ...

    async def fail_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        next_attempt_at: datetime,
    ) -> MemoryFormationJob: ...

    async def successful_watermark(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> str | None: ...

    async def list_idle_sessions(
        self, *, now: datetime, limit: int = 100
    ) -> list[tuple[str, str, str]]: ...


class MemoryRevisionRepository(Protocol):
    async def list_for_memory(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        user_id: str,
        subject_type: str,
        subject_id: str,
        limit: int = 100,
    ) -> list[MemoryRevision]: ...

    async def append_revision_and_set_current(
        self,
        revision: MemoryRevision,
        *,
        tenant_id: str,
        user_id: str,
        subject_type: str,
        subject_id: str,
        expected_current_revision_id: str | None,
    ) -> tuple[MemoryRevision, MemoryItem]: ...


class MemoryLifecycleRepository(Protocol):
    async def get_current_by_key(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: str,
        user_id: str | None = None,
        scope: str,
        memory_key: str,
        include_expired: bool = False,
    ) -> MemoryItem | None: ...

    async def compare_and_set_current(
        self, item: MemoryItem, *, expected_revision_id: str | None
    ) -> bool: ...


class MemoryIndexOperationRepository(Protocol):
    async def add(self, operation: MemoryIndexOperation) -> MemoryIndexOperation: ...

    async def get(
        self, index_operation_id: str, *, tenant_id: str
    ) -> MemoryIndexOperation | None: ...

    async def list_for_memory(
        self, memory_id: str, *, tenant_id: str, limit: int = 100
    ) -> list[MemoryIndexOperation]: ...

    async def latest_deletes_for_memories(
        self, memory_ids: list[str], *, tenant_id: str
    ) -> dict[str, MemoryIndexOperation]: ...

    async def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_seconds: float,
        tenant_id: str | None = None,
    ) -> MemoryIndexOperation | None: ...

    async def complete(
        self,
        index_operation_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        external_memory_id: str | None = None,
        result_metadata: dict | None = None,
    ) -> MemoryIndexOperation: ...

    async def fail(
        self,
        index_operation_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        next_attempt_at: datetime,
    ) -> MemoryIndexOperation: ...

    async def list_repair_candidates(
        self, *, tenant_id: str, statuses: list[str], limit: int = 100
    ) -> list[MemoryIndexOperation]: ...


class MemoryTraceRepository(Protocol):
    async def list_traces(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        scope: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        formation_job_id: str | None = None,
        memory_id: str | None = None,
        memory_key: str | None = None,
        decision_status: str | None = None,
        limit: int = 50,
    ) -> list[MemoryFormationTrace]: ...


class MemoryLifecycleTransactionStore(Protocol):
    async def commit_add(
        self,
        *,
        item: MemoryItem,
        revision: MemoryRevision,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
    ) -> tuple[MemoryItem, MemoryRevision, MemoryEvent, MemoryIndexOperation, bool]: ...

    async def commit_update(
        self,
        *,
        operation: MemoryLifecycleOperation,
        revision: MemoryRevision,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
        candidate_hash: str,
        confidence: float,
        importance: float,
        canonical_refs: list[str],
    ) -> tuple[MemoryItem, MemoryRevision, MemoryEvent, MemoryIndexOperation, bool]: ...

    async def record_decision(self, event: MemoryEvent) -> tuple[MemoryEvent, bool]: ...

    async def request_delete(
        self,
        *,
        operation: MemoryLifecycleOperation,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
    ) -> tuple[MemoryItem | None, MemoryEvent, MemoryIndexOperation, bool]: ...

    async def complete_delete(
        self,
        *,
        operation: MemoryLifecycleOperation,
        tombstone: MemoryEvent,
        index_operation_id: str,
    ) -> tuple[MemoryEvent, bool]: ...

    async def record_delete_failure(
        self,
        *,
        operation: MemoryLifecycleOperation,
        event: MemoryEvent,
        dead_letter: bool,
    ) -> tuple[MemoryItem, MemoryEvent, bool]: ...

    async def list_expired(self, *, now: datetime, limit: int = 100) -> list[MemoryItem]: ...

    async def get_item(self, memory_id: str) -> MemoryItem | None: ...

    async def complete_delete_from_index(
        self,
        index_operation: MemoryIndexOperation,
        *,
        now: datetime,
        provider_status: str | None = None,
    ) -> tuple[MemoryEvent, bool]: ...

    async def record_delete_index_failure(
        self,
        index_operation: MemoryIndexOperation,
        *,
        error_code: str,
        dead_letter: bool,
        now: datetime,
    ) -> tuple[MemoryItem, MemoryEvent, bool]: ...
