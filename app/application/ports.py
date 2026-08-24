from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.schemas.agents import AgentDefinitionV2
from app.schemas.common import UserContext
from app.schemas.delegated_runs import (
    DelegatedRunCancelCommand,
    DelegatedRunCommandResult,
    DelegatedRunCompleteCommand,
    DelegatedRunExisting,
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
from app.schemas.external_execution import (
    ExternalExecutionAcceptanceReservation,
    ExternalExecutionStartResult,
    ExternalExecutorAcceptance,
    ExternalExecutorAcceptanceRequest,
)
from app.schemas.invocation import AgentInvocationResult, InvokeRequest
from app.schemas.memory import (
    MemoryGovernanceRepairResponse,
    MemoryGovernanceResponse,
    MemoryManagementOperationResponse,
    MemoryPendingDecisionEvidence,
    UserMemoryDeleteResponse,
    UserMemoryListResponse,
)
from app.schemas.plans import Plan, PlanActionResponse
from app.schemas.registry_mutation import RegistryMutationCommand, RegistryMutationResult
from app.schemas.routing import RouteRequest, RouteResponse
from app.schemas.turns import CanonicalTurn, TurnUserInput
from app.services.registry_service import RegistryState


@dataclass(frozen=True, slots=True)
class RegistrySnapshotQuarantineInput:
    """A safe source-row diagnostic that Core retains only as quarantine metadata.

    Source adapters use this instead of forwarding a malformed legacy row into
    the v2 compiler.  It deliberately carries only a repair locator and one
    bounded reason code; the Snapshot builder applies the final redaction.
    """

    agent_id: str | None
    reason_code: str


@runtime_checkable
class RegistrySnapshotSourceState(Protocol):
    """Minimal trusted Registry state visible to a Host source mapper."""

    agents: Sequence[object]


@dataclass(frozen=True, slots=True)
class RegistrySnapshotSourceInput:
    """A Host mapper's safe candidate input for Core-owned Snapshot replacement."""

    source: str
    definitions: Sequence[object]


RegistrySnapshotSourceMapper = Callable[
    [RegistrySnapshotSourceState],
    RegistrySnapshotSourceInput | Awaitable[RegistrySnapshotSourceInput],
]


@dataclass(frozen=True, slots=True)
class ConnectorResolutionRequest:
    """Trusted facts that scope one deployment-owned Connector lookup.

    ``principal`` is deliberately request-local and not serializable.  The
    Core builds this value from the already-bound Native Principal; a Connector
    implementation must not recover any of these facts from adapter input or
    from caller-provided configuration.
    """

    tenant_id: str
    principal: UserContext = field(repr=False)
    adapter_key: str
    connector_ref: str
    # The Invocation Runtime establishes this absolute fact before Connector
    # resolution begins. Deployments may shorten their own work to its
    # remaining budget but must never start a fresh full timeout.
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResolvedConnector:
    """One request-scoped Connector capability delivered only to an Adapter.

    ``connection`` may contain endpoint, credentials, a short-lived client, or
    another deployment-private value.  It is excluded from ``repr`` and
    equality so Core persistence, trace, and diagnostic code can retain only
    the safe identity fields above it.
    """

    tenant_id: str
    adapter_key: str
    connector_ref: str
    revision: str
    connection: object = field(repr=False, compare=False)


@runtime_checkable
class ConnectorResolverApplicationPort(Protocol):
    """Resolve and release one Connector without becoming a plugin catalog.

    A deployment supplies one narrow implementation.  It receives only the
    immutable Binding-selected adapter/ref and the trusted request Principal;
    it never chooses Handling or dispatches an Adapter itself.
    """

    async def resolve(
        self,
        request: ConnectorResolutionRequest,
    ) -> ResolvedConnector | None: ...

    async def release(self, connector: ResolvedConnector) -> None: ...


@runtime_checkable
class RoutingApplicationPort(Protocol):
    async def route(self, request: RouteRequest) -> RouteResponse: ...


@runtime_checkable
class InvocationApplicationPort(Protocol):
    """Execute a Core-governed Invocation through the shared Runtime.

    Direct calls still select against the current trusted Snapshot. Routed
    calls retain the private Route capability so Runtime consumes the exact
    Candidate Set and Binding selected by Router. Hosts never supply an Adapter
    key, Connector, or replacement Handling.
    """

    async def invoke(self, request: InvokeRequest) -> AgentInvocationResult: ...

    async def invoke_from_route(
        self,
        route_request: RouteRequest,
        route_response: RouteResponse,
    ) -> AgentInvocationResult | None: ...


@runtime_checkable
class SnapshotRoutingApplicationPort(Protocol):
    """Route one request through a freshly compiled trusted v2 Definition Snapshot."""

    async def route_with_snapshot(
        self,
        request: RouteRequest,
        *,
        definitions: Sequence[object],
        source: str,
    ) -> RouteResponse: ...

    async def preflight_plan_with_snapshot(
        self,
        plan: Plan,
        *,
        user: UserContext,
        definitions: Sequence[object],
        source: str,
    ) -> None: ...


@runtime_checkable
class PlanPreflightApplicationPort(Protocol):
    """Validate a delayed Plan against a fresh trusted Candidate Set without mutation."""

    async def preflight_plan(self, plan: Plan, *, user: UserContext) -> None: ...


@runtime_checkable
class RegistrySnapshotRefreshApplicationPort(Protocol):
    """Refresh the process-owned compiled Registry view after a committed write."""

    async def refresh_registry_snapshot(self, registry: "RegistryApplicationPort") -> bool: ...


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
    async def find_existing(
        self,
        command: DelegatedRunStartCommand,
    ) -> DelegatedRunExisting | None: ...

    async def start(self, command: DelegatedRunStartCommand) -> DelegatedRunCommandResult: ...

    async def progress(self, command: DelegatedRunProgressCommand) -> DelegatedRunCommandResult: ...

    async def complete(self, command: DelegatedRunCompleteCommand) -> DelegatedRunCommandResult: ...

    async def fail(self, command: DelegatedRunFailCommand) -> DelegatedRunCommandResult: ...

    async def cancel(self, command: DelegatedRunCancelCommand) -> DelegatedRunCommandResult: ...

    async def timeout(self, command: DelegatedRunTimeoutCommand) -> DelegatedRunCommandResult: ...


@runtime_checkable
class ExternalExecutorApplicationPort(Protocol):
    """A Host-owned capability that accepts one declared executor reference."""

    def supports(self, executor_ref: str) -> bool: ...

    async def accept(
        self,
        request: ExternalExecutorAcceptanceRequest,
    ) -> ExternalExecutorAcceptance: ...


@runtime_checkable
class ExternalExecutionAcceptanceApplicationPort(Protocol):
    """Durably record one Host acceptance without persisting Host-private binding data."""

    async def record_accepted(
        self, reservation: ExternalExecutionAcceptanceReservation
    ) -> bool: ...


@runtime_checkable
class ExternalExecutionApplicationPort(Protocol):
    """Start one already-routed External Execution without letting a Host rewrite Handling."""

    async def start_from_route(
        self,
        request: RouteRequest,
        response: RouteResponse,
        *,
        turn_id: str,
        deadline_at: datetime,
    ) -> ExternalExecutionStartResult: ...


@runtime_checkable
class MemoryGovernanceApplicationPort(Protocol):
    async def query(
        self,
        *,
        tenant_id: str,
        memory_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> MemoryGovernanceResponse: ...

    async def repair(
        self,
        *,
        tenant_id: str,
        memory_id: str,
        expected_version: str,
        expected_anomaly: str,
        idempotency_key: str,
    ) -> MemoryGovernanceRepairResponse: ...


@runtime_checkable
class RegistryApplicationPort(Protocol):
    async def load(self) -> RegistryState: ...

    async def list_definitions(self, *, enabled_only: bool = False) -> list[AgentDefinitionV2]: ...

    async def get_definition(self, agent_id: str) -> AgentDefinitionV2 | None: ...

    async def available_definitions(self, user: UserContext) -> list[AgentDefinitionV2]: ...

    async def upsert_definition(
        self, definition: AgentDefinitionV2, *, expected_revision: int | None = None
    ) -> AgentDefinitionV2: ...

    async def set_enabled(
        self, agent_id: str, enabled: bool, *, expected_revision: int | None = None
    ) -> AgentDefinitionV2 | None: ...

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

    async def delete_user_memory(
        self,
        *,
        target_token: str,
        concurrency_token: str,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> UserMemoryDeleteResponse: ...

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
