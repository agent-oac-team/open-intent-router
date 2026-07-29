from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.schemas.agents import AgentDefinition
from app.schemas.delegated_runs import (
    DelegatedRunCancelCommand,
    DelegatedRunCommandResult,
    DelegatedRunCompleteCommand,
    DelegatedRunFailCommand,
    DelegatedRunProgressCommand,
    DelegatedRunStartCommand,
    DelegatedRunTimeoutCommand,
)
from app.schemas.events import AgentEvent, AgentEventResponse, ConversationEvent
from app.schemas.execution_traces import (
    ExecutionTraceEvent,
    ExecutionTraceEventDraft,
    ExecutionTraceQuery,
    ExecutionTraceSnapshot,
    ExecutionTraceWriteResult,
)
from app.schemas.knowledge import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.schemas.knowledge_assets import (
    CanonicalKnowledgeSearchRequest,
    CanonicalKnowledgeSearchResponse,
    ExactReadRequest,
    ExactReadResponse,
    GroupedKnowledgeSearchRequest,
    GroupedKnowledgeSearchResponse,
    KnowledgeAsset,
    KnowledgeAssetChunk,
    KnowledgeAssetGroup,
    KnowledgeImportJob,
    KnowledgeSourceRef,
)
from app.schemas.memory import (
    MemoryManagementOperationResponse,
    MemoryPendingDecisionEvidence,
    UserMemoryListResponse,
)
from app.schemas.plans import Plan, PlanActionResponse
from app.schemas.registry_mutation import RegistryMutationCommand, RegistryMutationResult
from app.schemas.routing import RouteRequest, RouteResponse
from app.schemas.turns import CanonicalTurn, TurnUserInput
from app.services.registry_service import RegistryState


@runtime_checkable
class RoutingApplicationPort(Protocol):
    async def route(self, request: RouteRequest) -> RouteResponse: ...


@runtime_checkable
class TurnApplicationPort(Protocol):
    async def start_turn(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        request_id: str,
        source: str,
        user_input: TurnUserInput,
    ): ...

    async def get_turn(
        self,
        *,
        turn_id: str,
        tenant_id: str,
        user_id: str,
    ) -> CanonicalTurn | None: ...

    async def attach_activity(
        self,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
        run_id: str | None = None,
        plan_id: str | None = None,
        blocked: bool = False,
    ) -> CanonicalTurn: ...


@runtime_checkable
class DelegatedRunApplicationPort(Protocol):
    async def start(self, command: DelegatedRunStartCommand) -> DelegatedRunCommandResult: ...

    async def progress(self, command: DelegatedRunProgressCommand) -> DelegatedRunCommandResult: ...

    async def complete(self, command: DelegatedRunCompleteCommand) -> DelegatedRunCommandResult: ...

    async def fail(self, command: DelegatedRunFailCommand) -> DelegatedRunCommandResult: ...

    async def cancel(self, command: DelegatedRunCancelCommand) -> DelegatedRunCommandResult: ...

    async def timeout(self, command: DelegatedRunTimeoutCommand) -> DelegatedRunCommandResult: ...


@runtime_checkable
class KnowledgeApplicationPort(Protocol):
    async def search(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResponse: ...


@runtime_checkable
class KnowledgeAssetApplicationPort(Protocol):
    async def search(
        self, request: CanonicalKnowledgeSearchRequest
    ) -> CanonicalKnowledgeSearchResponse: ...

    async def grouped_search(
        self, request: GroupedKnowledgeSearchRequest
    ) -> GroupedKnowledgeSearchResponse: ...

    async def exact_read(self, request: ExactReadRequest) -> ExactReadResponse: ...

    async def save_group(self, group: KnowledgeAssetGroup) -> KnowledgeAssetGroup: ...

    async def get_asset(self, asset_id: str) -> KnowledgeAsset | None: ...

    async def list_assets(self, *, tenant_id: str) -> list[KnowledgeAsset]: ...

    async def get_chunks(
        self,
        *,
        tenant_id: str,
        asset_ids: list[str] | None = None,
        chunk_ids: list[str] | None = None,
    ) -> list[KnowledgeAssetChunk]: ...

    async def latest_job(self, asset_id: str) -> KnowledgeImportJob | None: ...

    async def ingest_bytes(self, **kwargs) -> tuple[KnowledgeAsset, KnowledgeImportJob]: ...

    async def retry_job(
        self, job_id: str, *, extracted_chunks: list[tuple[str, KnowledgeSourceRef]]
    ) -> tuple[KnowledgeAsset, KnowledgeImportJob]: ...

    async def soft_delete(
        self, asset_id: str, *, tenant_id: str, owner_id: str
    ) -> KnowledgeAsset: ...


@runtime_checkable
class RegistryApplicationPort(Protocol):
    async def load(self) -> RegistryState: ...

    async def list_definitions(self, *, enabled_only: bool = False) -> list[AgentDefinition]: ...

    async def get_definition(self, agent_id: str) -> AgentDefinition | None: ...

    async def upsert_definition(
        self, definition: AgentDefinition, *, expected_revision: int | None = None
    ) -> AgentDefinition: ...

    async def set_enabled(
        self, agent_id: str, enabled: bool, *, expected_revision: int | None = None
    ) -> AgentDefinition | None: ...

    async def delete_definition(
        self, agent_id: str, *, expected_revision: int | None = None
    ) -> bool: ...

    async def mutate_definition(
        self, command: RegistryMutationCommand
    ) -> RegistryMutationResult: ...


@runtime_checkable
class EventApplicationPort(Protocol):
    async def record_conversation_event(self, event: ConversationEvent) -> ConversationEvent: ...

    async def record_agent_event(self, event: AgentEvent) -> AgentEventResponse: ...


@runtime_checkable
class ExecutionTraceApplicationPort(Protocol):
    async def record(self, event: ExecutionTraceEventDraft) -> ExecutionTraceWriteResult: ...

    async def try_record(self, event: ExecutionTraceEventDraft) -> bool: ...

    async def snapshot(self, query: ExecutionTraceQuery) -> ExecutionTraceSnapshot: ...

    async def events_after(
        self, query: ExecutionTraceQuery, *, after_offset: int
    ) -> list[ExecutionTraceEvent]: ...

    def stream(
        self,
        query: ExecutionTraceQuery,
        *,
        after_offset: int,
        poll_interval_seconds: float = 0.25,
    ) -> AsyncIterator[ExecutionTraceEvent]: ...


@runtime_checkable
class MemoryManagementApplicationPort(Protocol):
    async def list_user_memories(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_type: str | None,
        page: int,
    ) -> UserMemoryListResponse: ...

    async def get_pending_decision_evidence(
        self,
        *,
        decision_id: str,
        tenant_id: str,
        user_id: str,
    ) -> MemoryPendingDecisionEvidence: ...

    async def resolve_pending(
        self,
        *,
        decision_id: str,
        action: str,
        tenant_id: str,
        user_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_revision_id: str | None,
        trace_session_id: str | None = None,
        trace_turn_id: str | None = None,
    ) -> MemoryManagementOperationResponse: ...


@runtime_checkable
class PlanApplicationPort(Protocol):
    async def get_plan(self, plan_id: str, *, tenant_id: str, user_id: str) -> Plan | None: ...

    async def confirm(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str | None = None,
        expected_state_version: int | None = None,
        publish: bool = True,
    ) -> PlanActionResponse: ...

    async def cancel(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
        publish: bool = True,
    ) -> PlanActionResponse: ...

    async def apply_agent_event(
        self,
        event: AgentEvent,
        *,
        tenant_id: str,
        user_id: str,
        publish: bool = True,
    ) -> Plan | None: ...
