from datetime import UTC, datetime

from pydantic import Field, model_validator

from app.schemas.common import (
    ArtifactRef,
    ExecutionPolicy,
    JsonDict,
    NextActionType,
    PlanStatus,
    PlanStepStatus,
    StrictBaseModel,
    UserContext,
)


class PlanStep(StrictBaseModel):
    step_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: PlanStepStatus = "pending"
    depends_on: list[str] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)


class NextAction(StrictBaseModel):
    type: NextActionType = "none"
    message: str = ""
    agent_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    route: str | None = None
    params: JsonDict = Field(default_factory=dict)
    metadata: JsonDict = Field(default_factory=dict)


class Plan(StrictBaseModel):
    plan_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    session_id: str | None = None
    status: PlanStatus = "pending"
    current_step_id: str | None = None
    execution_policy: ExecutionPolicy | None = None
    next_action: NextAction | None = None
    last_event_id: str | None = None
    state_version: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    formation_event_type: str = Field(default="update", max_length=32)
    steps: list[PlanStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> "Plan":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step_id values must be unique")
        known = set(step_ids)
        graph = {step.step_id: set(step.depends_on) for step in self.steps}
        for step in self.steps:
            unknown = [item for item in step.depends_on if item not in known]
            if unknown:
                raise ValueError(f"step {step.step_id} depends on unknown steps: {unknown}")
        _validate_no_cycles(graph)
        if self.current_step_id and self.current_step_id not in known:
            raise ValueError("current_step_id must reference a step_id in steps")
        if not self.current_step_id and self.status in {"pending", "running", "blocked"}:
            self.current_step_id = self.steps[0].step_id
        if self.status in {"completed", "failed", "cancelled"}:
            self.current_step_id = None
        return self


class PlanActionRequest(StrictBaseModel):
    action: str = Field(pattern="^(confirm|cancel)$")
    user: UserContext


class PlanActionResponse(StrictBaseModel):
    plan_id: str
    status: PlanStatus
    current_step_id: str | None = None
    next_action: NextAction | None = None


class PlanExecutionRequest(StrictBaseModel):
    user: UserContext
    input: JsonDict = Field(default_factory=dict)
    context: JsonDict = Field(default_factory=dict)
    max_steps: int = Field(default=10, ge=1, le=50)


class PlanExecutionResponse(StrictBaseModel):
    plan: Plan
    results: list[JsonDict] = Field(default_factory=list)
    next_action: NextAction | None = None


def _validate_no_cycles(graph: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError("plan dependencies must not contain cycles")
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for step_id in graph:
        visit(step_id)
