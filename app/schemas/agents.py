from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from app.core.redaction import redact_value
from app.schemas.common import AgentType, JsonDict, SchemaContract, StrictBaseModel, UserContext


class TriggerSpec(StrictBaseModel):
    keywords: list[str] = Field(default_factory=list)
    positive_examples: list[str] = Field(default_factory=list)
    negative_examples: list[str] = Field(default_factory=list)


class AccessPolicy(StrictBaseModel):
    allow_roles: list[str] = Field(default_factory=list)
    allow_groups: list[str] = Field(default_factory=list)
    allow_tenants: list[str] = Field(default_factory=list)
    deny_roles: list[str] = Field(default_factory=list)
    deny_groups: list[str] = Field(default_factory=list)
    deny_tenants: list[str] = Field(default_factory=list)
    required_attributes: JsonDict = Field(default_factory=dict)

    def allows(self, user: UserContext) -> bool:
        tenant_id = user.tenant_id
        if set(user.roles) & set(self.deny_roles):
            return False
        if set(user.groups) & set(self.deny_groups):
            return False
        if tenant_id and tenant_id in self.deny_tenants:
            return False

        if self.allow_roles and not (set(user.roles) & set(self.allow_roles)):
            return False
        if self.allow_groups and not (set(user.groups) & set(self.allow_groups)):
            return False
        if self.allow_tenants and "*" not in self.allow_tenants:
            if not tenant_id or tenant_id not in self.allow_tenants:
                return False

        for key, expected in self.required_attributes.items():
            if user.attributes.get(key) != expected:
                return False
        return True


class InvocationSpec(StrictBaseModel):
    type: AgentType
    config: JsonDict = Field(default_factory=dict)
    provider_config: JsonDict = Field(default_factory=dict)


class UiHandoffSpec(StrictBaseModel):
    mode: str = "none"
    route: str | None = None
    params: JsonDict = Field(default_factory=dict)


class AgentDefinition(StrictBaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    version: str | None = None
    enabled: bool = True
    type: AgentType
    capabilities: list[str] = Field(default_factory=list)
    domain: str | None = None
    tags: list[str] = Field(default_factory=list)
    trigger: TriggerSpec = Field(default_factory=TriggerSpec)
    access_policy: AccessPolicy = Field(default_factory=AccessPolicy)
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    input_schema: SchemaContract = Field(default_factory=SchemaContract)
    output_schema: SchemaContract = Field(default_factory=SchemaContract)
    invocation: InvocationSpec
    ui_handoff: UiHandoffSpec = Field(default_factory=UiHandoffSpec)
    priority: int = 0
    metadata: JsonDict = Field(default_factory=dict)
    source: str = "database"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_schema_contracts(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for key in ("input_schema", "output_schema"):
            if data.get(key) in (None, {}):
                data[key] = {"type": "object", "properties": {}}
        if not data.get("invocation") and data.get("type"):
            data["invocation"] = {"type": data["type"], "config": {}}
        return data

    @model_validator(mode="after")
    def validate_agent_definition(self) -> "AgentDefinition":
        if self.invocation.type != self.type:
            raise ValueError("invocation.type must match AgentDefinition.type")
        missing_from_schema = [
            item for item in self.required_inputs if item not in self.input_schema.required
        ]
        if missing_from_schema:
            self.input_schema.required.extend(missing_from_schema)
        if (
            self.type == "ui_handoff"
            and self.ui_handoff.mode != "none"
            and not self.ui_handoff.route
        ):
            raise ValueError("ui_handoff.route is required when ui_handoff.mode is not none")
        return self

    def is_available_to(self, user: UserContext) -> bool:
        return self.enabled and self.access_policy.allows(user)

    def to_candidate(self) -> "CandidateAgent":
        return CandidateAgent(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            domain=self.domain,
            capabilities=self.capabilities,
            trigger=self.trigger,
            required_inputs=self.required_inputs,
            priority=self.priority,
        )

    def to_public(self) -> "AgentPublic":
        return AgentPublic(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            version=self.version,
            enabled=self.enabled,
            type=self.type,
            capabilities=self.capabilities,
            domain=self.domain,
            tags=self.tags,
            trigger=self.trigger,
            access_policy=self.access_policy,
            required_inputs=self.required_inputs,
            optional_inputs=self.optional_inputs,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            ui_handoff=self.ui_handoff,
            priority=self.priority,
            metadata=redact_value(self.metadata),
            source=self.source,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class CandidateAgent(StrictBaseModel):
    agent_id: str
    name: str
    description: str
    domain: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    trigger: TriggerSpec = Field(default_factory=TriggerSpec)
    required_inputs: list[str] = Field(default_factory=list)
    priority: int = 0


class AgentPublic(StrictBaseModel):
    agent_id: str
    name: str
    description: str
    version: str | None = None
    enabled: bool
    type: AgentType
    capabilities: list[str] = Field(default_factory=list)
    domain: str | None = None
    tags: list[str] = Field(default_factory=list)
    trigger: TriggerSpec = Field(default_factory=TriggerSpec)
    access_policy: AccessPolicy = Field(default_factory=AccessPolicy)
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    input_schema: SchemaContract = Field(default_factory=SchemaContract)
    output_schema: SchemaContract = Field(default_factory=SchemaContract)
    ui_handoff: UiHandoffSpec = Field(default_factory=UiHandoffSpec)
    priority: int = 0
    metadata: JsonDict = Field(default_factory=dict)
    source: str = "database"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentListResponse(StrictBaseModel):
    agents: list[AgentPublic]


class AvailableAgentsRequest(StrictBaseModel):
    user: UserContext


class AvailableAgentsResponse(StrictBaseModel):
    available_agents: list[str]
    candidate_agents_for_llm: list[CandidateAgent]
    source: str
    expires_in: int | None = None


class AgentEnabledRequest(StrictBaseModel):
    enabled: bool
