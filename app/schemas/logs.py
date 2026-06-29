from datetime import datetime

from pydantic import Field

from app.schemas.common import JsonDict, StrictBaseModel


class AgentRun(StrictBaseModel):
    run_id: str
    request_id: str | None = None
    session_id: str
    agent_id: str
    status: str
    invoker_type: str
    input: JsonDict = Field(default_factory=dict)
    output: JsonDict | None = None
    error: JsonDict | None = None
    latency_ms: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentResult(StrictBaseModel):
    result_id: str
    run_id: str
    session_id: str
    agent_id: str
    plan_id: str | None = None
    step_id: str | None = None
    status: str
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
