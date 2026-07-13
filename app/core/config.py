from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

RegistryBackend = Literal["database", "file", "hybrid"]
StorageBackend = Literal["memory", "database"]
RouteMode = Literal["route_only", "route_and_invoke"]
LLMProvider = Literal["mock", "openai_compatible"]
MemoryStrategyProvider = Literal["memory", "mem0"]
KnowledgeVectorBackend = Literal["memory", "milvus"]
Mem0VectorProvider = Literal["milvus"]
Mem0HistoryBackend = Literal["postgresql", "sqlite", "none"]
PlanExecutionPolicy = Literal[
    "return_plan_only",
    "require_confirmation",
    "auto_execute",
    "host_managed",
]
ContextPipelineMode = Literal["legacy", "observe", "enforced"]


class Settings(BaseSettings):
    app_env: str = "local"
    log_level: str = "INFO"
    cors_allow_origins: str = (
        "http://127.0.0.1:5173,http://127.0.0.1:5174,http://localhost:5173,http://localhost:5174"
    )

    database_url: str = "sqlite+aiosqlite:///./data/open-intent-router.db"
    storage_backend: StorageBackend = "database"

    registry_backend: RegistryBackend = "file"
    registry_file_path: str = "./config/agents.example.yaml"
    registry_file_fallback_on_empty: bool = False

    router_llm_provider: LLMProvider = "mock"
    router_llm_model: str = "mock-router"
    router_llm_base_url: str | None = None
    router_llm_api_key: str | None = Field(default=None)
    router_llm_timeout_seconds: float = 20.0
    router_prompt_file: str | None = "./config/prompts/router.zh.yaml"

    route_mode: RouteMode = "route_and_invoke"
    default_plan_execution_policy: PlanExecutionPolicy = "require_confirmation"
    allow_local_auto_execute_plans: bool = False
    admin_api_token: str | None = Field(default=None)

    router_max_host_history_messages: int = 20
    router_max_agent_history_messages: int = 12
    router_max_recent_events: int = 10
    router_max_recent_results: int = 5
    router_low_confidence_threshold: float = 0.2
    context_default_token_budget: int = 2000
    context_max_token_budget: int = 8000
    context_default_source_budgets: str = ""
    context_chars_per_token: float = 4.0
    context_per_item_token_limit: int = 512
    context_per_item_char_limit: int = 2000
    context_allow_request_budget_override: bool = True
    context_allow_summary_placeholder: bool = True
    context_pipeline_mode: ContextPipelineMode = "legacy"
    context_route_memory_enabled: bool = False
    context_route_knowledge_enabled: bool = False
    context_route_knowledge_direct_reply_enabled: bool = False
    context_route_knowledge_min_score: float = 0.85
    context_route_memory_scopes: str = ""
    context_route_knowledge_source_ids: str = ""
    context_policy_version: str = "context-policy-v1"
    context_budget_version: str = "context-budget-v1"
    context_projection_version: str = "context-projection-v1"
    context_model_window_tokens: int = 16000
    context_system_reserve_tokens: int = 1200
    context_schema_reserve_tokens: int = 1400
    context_rules_reserve_tokens: int = 600
    context_output_reserve_tokens: int = 1200
    context_agent_token_budget: int = 3000

    agent_http_timeout_seconds: float = 30.0

    memory_enabled: bool = True
    memory_strategy_provider: MemoryStrategyProvider = "memory"
    memory_prefetch_timeout_seconds: float = 3.0
    memory_default_max_items: int = 5
    memory_task_ttl_days: int = 14
    memory_session_summary_ttl_days: int = 14
    memory_artifact_reference_ttl_days: int = 14
    memory_mem0_fail_closed: bool | None = None
    memory_mem0_vector_provider: Mem0VectorProvider = "milvus"
    memory_mem0_milvus_collection: str = "oir_memory_vectors"
    memory_mem0_milvus_uri: str = ".data/oir_memory_milvus.db"
    memory_mem0_milvus_token: str | None = Field(default=None)
    memory_mem0_milvus_db_name: str | None = None
    memory_mem0_history_backend: Mem0HistoryBackend = "postgresql"
    memory_mem0_history_database_url: str | None = None
    memory_mem0_history_db_path: str = "./data/mem0-history.db"
    memory_mem0_embedding_base_url: str | None = None
    memory_mem0_embedding_api_key: str | None = Field(default=None)
    memory_mem0_embedding_model: str | None = None
    memory_mem0_embedding_dims: int | None = None
    memory_mem0_llm_provider: str = "openai"
    memory_mem0_llm_model: str | None = None
    memory_mem0_llm_base_url: str | None = None
    memory_mem0_llm_api_key: str | None = Field(default=None)
    memory_mem0_embedder_provider: str = "openai"
    memory_mem0_embedder_model: str | None = None
    memory_milvus_collection: str = "oir_memory_vectors"
    mem0_history_db_path: str = "./data/mem0-history.db"
    mem0_config_json: str = ""

    embedding_base_url: str | None = None
    embedding_api_key: str | None = Field(default=None)
    embedding_model: str | None = None
    embedding_dim: int | None = None

    knowledge_enabled: bool = True
    knowledge_vector_backend: KnowledgeVectorBackend = "memory"
    knowledge_prefetch_timeout_seconds: float = 5.0
    knowledge_default_max_items: int = 5
    knowledge_milvus_collection: str = "oir_knowledge_vectors"
    knowledge_milvus_uri: str | None = ".data/oir_knowledge_milvus.db"
    knowledge_milvus_token: str | None = None
    knowledge_milvus_db_name: str | None = None
    knowledge_embedding_base_url: str | None = None
    knowledge_embedding_api_key: str | None = Field(default=None)
    knowledge_embedding_model: str = "text-embedding-v4"
    knowledge_embedding_dim: int = 1024
    knowledge_default_source_ids: str = ""

    evidence_provider_enabled: bool = False
    evidence_fixed_questions_path: str = "./config/fixed_questions.example.yaml"
    evidence_provider_timeout_seconds: float = 1.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def memory_mem0_fail_closed_effective(self) -> bool:
        if self.memory_mem0_fail_closed is not None:
            return self.memory_mem0_fail_closed
        return self.app_env != "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
