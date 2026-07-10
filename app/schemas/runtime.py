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
    admin_auth_mode: str
    registry_mutation_mode: str
    evidence_provider_enabled: bool
    evidence_fixed_questions_path: str | None = None
    agent_http_timeout_seconds: float
    memory_enabled: bool
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
