import re
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from app.core.redaction import redact_value
from app.schemas.agent_context import AgentContextSpec
from app.schemas.common import (
    AgentType,
    JsonDict,
    SchemaContract,
    StrictBaseModel,
    UserContext,
    normalize_entitlements,
)

_SYMBOLIC_REFERENCE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_AGENT_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
_SECRET_LIKE_SYMBOLIC_REFERENCE_PATTERNS = (
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^AIza[A-Za-z0-9_-]{35}$"),
    re.compile(r"^ghp_[A-Za-z0-9]{36}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{22,}$"),
    re.compile(r"^glpat-[A-Za-z0-9_-]{20,}$"),
    re.compile(r"^sk-(?:live|proj)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^sk_(?:live|test)_[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^xoxb-[0-9A-Za-z-]{20,}$"),
)
_ADMIN_UNREDACTED_HANDLING_TEXT_FIELDS = frozenset({"kind"})


def _decode_percent_encoding(value: str) -> str:
    decoded_value = value
    while True:
        next_value = unquote(decoded_value)
        if next_value == decoded_value:
            break
        decoded_value = next_value
    return decoded_value


def _is_safe_internal_route(value: str) -> bool:
    parsed = urlsplit(value)
    decoded_path = _decode_percent_encoding(parsed.path)
    return (
        value.startswith("/")
        and not value.startswith("//")
        and not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
        and not decoded_path.startswith("//")
        and not any(part == ".." for part in decoded_path.split("/"))
        and "\\" not in decoded_path
    )


def _validate_reference(value: str, *, label: str, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(value) or any(
        secret_pattern.fullmatch(value)
        for secret_pattern in _SECRET_LIKE_SYMBOLIC_REFERENCE_PATTERNS
    ):
        raise ValueError(f"{label} must be a logical identifier")
    return value


def is_safe_agent_identifier(value: object) -> bool:
    """Whether an Agent locator can be retained in an admin-facing projection.

    Definition source parsing deliberately remains tolerant so a malformed row
    can be quarantined rather than aborting an entire reload.  Snapshot/public
    projections use this stricter logical-ID gate before retaining the value.
    """

    return (
        isinstance(value, str)
        and bool(_AGENT_IDENTIFIER_PATTERN.fullmatch(value))
        and not any(
            secret_pattern.fullmatch(value)
            for secret_pattern in _SECRET_LIKE_SYMBOLIC_REFERENCE_PATTERNS
        )
    )


def _redact_admin_handling_text(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: (
                "***REDACTED***"
                if key not in _ADMIN_UNREDACTED_HANDLING_TEXT_FIELDS and isinstance(item, str)
                else _redact_admin_handling_text(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_admin_handling_text(item) for item in value]
    return value


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
    any_entitlements: list[str] = Field(default_factory=list)
    required_attributes: JsonDict = Field(default_factory=dict)

    @field_validator("any_entitlements", mode="before")
    @classmethod
    def normalize_any_entitlements(cls, value: Any) -> list[str]:
        return normalize_entitlements(value)

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
        if self.any_entitlements and not (set(user.entitlements) & set(self.any_entitlements)):
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


class SafeHandlingConfiguration(StrictBaseModel):
    """Closed deployment-neutral tuning plus symbolic operation references.

    Text fields are identifiers rather than free-form payloads.  Their concrete
    Adapter/Connector/Executor bindings are validated when a Registry Snapshot is built.
    """

    function: str | None = Field(default=None, min_length=1, max_length=64)
    operation: str | None = Field(default=None, min_length=1, max_length=64)
    task: str | None = Field(default=None, min_length=1, max_length=64)
    tab: str | None = Field(default=None, min_length=1, max_length=64)

    max_tokens: int | None = Field(default=None, ge=0, le=1_000_000)
    token_budget: int | None = Field(default=None, ge=0, le=1_000_000)
    timeout_ms: int | None = Field(default=None, ge=0, le=300_000)
    priority: int | None = Field(default=None, ge=-10_000, le=10_000)
    limit: int | None = Field(default=None, ge=0, le=100_000)
    offset: int | None = Field(default=None, ge=0, le=100_000)
    page_size: int | None = Field(default=None, ge=1, le=100_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0, le=100_000)
    seed: int | None = None
    dry_run: bool | None = None
    include_history: bool | None = None
    include_metadata: bool | None = None

    @field_validator("function", "operation", "task", "tab")
    @classmethod
    def validate_symbolic_operation_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_reference(
            value,
            label="handling configuration reference",
            pattern=_SYMBOLIC_REFERENCE_PATTERN,
        )


class InvocationHandling(StrictBaseModel):
    """The v2 declaration for an Agent handled by an Invocation Runtime Adapter."""

    kind: Literal["invocation"] = "invocation"
    adapter_key: str = Field(min_length=1, max_length=64)
    connector_ref: str | None = Field(default=None, min_length=1, max_length=128)
    config: SafeHandlingConfiguration = Field(default_factory=SafeHandlingConfiguration)

    @field_validator("adapter_key")
    @classmethod
    def validate_adapter_key(cls, value: str) -> str:
        return _validate_reference(
            value,
            label="adapter_key",
            pattern=_SYMBOLIC_REFERENCE_PATTERN,
        )

    @field_validator("connector_ref")
    @classmethod
    def validate_connector_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_reference(
            value,
            label="connector_ref",
            pattern=_SYMBOLIC_REFERENCE_PATTERN,
        )


class ExternalExecutionHandling(StrictBaseModel):
    """The v2 declaration for execution accepted by a Host External Executor."""

    kind: Literal["external_execution"] = "external_execution"
    executor_ref: str = Field(min_length=1, max_length=128)
    params: SafeHandlingConfiguration = Field(default_factory=SafeHandlingConfiguration)

    @field_validator("executor_ref")
    @classmethod
    def validate_executor_ref(cls, value: str) -> str:
        return _validate_reference(
            value,
            label="executor_ref",
            pattern=_SYMBOLIC_REFERENCE_PATTERN,
        )


class UiHandoffHandling(StrictBaseModel):
    """The v2 declaration for a Host collaboration result rather than Invocation."""

    kind: Literal["ui_handoff"] = "ui_handoff"
    route: str = Field(min_length=1, max_length=512)
    params: SafeHandlingConfiguration = Field(default_factory=SafeHandlingConfiguration)

    @field_validator("route")
    @classmethod
    def require_internal_route(cls, value: str) -> str:
        if not _is_safe_internal_route(value):
            raise ValueError("ui_handoff.route must be an internal route")
        return value


AgentHandlingKind = Literal["invocation", "external_execution", "ui_handoff"]
AgentHandling = Annotated[
    InvocationHandling | ExternalExecutionHandling | UiHandoffHandling,
    Field(discriminator="kind"),
]
_AGENT_HANDLING_ADAPTER = TypeAdapter(AgentHandling)


def _admin_handling_projection(handling: AgentHandling) -> JsonDict:
    """Return full safe handling details, or a non-leaking marker for bypassed validation."""

    raw_handling = handling.model_dump(mode="json", warnings=False)
    try:
        validated_handling = _AGENT_HANDLING_ADAPTER.validate_python(raw_handling)
    except ValidationError:
        kind = raw_handling.get("kind")
        safe_kind = (
            kind if kind in {"invocation", "external_execution", "ui_handoff"} else "redacted"
        )
        return {"kind": safe_kind, "redacted": True}
    projected_handling = _redact_admin_handling_text(
        validated_handling.model_dump(mode="json", exclude_none=True)
    )
    if not isinstance(projected_handling, dict):  # pragma: no cover - defensive narrowing
        return {"kind": "redacted", "redacted": True}
    return projected_handling


class AgentPublicV2(StrictBaseModel):
    agent_id: str
    name: str
    description: str
    version: str | None = None
    revision: int
    enabled: bool
    capabilities: list[str] = Field(default_factory=list)
    domain: str | None = None
    tags: list[str] = Field(default_factory=list)
    trigger: TriggerSpec = Field(default_factory=TriggerSpec)
    access_policy: AccessPolicy = Field(default_factory=AccessPolicy)
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    input_schema: SchemaContract = Field(default_factory=SchemaContract)
    output_schema: SchemaContract = Field(default_factory=SchemaContract)
    context: AgentContextSpec = Field(default_factory=AgentContextSpec)
    priority: int = 0
    source: str = "database"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    handling_kind: AgentHandlingKind


class CandidateAgentV2(StrictBaseModel):
    agent_id: str
    name: str
    description: str
    domain: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    trigger: TriggerSpec = Field(default_factory=TriggerSpec)
    required_inputs: list[str] = Field(default_factory=list)
    priority: int = 0
    handling_kind: AgentHandlingKind


class AgentAdminV2(AgentPublicV2):
    handling: JsonDict


class AgentDefinitionV2(StrictBaseModel):
    """The closed v2 Native Definition shape introduced alongside legacy v1 callers."""

    schema_version: Literal["oir-agent-v2"]
    agent_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    version: str | None = None
    revision: int = Field(default=0, ge=0)
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=list)
    domain: str | None = None
    tags: list[str] = Field(default_factory=list)
    trigger: TriggerSpec = Field(default_factory=TriggerSpec)
    access_policy: AccessPolicy = Field(default_factory=AccessPolicy)
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    input_schema: SchemaContract = Field(default_factory=SchemaContract)
    output_schema: SchemaContract = Field(default_factory=SchemaContract)
    context: AgentContextSpec = Field(default_factory=AgentContextSpec)
    priority: int = 0
    source: str = "database"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    handling: AgentHandling

    @model_validator(mode="after")
    def require_declared_inputs_in_schema(self) -> "AgentDefinitionV2":
        missing_from_schema = [
            item for item in self.required_inputs if item not in self.input_schema.required
        ]
        if missing_from_schema:
            self.input_schema.required.extend(missing_from_schema)
        return self

    def to_public(self) -> AgentPublicV2:
        return AgentPublicV2(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            version=self.version,
            revision=self.revision,
            enabled=self.enabled,
            capabilities=self.capabilities,
            domain=self.domain,
            tags=self.tags,
            trigger=self.trigger,
            access_policy=self.access_policy,
            required_inputs=self.required_inputs,
            optional_inputs=self.optional_inputs,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            context=self.context,
            priority=self.priority,
            source=self.source,
            created_at=self.created_at,
            updated_at=self.updated_at,
            handling_kind=self.handling.kind,
        )

    def to_candidate(self) -> CandidateAgentV2:
        return CandidateAgentV2(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            domain=self.domain,
            capabilities=self.capabilities,
            trigger=self.trigger,
            required_inputs=self.required_inputs,
            priority=self.priority,
            handling_kind=self.handling.kind,
        )

    def to_admin(self) -> AgentAdminV2:
        return AgentAdminV2(
            **self.to_public().model_dump(),
            handling=_admin_handling_projection(self.handling),
        )


class AgentDefinition(StrictBaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    version: str | None = None
    revision: int = Field(default=0, ge=0)
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
    context: AgentContextSpec = Field(default_factory=AgentContextSpec)
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
            revision=self.revision,
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
            context=self.context,
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
    revision: int = Field(default=0, ge=0)
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
    context: AgentContextSpec = Field(default_factory=AgentContextSpec)
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
