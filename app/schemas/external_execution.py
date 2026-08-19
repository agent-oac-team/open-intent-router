"""Host-neutral values used to accept External Execution bindings."""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import JsonDict, StrictBaseModel
from app.schemas.delegated_runs import DelegatedRunReference
from app.schemas.logs import ExternalExecutionBindingSnapshot

_EXECUTOR_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_-]{0,127}$"
ExternalExecutorRejectionCode = Literal[
    "external_executor_unsupported",
    "external_executor_unauthorized",
    "external_executor_unhealthy",
]


class ExternalExecutionPrincipal(StrictBaseModel):
    """Minimal trusted Principal projection supplied to a Host Executor."""

    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    roles: list[str] = Field(default_factory=list, max_length=50)
    groups: list[str] = Field(default_factory=list, max_length=50)
    entitlements: list[str] = Field(default_factory=list, max_length=200)


class ExternalExecutorAcceptanceRequest(StrictBaseModel):
    """The bounded data a Host needs to accept one declared executor reference.

    ``acceptance_id`` is stable for one canonical Route/Turn attempt. Hosts must
    return the same acceptance for retries of that identifier.
    """

    acceptance_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_EXECUTOR_IDENTIFIER_PATTERN,
    )
    executor_ref: str = Field(min_length=1, max_length=128, pattern=_EXECUTOR_IDENTIFIER_PATTERN)
    agent_id: str = Field(min_length=1, max_length=128)
    agent_revision: int = Field(ge=0)
    principal: ExternalExecutionPrincipal
    params: JsonDict = Field(default_factory=dict)


class ExternalExecutionAcceptanceReservation(StrictBaseModel):
    """Safe durable identity for an accepted Host External Execution request.

    This deliberately stores neither the Host binding token nor Principal claims.
    A Host uses it only to make its deterministic acceptance boundary idempotent
    across workers and process restarts.
    """

    acceptance_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_EXECUTOR_IDENTIFIER_PATTERN,
    )
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    executor_ref: str = Field(min_length=1, max_length=128, pattern=_EXECUTOR_IDENTIFIER_PATTERN)


class ExternalExecutorAcceptance(StrictBaseModel):
    """A Host's safe acceptance or rejection of a declared External Executor."""

    accepted: bool
    binding_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_EXECUTOR_IDENTIFIER_PATTERN,
    )
    reason_code: ExternalExecutorRejectionCode | None = None

    @model_validator(mode="after")
    def require_a_complete_outcome(self) -> "ExternalExecutorAcceptance":
        if self.accepted:
            if self.binding_id is None or self.reason_code is not None:
                raise ValueError("accepted External Executor responses require only binding_id")
        elif self.binding_id is not None or self.reason_code is None:
            raise ValueError("rejected External Executor responses require only reason_code")
        return self


class ExternalExecutionStartResult(StrictBaseModel):
    """The accepted canonical Run and opaque Ticket returned to a Host Adapter."""

    run: DelegatedRunReference
    execution_ticket: str = Field(min_length=32, max_length=512)
    binding_snapshot: ExternalExecutionBindingSnapshot
    binding_trace_facts: JsonDict = Field(default_factory=dict)
