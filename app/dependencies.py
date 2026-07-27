from functools import lru_cache
from uuid import uuid4

from app.core.config import get_settings
from app.core.memory_runtime import MemoryRuntimePolicy, build_memory_runtime_policy
from app.db.session import create_session_factory
from app.llm.conversation_formation import OpenAICompatibleConversationFormationModel
from app.plugins.evidence import build_evidence_provider
from app.repositories.canonical_invocations import (
    DatabaseCanonicalInvocationStore,
    MemoryCanonicalInvocationStore,
)
from app.repositories.context_stores import (
    DatabaseKnowledgeRepository,
    DatabaseMemoryItemRepository,
    KnowledgeRepository,
    MemoryItemRepository,
)
from app.repositories.database import (
    DatabaseAgentDefinitionRepository,
    DatabaseEventRepository,
    DatabaseMessageRepository,
    DatabasePlanRepository,
    DatabaseResultRepository,
    DatabaseRouteLogRepository,
    DatabaseRunRepository,
)
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunCompletionStore,
    DatabaseDelegatedRunFailureStore,
    DatabaseDelegatedRunMaintenanceStore,
    DatabaseDelegatedRunProgressStore,
    DatabaseDelegatedRunStartStore,
    MemoryDelegatedRunCompletionStore,
    MemoryDelegatedRunFailureStore,
    MemoryDelegatedRunMaintenanceStore,
    MemoryDelegatedRunProgressStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.execution_traces import (
    DatabaseExecutionTraceRepository,
    MemoryExecutionTraceRepository,
)
from app.repositories.file_registry import FileRegistrySource
from app.repositories.knowledge_assets import (
    DatabaseCanonicalKnowledgeRepository,
    MemoryCanonicalKnowledgeRepository,
)
from app.repositories.memory import (
    MemoryAgentDefinitionRepository,
    MemoryEventRepository,
    MemoryMessageRepository,
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRouteLogRepository,
    MemoryRunRepository,
)
from app.repositories.memory_formation import (
    DatabaseMemoryFormationTurnJobRepository,
    MemoryFormationTurnJobRepository,
)
from app.repositories.memory_traces import (
    DatabaseMemoryFormationTraceRepository,
    MemoryFormationTraceRepository,
)
from app.repositories.turn_outbox import (
    DatabaseTurnOutboxRepository,
    MemoryTurnOutboxRepository,
)
from app.repositories.turn_route_completion import (
    DatabaseRouteTurnCompletionStore,
    MemoryRouteTurnCompletionStore,
)
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.chat_history_service import ChatHistoryService
from app.services.context_service import ContextService
from app.services.delegated_run_service import DelegatedRunService
from app.services.event_service import EventService
from app.services.execution_trace_service import ExecutionTraceService
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.knowledge_asset_service import KnowledgeAssetService
from app.services.knowledge_service import KnowledgeService
from app.services.memory_candidate_hard_rules import MemoryCandidateHardRules
from app.services.memory_candidate_policy import MemoryCandidatePolicy
from app.services.memory_candidate_safety_filter import TemporaryLanguageSafetyFilter
from app.services.memory_candidate_semantics import MemoryCandidateSemanticValidator
from app.services.memory_formation import (
    FormationIdleSweeper,
    FormationJobWorker,
    FormationTriggerCoordinator,
    MemoryFormationRuntime,
    MemoryFormationRuntimeStatus,
    TurnCapsuleBuilder,
    TurnCaptureService,
    TurnOutboxFormationConsumer,
    UnavailableFormationJobProcessor,
)
from app.services.memory_integration import (
    MemoryFormationProcessor,
    StructuredFormationPublisher,
)
from app.services.memory_maintenance import (
    MemoryMaintenanceRuntime,
    MemoryMaintenanceRuntimeStatus,
)
from app.services.memory_management import MemoryManagementService
from app.services.memory_observability import MemoryObservabilityService
from app.services.memory_service import MemoryService
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService
from app.services.registry_service import AgentRegistryService
from app.services.router_service import RouterService
from app.services.task_continuation import TaskMemoryPlanResolver
from app.services.turn_service import TurnService

_memory_runtime_policy_override: MemoryRuntimePolicy | None = None
_memory_runtime_database_url: str | None = None
_memory_runtime_collection: str | None = None


def configure_memory_runtime(
    policy: MemoryRuntimePolicy,
    *,
    database_url: str | None = None,
    collection: str | None = None,
) -> None:
    global _memory_runtime_policy_override
    global _memory_runtime_database_url
    global _memory_runtime_collection
    _memory_runtime_policy_override = policy
    _memory_runtime_database_url = database_url
    _memory_runtime_collection = collection
    get_memory_runtime_policy.cache_clear()
    get_memory_data_settings.cache_clear()


@lru_cache
def get_memory_runtime_policy() -> MemoryRuntimePolicy:
    return _memory_runtime_policy_override or build_memory_runtime_policy(
        get_settings().memory_mode
    )


def _memory_data_settings(
    settings,
    policy: MemoryRuntimePolicy | None = None,
    *,
    database_url: str | None = None,
    collection: str | None = None,
):
    resolved_policy = policy or get_memory_runtime_policy()
    if resolved_policy.execution_plane == "state_rehearsal":
        resolved_database_url = database_url or _memory_runtime_database_url
        resolved_collection = collection or _memory_runtime_collection
        if not resolved_database_url:
            raise ValueError("State Rehearsal requires an isolated Memory database URL")
        if resolved_database_url == settings.database_url:
            raise ValueError("State Rehearsal Memory database must differ from canonical database")
        if not resolved_collection or resolved_collection in {
            settings.memory_milvus_collection,
            settings.knowledge_milvus_collection,
        }:
            raise ValueError("State Rehearsal Memory collection must be isolated")
        return settings.model_copy(
            update={
                "database_url": resolved_database_url,
                "memory_milvus_collection": resolved_collection,
                "memory_mem0_milvus_collection": resolved_collection,
                "memory_mem0_history_database_url": resolved_database_url,
            }
        )
    return settings


@lru_cache
def get_memory_data_settings():
    return _memory_data_settings(get_settings())


@lru_cache
def get_registry_service() -> AgentRegistryService:
    settings = get_settings()
    if settings.storage_backend == "memory":
        repository = MemoryAgentDefinitionRepository()
    else:
        repository = DatabaseAgentDefinitionRepository(create_session_factory(settings))
    return AgentRegistryService(
        settings=settings,
        repository=repository,
        file_source=FileRegistrySource(settings.registry_file_path),
    )


@lru_cache
def get_repository_bundle() -> dict:
    settings = get_settings()
    if settings.storage_backend == "memory":
        return {
            "messages": MemoryMessageRepository(),
            "events": MemoryEventRepository(),
            "runs": MemoryRunRepository(),
            "results": MemoryResultRepository(),
            "plans": MemoryPlanRepository(),
            "route_logs": MemoryRouteLogRepository(),
        }
    session_factory = create_session_factory(settings)
    return {
        "messages": DatabaseMessageRepository(session_factory),
        "events": DatabaseEventRepository(session_factory),
        "runs": DatabaseRunRepository(session_factory),
        "results": DatabaseResultRepository(session_factory),
        "plans": DatabasePlanRepository(session_factory),
        "route_logs": DatabaseRouteLogRepository(session_factory),
    }


@lru_cache
def get_context_repository_bundle() -> dict:
    settings = get_settings()
    if settings.storage_backend == "database":
        memory_session_factory = create_session_factory(get_memory_data_settings())
        knowledge_session_factory = create_session_factory(settings)
        return {
            "memory_items": DatabaseMemoryItemRepository(memory_session_factory),
            "knowledge": DatabaseKnowledgeRepository(knowledge_session_factory),
        }
    return {
        "memory_items": MemoryItemRepository(),
        "knowledge": KnowledgeRepository(),
    }


@lru_cache
def get_memory_service() -> MemoryService:
    settings = get_memory_data_settings()
    repositories = get_context_repository_bundle()
    return MemoryService(
        settings=settings,
        repository=repositories["memory_items"],
        formation_repository=get_memory_formation_repository(),
        runtime_policy=get_memory_runtime_policy(),
        execution_traces=get_memory_index_trace_service(),
        turns=get_turn_service(),
    )


@lru_cache
def get_memory_formation_repository():
    settings = get_memory_data_settings()
    if settings.storage_backend == "database":
        return DatabaseMemoryFormationTurnJobRepository(create_session_factory(settings))
    return MemoryFormationTurnJobRepository()


@lru_cache
def get_memory_formation_runtime_status() -> MemoryFormationRuntimeStatus:
    return MemoryFormationRuntimeStatus()


@lru_cache
def get_memory_maintenance_runtime_status() -> MemoryMaintenanceRuntimeStatus:
    return MemoryMaintenanceRuntimeStatus()


def build_memory_maintenance_runtime(*, settings=None, memory_service=None):
    resolved_settings = settings or get_settings()
    runtime_policy = (
        get_memory_runtime_policy()
        if settings is None
        else build_memory_runtime_policy(resolved_settings.memory_mode)
    )
    return MemoryMaintenanceRuntime(
        settings=resolved_settings,
        memory_service=memory_service or get_memory_service(),
        status=get_memory_maintenance_runtime_status(),
        runtime_policy=runtime_policy,
    )


@lru_cache
def get_memory_trace_repository():
    settings = get_memory_data_settings()
    if settings.storage_backend == "database":
        return DatabaseMemoryFormationTraceRepository(create_session_factory(settings))
    return MemoryFormationTraceRepository(
        formation_repository=get_memory_formation_repository(),
        event_repository=get_memory_service().repository,
    )


@lru_cache
def get_memory_observability_service() -> MemoryObservabilityService:
    return MemoryObservabilityService(
        settings=get_settings(),
        memory_service=get_memory_service(),
        formation_repository=get_memory_formation_repository(),
        trace_repository=get_memory_trace_repository(),
        runtime_status=get_memory_formation_runtime_status(),
        maintenance_status=get_memory_maintenance_runtime_status(),
        turn_repository=get_turn_repository(),
        outbox_repository=get_turn_outbox_repository(),
        runtime_policy=get_memory_runtime_policy(),
    )


@lru_cache
def get_memory_management_service() -> MemoryManagementService:
    return MemoryManagementService(
        memory_service=get_memory_service(),
        execution_traces=get_execution_trace_service(),
    )


def build_memory_formation_runtime(
    *, settings=None, processor=None, repository=None, reconciler=None
) -> MemoryFormationRuntime:
    resolved_settings = settings or get_settings()
    runtime_policy = (
        get_memory_runtime_policy()
        if settings is None
        else build_memory_runtime_policy(resolved_settings.memory_mode)
    )
    memory_settings = _memory_data_settings(resolved_settings, runtime_policy)
    if runtime_policy.effective_formation_mode == "off":
        resolved_repository = repository or MemoryFormationTurnJobRepository()
        resolved_processor = processor or UnavailableFormationJobProcessor()
    else:
        resolved_repository = repository or get_memory_formation_repository()
        resolved_processor = processor or get_memory_formation_processor()
    resolved_reconciler = reconciler
    if resolved_reconciler is None:
        resolved_reconciler = TurnOutboxFormationConsumer(
            settings=resolved_settings,
            outbox_repository=get_turn_outbox_repository(),
            turn_repository=get_turn_repository(),
            builder=TurnCapsuleBuilder(resolved_settings),
            coordinator=FormationTriggerCoordinator(
                settings=resolved_settings,
                repository=resolved_repository,
                runtime_policy=runtime_policy,
            ),
            event_repository=get_memory_service().repository,
            owner=f"turn-outbox-formation-{uuid4().hex}",
            runtime_policy=runtime_policy,
        )
    return MemoryFormationRuntime(
        settings=memory_settings,
        worker=FormationJobWorker(
            settings=resolved_settings,
            repository=resolved_repository,
            processor=resolved_processor,
            owner=f"formation-worker-{uuid4().hex}",
            runtime_policy=runtime_policy,
        ),
        sweeper=FormationIdleSweeper(
            settings=resolved_settings,
            repository=resolved_repository,
            runtime_policy=runtime_policy,
        ),
        reconciler=resolved_reconciler,
        status=get_memory_formation_runtime_status(),
        runtime_policy=runtime_policy,
    )


@lru_cache
def get_knowledge_service() -> KnowledgeService:
    settings = get_settings()
    repositories = get_context_repository_bundle()
    return KnowledgeService(settings=settings, repository=repositories["knowledge"])


@lru_cache
def get_knowledge_asset_repository():
    settings = get_settings()
    if settings.storage_backend == "database":
        return DatabaseCanonicalKnowledgeRepository(create_session_factory(settings))
    return MemoryCanonicalKnowledgeRepository()


@lru_cache
def get_knowledge_asset_service() -> KnowledgeAssetService:
    return KnowledgeAssetService(get_knowledge_asset_repository())


def get_agent_context_service() -> AgentContextAssemblyService:
    settings = get_settings()
    return AgentContextAssemblyService(
        settings=settings,
        memory_service=get_memory_service(),
        knowledge_service=get_knowledge_service(),
        runtime_policy=get_memory_runtime_policy(),
    )


@lru_cache
def get_structured_formation_publisher() -> StructuredFormationPublisher | None:
    return None


@lru_cache
def get_turn_capture_service() -> TurnCaptureService | None:
    return None


@lru_cache
def get_memory_formation_processor():
    settings = get_memory_data_settings()
    memory_service = get_memory_service()
    hard_rules = MemoryCandidateHardRules(repository=memory_service.repository)
    semantic_validator = MemoryCandidateSemanticValidator()
    safety_filter = TemporaryLanguageSafetyFilter()
    return MemoryFormationProcessor(
        repository=get_memory_formation_repository(),
        memory_repository=memory_service.repository,
        model=OpenAICompatibleConversationFormationModel(settings),
        policy=MemoryCandidatePolicy(
            settings=settings,
            repository=memory_service.repository,
            hard_rules=hard_rules,
            semantic_validator=semantic_validator,
            safety_filter=safety_filter,
            verifier=None,
        ),
        lifecycle=memory_service.lifecycle,
        execution_traces=get_execution_trace_service(),
        turns=get_turn_service(),
    )


def get_router_service() -> RouterService:
    settings = get_settings()
    repositories = get_repository_bundle()
    return RouterService(
        settings=settings,
        registry=get_registry_service(),
        context_service=ContextService(
            settings,
            memory_service=get_memory_service(),
            knowledge_service=get_knowledge_service(),
            runtime_policy=get_memory_runtime_policy(),
        ),
        chat_history_service=get_chat_history_service(),
        result_repository=repositories["results"],
        event_service=EventService(repositories["events"]),
        route_log_repository=repositories["route_logs"],
        evidence_provider=build_evidence_provider(settings),
        plan_service=get_plan_service(),
        agent_context_service=get_agent_context_service(),
        plan_continuation_resolver=get_task_memory_plan_resolver(),
        turn_service=get_turn_service(),
        runtime_policy=get_memory_runtime_policy(),
    )


@lru_cache
def get_turn_repository():
    settings = get_settings()
    if settings.storage_backend == "database":
        return DatabaseTurnRepository(create_session_factory(settings))
    return MemoryTurnRepository()


def get_turn_service() -> TurnService:
    settings = get_settings()
    turns = get_turn_repository()
    outbox = get_turn_outbox_repository()
    completion_store = (
        DatabaseRouteTurnCompletionStore(create_session_factory(settings))
        if settings.storage_backend == "database"
        else MemoryRouteTurnCompletionStore(
            turn_repository=turns,
            outbox_repository=outbox,
        )
    )
    return TurnService(turns, route_completion_store=completion_store)


@lru_cache
def get_turn_outbox_repository():
    settings = get_settings()
    if settings.storage_backend == "database":
        return DatabaseTurnOutboxRepository(create_session_factory(settings))
    return MemoryTurnOutboxRepository()


@lru_cache
def get_delegated_run_service() -> DelegatedRunService:
    settings = get_settings()
    if settings.storage_backend == "database":
        factory = create_session_factory(settings)
        return DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            progress_store=DatabaseDelegatedRunProgressStore(factory),
            completion_store=DatabaseDelegatedRunCompletionStore(factory),
            failure_store=DatabaseDelegatedRunFailureStore(factory),
            maintenance_store=DatabaseDelegatedRunMaintenanceStore(factory),
        )
    repositories = get_repository_bundle()
    turns = get_turn_repository()
    outbox = get_turn_outbox_repository()
    return DelegatedRunService(
        MemoryDelegatedRunStartStore(
            run_repository=repositories["runs"],
            turn_repository=turns,
        ),
        progress_store=MemoryDelegatedRunProgressStore(
            run_repository=repositories["runs"],
            event_repository=repositories["events"],
        ),
        completion_store=MemoryDelegatedRunCompletionStore(
            run_repository=repositories["runs"],
            result_repository=repositories["results"],
            event_repository=repositories["events"],
            turn_repository=turns,
            outbox_repository=outbox,
            plan_repository=repositories["plans"],
        ),
        failure_store=MemoryDelegatedRunFailureStore(
            run_repository=repositories["runs"],
            event_repository=repositories["events"],
            turn_repository=turns,
            outbox_repository=outbox,
            plan_repository=repositories["plans"],
        ),
        maintenance_store=MemoryDelegatedRunMaintenanceStore(
            run_repository=repositories["runs"],
            event_repository=repositories["events"],
            turn_repository=turns,
            outbox_repository=outbox,
        ),
    )


def get_invocation_service() -> InvocationService:
    settings = get_settings()
    repositories = get_repository_bundle()
    return InvocationService(
        registry=get_registry_service(),
        run_repository=repositories["runs"],
        result_repository=repositories["results"],
        invokers=build_default_invoker_registry(settings),
        agent_context_service=get_agent_context_service(),
        plan_service=get_plan_service(),
        turn_capture=get_turn_capture_service(),
        structured_formation=get_structured_formation_publisher(),
        memory_service=get_memory_service(),
        canonical_invocation_store=get_canonical_invocation_store(),
        runtime_policy=get_memory_runtime_policy(),
        memory_formation_policy_version=settings.memory_formation_policy_version,
    )


@lru_cache
def get_canonical_invocation_store():
    settings = get_settings()
    if settings.storage_backend == "database":
        return DatabaseCanonicalInvocationStore(create_session_factory(settings))
    repositories = get_repository_bundle()
    return MemoryCanonicalInvocationStore(
        run_repository=repositories["runs"],
        result_repository=repositories["results"],
        turn_repository=get_turn_repository(),
        outbox_repository=get_turn_outbox_repository(),
    )


def get_chat_history_service() -> ChatHistoryService:
    settings = get_settings()
    repositories = get_repository_bundle()
    return ChatHistoryService(
        repositories["messages"],
        host_limit=settings.router_max_host_history_messages,
        agent_limit=settings.router_max_agent_history_messages,
    )


def get_event_service() -> EventService:
    repositories = get_repository_bundle()
    return EventService(repositories["events"])


@lru_cache
def get_execution_trace_repository():
    settings = get_settings()
    if settings.storage_backend == "database":
        return DatabaseExecutionTraceRepository(create_session_factory(settings))
    return MemoryExecutionTraceRepository()


@lru_cache
def get_execution_trace_service() -> ExecutionTraceService:
    memory_service = get_memory_service()
    return ExecutionTraceService(
        get_execution_trace_repository(),
        canonical_turns=get_turn_service(),
        memory_items=memory_service.repository,
        index_operations=memory_service.index_outbox,
    )


@lru_cache
def get_memory_index_trace_service() -> ExecutionTraceService:
    return ExecutionTraceService(
        get_execution_trace_repository(),
        canonical_turns=get_turn_service(),
    )


def get_run_repository():
    return get_repository_bundle()["runs"]


@lru_cache
def get_plan_service() -> PlanService:
    repositories = get_repository_bundle()
    return PlanService(
        repositories["plans"],
        structured_formation=get_structured_formation_publisher(),
        runtime_policy=get_memory_runtime_policy(),
    )


@lru_cache
def get_task_memory_plan_resolver() -> TaskMemoryPlanResolver:
    return TaskMemoryPlanResolver(
        memory_service=get_memory_service(),
        plan_service=get_plan_service(),
    )


def get_plan_executor() -> PlanExecutor:
    return PlanExecutor(
        plan_service=get_plan_service(),
        registry=get_registry_service(),
        invocation_service=get_invocation_service(),
    )
