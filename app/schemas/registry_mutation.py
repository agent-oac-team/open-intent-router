from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from app.schemas.agents import AgentDefinition
from app.schemas.registry_audit import RegistryAuditRecord

RegistryMutationOperation = Literal["create", "update", "enable", "disable", "delete"]


@dataclass(frozen=True)
class RegistryMutationCommand:
    operation: RegistryMutationOperation
    agent_id: str
    actor_id: str
    source: str
    expected_revision: int
    definition: AgentDefinition | None = None
    revision_id: str = field(default_factory=lambda: f"registry_revision_{uuid4().hex}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.agent_id or not self.actor_id or not self.source:
            raise ValueError("registry mutation identity fields are required")
        if self.expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if self.operation in {"create", "update"}:
            if self.definition is None or self.definition.agent_id != self.agent_id:
                raise ValueError("registry mutation definition must match agent_id")
        elif self.definition is not None:
            raise ValueError("definition is only valid for create or update")
        if self.operation == "create" and self.expected_revision != 0:
            raise ValueError("create requires expected_revision=0")


@dataclass(frozen=True)
class RegistryMutationResult:
    operation: RegistryMutationOperation
    before: AgentDefinition | None
    after: AgentDefinition | None
    audit: RegistryAuditRecord
