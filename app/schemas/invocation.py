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
    context: JsonDict = Field(default_factory=dict)
    memory_context: MemoryContext = Field(default_factory=MemoryContext)
    knowledge_context: KnowledgeContext = Field(default_factory=KnowledgeContext)
    knowledge_context_handle: str | None = None
    knowledge_context_trace_id: str | None = None


class AgentInvocationResult(StrictBaseModel):
    run_id: str
    agent_id: str
    status: AgentRunStatus
    message: str = ""
    output: JsonDict | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    usage: JsonDict = Field(default_factory=dict)
    error: ErrorDetail | None = None


class InvokeRequest(StrictBaseModel):
    request_id: str | None = None
    session_id: str
    agent_id: str
    user: UserContext
    input: JsonDict = Field(default_factory=dict)
    context: JsonDict = Field(default_factory=dict)
    memory_context: MemoryContext | None = None
    knowledge_context: KnowledgeContext | None = None
    knowledge_context_handle: str | None = None
    knowledge_context_trace_id: str | None = None


class RouteAndInvokeResponse(StrictBaseModel):
    route: JsonDict
    result: AgentInvocationResult | None = None
