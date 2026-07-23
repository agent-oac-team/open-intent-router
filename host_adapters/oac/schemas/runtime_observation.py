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


class RuntimeObservationAcceptedResponse(StrictBaseModel):
    accepted: bool
    observation_status: Literal["complete", "incomplete"] = "complete"
    incomplete_reason_codes: list[str] = Field(default_factory=list, max_length=20)
