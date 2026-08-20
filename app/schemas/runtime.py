from typing import Literal

from pydantic import Field

from app.schemas.common import JsonDict, StrictBaseModel


class ReadinessResponse(StrictBaseModel):
    status: Literal["ok", "degraded"]
    registry_status: Literal["ok", "degraded"]
    active_source: str | None = None
    message: None = None
    runtime_status: Literal["ready", "degraded"] = "ready"
    reason_code: str | None = None
    impacted_definition_count: int = Field(default=0, ge=0)


class RuntimeCatalogReadinessErrorResponse(StrictBaseModel):
    status: Literal["error"]
    runtime_status: Literal["error"]
    runtime_reason: str


class RuntimeInventoryDefinition(StrictBaseModel):
    agent_id: str
    revision: int = Field(ge=0)
    enabled: bool
    handling_kind: Literal["invocation", "external_execution", "ui_handoff"]
    handling: JsonDict
    binding_status: Literal["ready", "isolated", "disabled"]
    isolation_reason_code: str | None = None


class RuntimeInventoryQuarantine(StrictBaseModel):
    source_index: int = Field(ge=0)
    agent_id: str | None = None
    reason_code: str


class RuntimeInventoryResponse(StrictBaseModel):
    status: Literal["ok", "degraded", "error"]
    runtime_status: Literal["ready", "degraded", "error"]
    registry_status: Literal["ok", "degraded", "error"]
    active_source: str | None = None
    reason_code: str | None = None
    impacted_definition_count: int = Field(default=0, ge=0)
    quarantined_definition_count: int = Field(default=0, ge=0)
    quarantined_definitions: list[RuntimeInventoryQuarantine] = Field(default_factory=list)
    definitions: list[RuntimeInventoryDefinition] = Field(default_factory=list)


class RuntimeConfigResponse(StrictBaseModel):
    app_env: str
    storage_backend: str
    registry_backend: str
    registry_status: str
    registry_active_source: str
    registry_message: str = ""
    registry_agent_count: int = 0
    route_mode: str
    router_llm_provider: str
    router_llm_model: str
    router_llm_base_url: str | None = None
    router_prompt_file: str | None = None
    router_llm_api_key_configured: bool = False
    admin_api_token_configured: bool = False
    memory_identity_secret_configured: bool = False
    admin_auth_mode: str
    registry_mutation_mode: str
    evidence_provider_enabled: bool
    evidence_fixed_questions_path: str | None = None
    agent_http_timeout_seconds: float
    memory_mode: str
    memory_policy_version: str
    memory_config_source: str
    memory_enabled: bool
    memory_recall_enabled: bool
    memory_formation_mode: str
    memory_execution_mode: str
    memory_turn_outbox_consumer_enabled: bool
    memory_formation_model_version: str
    memory_formation_prompt_version: str
    memory_formation_policy_version: str
    memory_formation_worker_enabled: bool
    memory_formation_sweeper_enabled: bool
    memory_index_worker_enabled: bool
    memory_ttl_sweeper_enabled: bool
    memory_consolidation_enabled: bool
    memory_governed_context_enabled: bool
    memory_route_scopes: list[str]
    memory_formation_queue_depth: int = 0
    memory_formation_oldest_pending_seconds: float | None = None
    memory_formation_dead_letter_count: int = 0
    memory_formation_last_error: str | None = None
    memory_index_out_of_sync_count: int = 0
    memory_index_dead_letter_count: int = 0
    memory_deletion_pending_count: int = 0
    memory_strategy_provider: str
    memory_prefetch_timeout_seconds: float
    memory_database_url: str | None = None
    memory_mem0_collection: str | None = None
    memory_mem0_vector_provider: str | None = None
    memory_mem0_milvus_uri: str | None = None
    memory_mem0_history_backend: str | None = None
    memory_mem0_fail_closed: bool = False
    memory_mem0_degraded: bool = False
    memory_mem0_last_error: str | None = None
    memory_mem0_health_status: str | None = None
    memory_embedding_model: str | None = None
    memory_embedding_dims: int | None = None
    memory_infrastructure_sources: dict[str, str] = Field(default_factory=dict)
    memory_rehearsal_collection: str | None = None
    context_pipeline_mode: str
    context_route_memory_enabled: bool
    context_policy_version: str
    context_budget_version: str
    context_projection_version: str
