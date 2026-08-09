from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import StrictBaseModel


class MemoryDecisionActionRequest(StrictBaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=500)
    expected_revision_id: str | None = Field(default=None, max_length=128)


class UiHandoffEventRequest(StrictBaseModel):
    handoff_id: str = Field(min_length=1, max_length=128)
    status: Literal["requested", "completed", "failed"]
    from_path: str = Field(min_length=1, max_length=512)
    target_route: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=128)
    failure_code: str | None = Field(default=None, max_length=128)
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_route_and_failure(self) -> "UiHandoffEventRequest":
        if not self.from_path.startswith("/") or not self.target_route.startswith("/"):
            raise ValueError("ui handoff routes must be absolute paths")
        if self.status == "failed" and not self.failure_code:
            raise ValueError("failed ui handoff requires failure_code")
        if self.status != "failed" and self.failure_code is not None:
            raise ValueError("only failed ui handoff may include failure_code")
        return self


class PageWorkflowEventRequest(StrictBaseModel):
    event_id: str = Field(min_length=1, max_length=120)
    run_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    capability: str = Field(min_length=1, max_length=128)
    status: Literal["started", "stage", "completed", "failed"]
    stage_name: str | None = Field(default=None, max_length=128)
    result_summary: str | None = Field(default=None, max_length=512)
    error_code: str | None = Field(default=None, max_length=128)
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_lifecycle_details(self) -> "PageWorkflowEventRequest":
        if self.status == "stage" and not self.stage_name:
            raise ValueError("page workflow stage requires stage_name")
        if self.status == "completed" and not self.result_summary:
            raise ValueError("completed page workflow requires result_summary")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed page workflow requires error_code")
        if self.status != "stage" and self.stage_name is not None:
            raise ValueError("only page workflow stage may include stage_name")
        if self.status != "completed" and self.result_summary is not None:
            raise ValueError("only completed page workflow may include result_summary")
        if self.status != "failed" and self.error_code is not None:
            raise ValueError("only failed page workflow may include error_code")
        return self


class RuntimeObservationAcceptedResponse(StrictBaseModel):
    accepted: bool
    observation_status: Literal["complete", "incomplete"] = "complete"
    incomplete_reason_codes: list[str] = Field(default_factory=list, max_length=20)
