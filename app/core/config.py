from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


RegistryBackend = Literal["database", "file", "hybrid"]
StorageBackend = Literal["memory", "database"]
RouteMode = Literal["route_only", "route_and_invoke"]
LLMProvider = Literal["mock", "openai_compatible"]
PlanExecutionPolicy = Literal[
    "return_plan_only",
    "require_confirmation",
    "auto_execute",
    "host_managed",
]


class Settings(BaseSettings):
    app_env: str = "local"
    log_level: str = "INFO"
    cors_allow_origins: str = (
        "http://127.0.0.1:5173,"
        "http://127.0.0.1:5174,"
        "http://localhost:5173,"
        "http://localhost:5174"
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

    agent_http_timeout_seconds: float = 30.0

    evidence_provider_enabled: bool = False
    evidence_fixed_questions_path: str = "./config/fixed_questions.example.yaml"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
