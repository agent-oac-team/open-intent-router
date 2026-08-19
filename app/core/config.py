from functools import lru_cache
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.memory_runtime import (
    RETIRED_MEMORY_BEHAVIOR_VARIABLES,
    MemoryMode,
    build_memory_runtime_policy,
    reject_retired_memory_behavior_variables,
)

RegistryBackend = Literal["database", "file", "hybrid"]
StorageBackend = Literal["memory", "database"]
RouteMode = Literal["route_only", "route_and_invoke"]
LLMProvider = Literal["mock", "openai_compatible"]
MemoryStrategyProvider = Literal["memory", "mem0"]
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
    def __init__(self, **values) -> None:
        retired_field_names = {name.lower() for name in RETIRED_MEMORY_BEHAVIOR_VARIABLES}
        retired_fields = retired_field_names.intersection(str(name).lower() for name in values)
        if retired_fields:
            names = ", ".join(sorted(name.upper() for name in retired_fields))
            raise ValueError(
                f"Retired Memory behavior settings detected: {names}. "
                "Use memory_mode or an explicit MemoryRuntimePolicy."
            )
        reject_retired_memory_behavior_variables()
        env_file = values.get("_env_file", self.model_config.get("env_file"))
        if env_file is not None:
            env_files = env_file if isinstance(env_file, list | tuple) else [env_file]
            for path in env_files:
                reject_retired_memory_behavior_variables(dotenv_values(path))
        super().__init__(**values)

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
    admin_actor_id: str = Field(default="admin", min_length=1, max_length=128)
    native_principal_secret: str | None = Field(default=None, repr=False)
    memory_identity_secret: str | None = Field(default=None)
    execution_ticket_secret: str | None = Field(default=None, repr=False)
    execution_ticket_lease_seconds: int = Field(default=30, ge=5, le=300)
    delegated_run_timeout_interval_seconds: float = Field(default=1.0, gt=0)
    delegated_run_timeout_batch_size: int = Field(default=100, ge=1, le=1000)

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
    runtime_catalog_shutdown_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    memory_mode: MemoryMode = "off"
    memory_import_legacy_history_enabled: bool = False
    memory_rehearsal_database_url: str | None = None
    memory_rehearsal_milvus_collection: str = "oir_memory_vectors_rehearsal"
    memory_formation_window_turns: int = Field(default=5, ge=1, le=100)
    memory_formation_idle_seconds: float = Field(default=30.0, gt=0)
    memory_formation_model: str = "formation-default"
    memory_formation_model_version: str = "formation-model-v1"
    memory_formation_prompt_version: str = "formation-prompt-v1"
    memory_formation_policy_version: str = "formation-policy-v1"
    memory_formation_auto_threshold: float = Field(default=0.90, ge=0, le=1)
    memory_formation_pending_threshold: float = Field(default=0.70, ge=0, le=1)
    memory_formation_model_timeout_seconds: float = Field(default=20.0, gt=0)
    memory_formation_lease_seconds: float = Field(default=60.0, gt=0)
    memory_formation_max_attempts: int = Field(default=5, ge=1)
    memory_formation_retry_base_seconds: float = Field(default=5.0, gt=0)
    memory_formation_retry_max_seconds: float = Field(default=300.0, gt=0)
    memory_maintenance_interval_seconds: float = Field(default=1.0, gt=0)
    memory_formation_sweep_interval_seconds: float = Field(default=5.0, gt=0)
    memory_consolidation_interval_seconds: float = Field(default=3600.0, gt=0)
    memory_formation_capsule_user_chars: int = Field(default=2000, ge=1, le=20000)
    memory_formation_capsule_assistant_chars: int = Field(default=2000, ge=1, le=20000)
    memory_formation_capsule_summary_chars: int = Field(default=1000, ge=1, le=10000)
    memory_formation_prompt_max_chars: int = Field(default=30000, ge=8000, le=100000)
    memory_strategy_provider: MemoryStrategyProvider = "memory"
    memory_prefetch_timeout_seconds: float = 3.0
    memory_default_max_items: int = 5
    memory_database_url: str | None = None
    memory_milvus_collection: str | None = None
    memory_milvus_uri: str | None = None
    memory_milvus_token: str | None = Field(default=None)
    memory_milvus_db_name: str | None = None
    memory_embedding_base_url: str | None = None
    memory_embedding_api_key: str | None = Field(default=None)
    memory_embedding_model: str | None = None
    memory_embedding_dims: int | None = None
    memory_task_ttl_days: int = 14
    memory_session_summary_ttl_days: int = 14
    memory_artifact_reference_ttl_days: int = 14
    memory_mem0_fail_closed: bool | None = None
    memory_mem0_vector_provider: Mem0VectorProvider = "milvus"
    memory_mem0_history_backend: Mem0HistoryBackend = "postgresql"
    memory_mem0_history_database_url: str | None = None
    memory_mem0_history_db_path: str = "./data/mem0-history.db"
    memory_mem0_llm_provider: str = "openai"
    memory_mem0_llm_model: str | None = None
    memory_mem0_llm_base_url: str | None = None
    memory_mem0_llm_api_key: str | None = Field(default=None)
    memory_mem0_embedder_provider: str = "openai"
    mem0_history_db_path: str = "./data/mem0-history.db"

    embedding_base_url: str | None = None
    embedding_api_key: str | None = Field(default=None)
    embedding_model: str | None = None
    embedding_dim: int | None = None

    knowledge_prefetch_timeout_seconds: float = 12.0
    knowledge_default_max_items: int = 5
    knowledge_context_handle_ttl_seconds: float = Field(default=60.0, gt=0)
    knowledge_provider_base_url: str | None = None
    knowledge_provider_deadline_seconds: float = Field(default=12.0, gt=0)
    knowledge_provider_jwt_issuer: str = Field(default="oir", min_length=1, max_length=128)
    knowledge_provider_jwt_audience: str = Field(
        default="knowledge_sys", min_length=1, max_length=128
    )
    knowledge_provider_jwt_key_id: str | None = Field(default=None, max_length=128)
    knowledge_provider_jwt_private_key: str | None = Field(default=None, repr=False)
    knowledge_provider_jwt_private_key_file: str | None = None
    knowledge_provider_jwt_ttl_seconds: int = Field(default=60, ge=1, le=300)
    knowledge_provider_circuit_window_seconds: float = Field(default=30.0, gt=0)
    knowledge_provider_circuit_failure_threshold: int = Field(default=5, ge=1)
    knowledge_provider_circuit_open_seconds: float = Field(default=30.0, gt=0)

    evidence_provider_enabled: bool = False
    evidence_fixed_questions_path: str = "./config/fixed_questions.example.yaml"
    evidence_provider_timeout_seconds: float = 1.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def validate_memory_formation_settings(self) -> "Settings":
        if self.memory_formation_pending_threshold >= self.memory_formation_auto_threshold:
            raise ValueError("memory formation pending threshold must be below auto threshold")
        if self.memory_formation_retry_base_seconds > self.memory_formation_retry_max_seconds:
            raise ValueError("memory formation retry base must not exceed retry maximum")
        if self.memory_formation_lease_seconds <= self.memory_formation_model_timeout_seconds:
            raise ValueError("memory formation lease must exceed model timeout")
        minimum_prompt_chars = 4000 + (950 * self.memory_formation_window_turns)
        if self.memory_formation_prompt_max_chars < minimum_prompt_chars:
            raise ValueError(
                "memory formation prompt budget is too small for the configured turn window; "
                f"requires at least {minimum_prompt_chars} characters"
            )
        if self.knowledge_provider_base_url:
            missing_provider_fields = []
            if self.knowledge_provider_jwt_issuer != "oir":
                missing_provider_fields.append("KNOWLEDGE_PROVIDER_JWT_ISSUER must be oir")
            if self.knowledge_provider_jwt_audience != "knowledge_sys":
                missing_provider_fields.append(
                    "KNOWLEDGE_PROVIDER_JWT_AUDIENCE must be knowledge_sys"
                )
            if not self.knowledge_provider_jwt_key_id:
                missing_provider_fields.append("KNOWLEDGE_PROVIDER_JWT_KEY_ID")
            configured_key_sources = sum(
                bool(value)
                for value in (
                    self.knowledge_provider_jwt_private_key,
                    self.knowledge_provider_jwt_private_key_file,
                )
            )
            if configured_key_sources != 1:
                missing_provider_fields.append(
                    "exactly one of KNOWLEDGE_PROVIDER_JWT_PRIVATE_KEY "
                    "or KNOWLEDGE_PROVIDER_JWT_PRIVATE_KEY_FILE"
                )
            if missing_provider_fields:
                raise ValueError(
                    "Knowledge Provider configuration is incomplete: "
                    + ", ".join(missing_provider_fields)
                )
        if self.memory_import_legacy_history_enabled:
            raise ValueError("legacy history import is not supported")
        if self.memory_mode != "off":
            missing = []
            if not self.memory_database_url:
                missing.append("MEMORY_DATABASE_URL")
            if self.memory_strategy_provider == "mem0":
                required_mem0 = {
                    "MEMORY_MILVUS_URI": self.memory_milvus_uri,
                    "MEMORY_MILVUS_COLLECTION": self.memory_milvus_collection,
                    "MEMORY_EMBEDDING_MODEL": self.memory_embedding_model,
                    "MEMORY_EMBEDDING_DIMS": self.memory_embedding_dims,
                }
                missing.extend(name for name, value in required_mem0.items() if not value)
            if missing:
                names = ", ".join(missing)
                raise ValueError(
                    "Memory is enabled but required explicit settings are missing: "
                    f"{names}. Configure the MEMORY_* namespace before startup."
                )
        return self

    @property
    def memory_runtime_policy(self):
        return build_memory_runtime_policy(self.memory_mode)

    # Compatibility projections. These are deliberately not Pydantic fields and
    # therefore cannot be configured independently through environment variables.
    @property
    def memory_enabled(self) -> bool:
        return self.memory_runtime_policy.memory_enabled

    @property
    def memory_recall_enabled(self) -> bool:
        return self.memory_runtime_policy.effective_recall_enabled

    @property
    def memory_formation_mode(self) -> str:
        return self.memory_runtime_policy.effective_formation_mode

    @property
    def memory_execution_mode(self) -> str:
        return self.memory_runtime_policy.execution_plane

    @property
    def memory_turn_outbox_consumer_enabled(self) -> bool:
        return self.memory_runtime_policy.turn_outbox_consumer_enabled

    @property
    def memory_formation_worker_enabled(self) -> bool:
        return self.memory_runtime_policy.formation_worker_enabled

    @property
    def memory_formation_sweeper_enabled(self) -> bool:
        return self.memory_runtime_policy.formation_sweeper_enabled

    @property
    def memory_index_worker_enabled(self) -> bool:
        return self.memory_runtime_policy.index_worker_enabled

    @property
    def memory_ttl_sweeper_enabled(self) -> bool:
        return self.memory_runtime_policy.ttl_sweeper_enabled

    @property
    def memory_consolidation_enabled(self) -> bool:
        return self.memory_runtime_policy.consolidation_enabled

    @property
    def context_route_memory_enabled(self) -> bool:
        return self.memory_runtime_policy.effective_governed_context_memory_enabled

    @property
    def context_route_memory_scopes(self) -> str:
        return ",".join(self.memory_runtime_policy.route_memory_scopes)

    @property
    def memory_mem0_fail_closed_effective(self) -> bool:
        if self.memory_mem0_fail_closed is not None:
            return self.memory_mem0_fail_closed
        return self.app_env != "local"

    @property
    def effective_memory_database_url(self) -> str | None:
        return self.memory_database_url

    @property
    def effective_memory_milvus_collection(self) -> str | None:
        return self.memory_milvus_collection

    @property
    def effective_memory_milvus_uri(self) -> str | None:
        return self.memory_milvus_uri

    @property
    def effective_memory_milvus_token(self) -> str | None:
        return self.memory_milvus_token

    @property
    def effective_memory_milvus_db_name(self) -> str | None:
        return self.memory_milvus_db_name


@lru_cache
def get_settings() -> Settings:
    return Settings()
