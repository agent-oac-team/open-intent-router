from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.agent_context import KnowledgeContext, MemoryContext
from app.schemas.common import (
    AgentRunStatus,
    ArtifactRef,
    ErrorDetail,
    JsonDict,
    StrictBaseModel,
    UserContext,
)


class AgentInvocation(StrictBaseModel):
    run_id: str
    request_id: str | None = None
    session_id: str
    agent_id: str
    user: UserContext
    input: JsonDict = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    context: JsonDict = Field(default_factory=dict)
    memory_context: MemoryContext = Field(default_factory=MemoryContext)
    knowledge_context: KnowledgeContext = Field(default_factory=KnowledgeContext)
    knowledge_context_handle: str | None = None
    knowledge_context_trace_id: str | None = None
    deadline_at: datetime | None = None


class AgentInvocationResult(StrictBaseModel):
    run_id: str
    agent_id: str
    status: AgentRunStatus
    message: str = ""
    output: JsonDict | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    usage: JsonDict = Field(default_factory=dict)
    error: ErrorDetail | None = None


InvocationControlState = Literal[
    "cancelled",
    "stop_confirmed",
    "stop_unconfirmed",
    "unsupported",
    "terminal",
]


class InvocationCancelRequest(StrictBaseModel):
    """An intentionally empty, principal-bound request to stop one accepted Run."""


class InvocationCancelResponse(StrictBaseModel):
    """Truthful, bounded control facts for one owned Invocation Run.

    ``control_state`` is deliberately separate from ``run_status``. A Runtime
    may know that an Adapter confirmed stop while the short canonical terminal
    transaction is still settling; it must not report the Run as cancelled
    until that transition is durable.
    """

    run_id: str
    run_status: str
    control_state: InvocationControlState
    completion_certainty: Literal["certain", "unknown"] | None = None
    reason_code: str | None = Field(default=None, max_length=64)


class InvokeRequest(StrictBaseModel):
    request_id: str | None = None
    session_id: str
    agent_id: str
    user: UserContext
    input: JsonDict = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    context: JsonDict = Field(default_factory=dict)
    memory_context: MemoryContext | None = None
    knowledge_context: KnowledgeContext | None = None
    knowledge_context_handle: str | None = None
    knowledge_context_trace_id: str | None = None


class RouteAndInvokeResponse(StrictBaseModel):
    route: JsonDict
    result: AgentInvocationResult | None = None
