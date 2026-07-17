from datetime import datetime

from pydantic import Field

from app.schemas.common import JsonDict, StrictBaseModel


class AgentRun(StrictBaseModel):
    run_id: str
    request_id: str | None = None
    session_id: str
    agent_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    turn_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    status: str
    invoker_type: str
    delegated: bool = False
    delegation_key: str | None = None
    state_version: int = Field(default=1, ge=1)
    event_sequence: int = Field(default=0, ge=0)
    deadline_at: datetime | None = None
    heartbeat_at: datetime | None = None
    claim_owner: str | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    terminal_event_id: str | None = None
    input: JsonDict = Field(default_factory=dict)
    output: JsonDict | None = None
    error: JsonDict | None = None
    latency_ms: int | None = None
    formation_suppressed: bool = False
    used_memory_ids: list[str] = Field(default_factory=list, max_length=50)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentResult(StrictBaseModel):
    result_id: str
    run_id: str
    session_id: str
    agent_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    turn_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    status: str
    run_state_version: int | None = Field(default=None, ge=1)
    message: str = ""
    formation_suppressed: bool = False
    formation_skip_audit_required: bool = False
    output: JsonDict | None = None
    artifact_refs: list[JsonDict] = Field(default_factory=list)
    error: JsonDict | None = None
    created_at: datetime | None = None


class RouteLog(StrictBaseModel):
    request_id: str
    session_id: str
    model_name: str
    candidate_agent_ids: list[str] = Field(default_factory=list)
    prompt_summary: str = ""
    evidence: list[JsonDict] = Field(default_factory=list)
    raw_output: JsonDict | None = None
    parsed_output: JsonDict | None = None
    validation_status: str = "ok"
    error: JsonDict | None = None
    latency_ms: int | None = None
    created_at: datetime | None = None
