from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.memory_runtime import MemoryRuntimePolicy
from app.core.redaction import redact_connection_location
from app.dependencies import (
    get_memory_observability_service,
    get_memory_runtime_policy,
    get_registry_service,
)
from app.schemas.runtime import RuntimeConfigResponse
from app.services.mem0_config import mem0_health_check, memory_infrastructure_metadata
from app.services.memory_observability import MemoryObservabilityService
from app.services.registry_service import AgentRegistryService

router = APIRouter(prefix="/api/v1", tags=["runtime"])


@router.get("/runtime/config", response_model=RuntimeConfigResponse)
async def runtime_config(
    settings: Settings = Depends(get_settings),
    registry: AgentRegistryService = Depends(get_registry_service),
    memory_observability: MemoryObservabilityService = Depends(get_memory_observability_service),
    memory_policy: MemoryRuntimePolicy = Depends(get_memory_runtime_policy),
) -> RuntimeConfigResponse:
    if registry.state.active_source == "none":
        await registry.load()
    admin_auth_mode = _admin_auth_mode(settings)
    registry_mutation_mode = _registry_mutation_mode(settings, admin_auth_mode)
    mem0_metadata = mem0_health_check(settings)
    memory_infrastructure = memory_infrastructure_metadata(settings)
    memory_health = await memory_observability.health()
    return RuntimeConfigResponse(
        app_env=settings.app_env,
        storage_backend=settings.storage_backend,
        registry_backend=settings.registry_backend,
        registry_status=registry.state.status,
        registry_active_source=registry.state.active_source,
        registry_message=registry.state.message,
        registry_agent_count=len(registry.state.agents),
        route_mode=settings.route_mode,
        router_llm_provider=settings.router_llm_provider,
        router_llm_model=settings.router_llm_model,
        router_llm_base_url=(
            redact_connection_location(settings.router_llm_base_url)
            if settings.router_llm_base_url
            else None
        ),
        router_prompt_file=settings.router_prompt_file,
        router_llm_api_key_configured=bool(settings.router_llm_api_key),
        admin_api_token_configured=bool(settings.admin_api_token),
        memory_identity_secret_configured=bool(settings.memory_identity_secret),
        admin_auth_mode=admin_auth_mode,
        registry_mutation_mode=registry_mutation_mode,
        evidence_provider_enabled=settings.evidence_provider_enabled,
        evidence_fixed_questions_path=settings.evidence_fixed_questions_path,
        agent_http_timeout_seconds=settings.agent_http_timeout_seconds,
        memory_mode=memory_policy.mode,
        memory_policy_version=memory_policy.version,
        memory_config_source=memory_policy.config_source,
        memory_enabled=memory_policy.memory_enabled,
        memory_recall_enabled=memory_policy.effective_recall_enabled,
        memory_formation_mode=memory_policy.effective_formation_mode,
        memory_execution_mode=memory_policy.execution_plane,
        memory_turn_outbox_consumer_enabled=memory_policy.turn_outbox_consumer_enabled,
        memory_formation_model_version=settings.memory_formation_model_version,
        memory_formation_prompt_version=settings.memory_formation_prompt_version,
        memory_formation_policy_version=settings.memory_formation_policy_version,
        memory_formation_worker_enabled=memory_policy.effective_formation_worker_enabled,
        memory_formation_sweeper_enabled=memory_policy.effective_formation_sweeper_enabled,
        memory_index_worker_enabled=memory_policy.effective_index_worker_enabled,
        memory_ttl_sweeper_enabled=memory_policy.effective_ttl_sweeper_enabled,
        memory_consolidation_enabled=memory_policy.consolidation_enabled,
        memory_governed_context_enabled=(memory_policy.effective_governed_context_memory_enabled),
        memory_route_scopes=list(memory_policy.route_memory_scopes),
        memory_formation_queue_depth=memory_health.queue_depth,
        memory_formation_oldest_pending_seconds=memory_health.oldest_pending_seconds,
        memory_formation_dead_letter_count=memory_health.dead_letter_count,
        memory_formation_last_error=memory_health.last_safe_error,
        memory_index_out_of_sync_count=memory_health.index_out_of_sync_count,
        memory_index_dead_letter_count=memory_health.index_dead_letter_count,
        memory_deletion_pending_count=memory_health.deletion_pending_count,
        memory_strategy_provider=settings.memory_strategy_provider,
        memory_prefetch_timeout_seconds=settings.memory_prefetch_timeout_seconds,
        memory_database_url=(
            redact_connection_location(str(memory_infrastructure["database_url"]))
            if memory_infrastructure.get("database_url")
            else None
        ),
        memory_mem0_collection=mem0_metadata.get("collection"),
        memory_mem0_vector_provider=mem0_metadata.get("vector_provider"),
        memory_mem0_milvus_uri=(
            redact_connection_location(str(mem0_metadata["milvus_uri"]))
            if mem0_metadata.get("milvus_uri")
            else None
        ),
        memory_mem0_history_backend=mem0_metadata.get("history_backend"),
        memory_mem0_fail_closed=bool(mem0_metadata.get("fail_closed")),
        memory_mem0_degraded=mem0_metadata.get("status") == "degraded",
        memory_mem0_last_error=None,
        memory_mem0_health_status=mem0_metadata.get("status"),
        memory_embedding_model=memory_infrastructure.get("embedding_model"),
        memory_embedding_dims=memory_infrastructure.get("embedding_dims"),
        memory_infrastructure_sources=memory_infrastructure.get("configuration_sources", {}),
        memory_rehearsal_collection=(
            settings.memory_rehearsal_milvus_collection
            if memory_policy.execution_plane == "state_rehearsal"
            else None
        ),
        context_pipeline_mode=settings.context_pipeline_mode,
        context_route_memory_enabled=(memory_policy.effective_governed_context_memory_enabled),
        context_policy_version=settings.context_policy_version,
        context_budget_version=settings.context_budget_version,
        context_projection_version=settings.context_projection_version,
    )


def _admin_auth_mode(settings: Settings) -> str:
    if settings.admin_api_token:
        return "token_required"
    if settings.app_env == "local":
        return "local_loopback_open"
    return "token_missing"


def _registry_mutation_mode(settings: Settings, admin_auth_mode: str) -> str:
    if settings.registry_backend == "file":
        return "read_only_file"
    if admin_auth_mode == "local_loopback_open":
        return "local_dev_write_enabled"
    if admin_auth_mode == "token_required":
        return "token_required"
    return "disabled_token_missing"
