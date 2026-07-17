from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.schemas.common import StrictBaseModel


class ExecutionTicketStatus(StrEnum):
    ISSUED = "issued"
    CLAIMED = "claimed"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ExecutionTicketClaims(StrictBaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    plan_id: str | None = Field(default=None, max_length=128)
    step_id: str | None = Field(default=None, max_length=128)
    purpose: str = Field(min_length=1, max_length=64)
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=256)


class ExecutionTicketRecord(StrictBaseModel):
    ticket_hash: str = Field(min_length=64, max_length=64)
    claims: ExecutionTicketClaims
    status: ExecutionTicketStatus = ExecutionTicketStatus.ISSUED
    run_state_version: int = Field(default=1, ge=1)
    event_sequence: int = Field(default=0, ge=0)
    lease_owner: str | None = Field(default=None, max_length=128)
    lease_token: str | None = Field(default=None, max_length=128)
    lease_expires_at: datetime | None = None
    consumed_event_id: str | None = Field(default=None, max_length=128)
    consumed_at: datetime | None = None


class ExecutionTicketIssueResult(StrictBaseModel):
    ticket: str = Field(min_length=32, max_length=512)
    claims: ExecutionTicketClaims


class ExecutionTicketClaimResult(StrictBaseModel):
    record: ExecutionTicketRecord
    duplicate: bool = False


class LegacyExecutionCorrelationQuery(StrictBaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    plan_id: str | None = Field(default=None, max_length=128)
    step_id: str | None = Field(default=None, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=64)
    now: datetime


class LegacyExecutionCorrelationResult(StrictBaseModel):
    record: ExecutionTicketRecord
    correlation_mode: str = "legacy_unique"
