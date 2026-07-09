from functools import lru_cache

from app.core.config import get_settings
from app.db.session import create_session_factory
from app.plugins.evidence import build_evidence_provider
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
from app.repositories.file_registry import FileRegistrySource
from app.repositories.memory import (
    MemoryAgentDefinitionRepository,
    MemoryEventRepository,
    MemoryMessageRepository,
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRouteLogRepository,
    MemoryRunRepository,
)
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.chat_history_service import ChatHistoryService
from app.services.context_service import ContextService
from app.services.event_service import EventService
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.knowledge_service import KnowledgeService
from app.services.memory_service import MemoryService
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService
from app.services.registry_service import AgentRegistryService
from app.services.router_service import RouterService


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
        session_factory = create_session_factory(settings)
        return {
            "memory_items": DatabaseMemoryItemRepository(session_factory),
            "knowledge": DatabaseKnowledgeRepository(session_factory),
        }
    return {
        "memory_items": MemoryItemRepository(),
        "knowledge": KnowledgeRepository(),
    }


@lru_cache
def get_memory_service() -> MemoryService:
    settings = get_settings()
    repositories = get_context_repository_bundle()
    return MemoryService(settings=settings, repository=repositories["memory_items"])


@lru_cache
def get_knowledge_service() -> KnowledgeService:
    settings = get_settings()
    repositories = get_context_repository_bundle()
    return KnowledgeService(settings=settings, repository=repositories["knowledge"])


def get_agent_context_service() -> AgentContextAssemblyService:
    settings = get_settings()
    return AgentContextAssemblyService(
        settings=settings,
        memory_service=get_memory_service(),
        knowledge_service=get_knowledge_service(),
    )


def get_router_service() -> RouterService:
    settings = get_settings()
    repositories = get_repository_bundle()
    return RouterService(
        settings=settings,
        registry=get_registry_service(),
        context_service=ContextService(settings),
        chat_history_service=get_chat_history_service(),
        result_repository=repositories["results"],
        route_log_repository=repositories["route_logs"],
        evidence_provider=build_evidence_provider(settings),
        plan_service=PlanService(repositories["plans"]),
        agent_context_service=get_agent_context_service(),
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


def get_plan_service() -> PlanService:
    repositories = get_repository_bundle()
    return PlanService(repositories["plans"])


def get_plan_executor() -> PlanExecutor:
    return PlanExecutor(
        plan_service=get_plan_service(),
        registry=get_registry_service(),
        invocation_service=get_invocation_service(),
    )
