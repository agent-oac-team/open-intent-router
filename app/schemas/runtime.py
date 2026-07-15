from app.schemas.common import StrictBaseModel


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
    memory_enabled: bool
    memory_formation_mode: str
    memory_formation_model_version: str
    memory_formation_prompt_version: str
    memory_formation_policy_version: str
    memory_formation_worker_enabled: bool
    memory_formation_sweeper_enabled: bool
    memory_index_worker_enabled: bool
    memory_ttl_sweeper_enabled: bool
    memory_consolidation_enabled: bool
    memory_formation_queue_depth: int = 0
    memory_formation_oldest_pending_seconds: float | None = None
    memory_formation_dead_letter_count: int = 0
    memory_formation_last_error: str | None = None
    memory_index_out_of_sync_count: int = 0
    memory_index_dead_letter_count: int = 0
    memory_deletion_pending_count: int = 0
    memory_strategy_provider: str
    memory_prefetch_timeout_seconds: float
    memory_mem0_collection: str | None = None
    memory_mem0_vector_provider: str | None = None
    memory_mem0_milvus_uri: str | None = None
    memory_mem0_history_backend: str | None = None
    memory_mem0_fail_closed: bool = False
    memory_mem0_degraded: bool = False
    memory_mem0_last_error: str | None = None
    memory_mem0_health_status: str | None = None
    knowledge_enabled: bool
    knowledge_vector_backend: str
    knowledge_prefetch_timeout_seconds: float
    knowledge_milvus_collection: str | None = None
    knowledge_milvus_uri: str | None = None
    context_pipeline_mode: str
    context_route_memory_enabled: bool
    context_route_knowledge_enabled: bool
    context_policy_version: str
    context_budget_version: str
    context_projection_version: str
