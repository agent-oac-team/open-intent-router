from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import JsonDict, StrictBaseModel


class RegistryAuditRecord(StrictBaseModel):
    revision_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    operation: Literal["create", "update", "enable", "disable", "delete"]
    operator_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    before: JsonDict | None = None
    after: JsonDict | None = None
    created_at: datetime
