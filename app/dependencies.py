"""FastAPI providers for the current application's preassembled Container."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request

from app.core.config import Settings
from app.core.errors import (
    ApplicationRuntimeUnavailable,
    RegistryUnavailableError,
    RuntimeCatalogUnavailableError,
)
from app.core.memory_runtime import MemoryRuntimePolicy, build_memory_runtime_policy
from app.runtime.application import ApplicationContainer, ApplicationRuntimeView
from app.runtime.catalog import RuntimeCatalog
from app.runtime.services import (
    ApplicationServices,
    build_knowledge_provider,
    resolve_memory_data_settings,
)
from app.services.agent_event_service import NativeAgentEventService
from app.services.chat_history_service import ChatHistoryService
from app.services.delegated_run_service import DelegatedRunService
from app.services.execution_ticket_service import ExecutionTicketService
from app.services.execution_trace_service import ExecutionTraceService
from app.services.invocation_service import InvocationService
from app.services.memory_governance import MemoryGovernanceService
from app.services.memory_management import MemoryManagementService
from app.services.memory_observability import MemoryObservabilityService
from app.services.memory_service import MemoryService
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService
from app.services.registry_service import AgentRegistryService
from app.services.registry_snapshot import RegistrySnapshotRuntime
from app.services.router_service import RouterService
from app.services.turn_service import TurnService


def get_application_container(request: Request) -> ApplicationContainer:
    view = getattr(request.app.state, "application_runtime_view", None)
    if not isinstance(view, ApplicationRuntimeView):
        raise ApplicationRuntimeUnavailable("Application Runtime is unavailable")
    return view.require_container()


def get_application_services(request: Request) -> ApplicationServices:
    services = get_application_container(request).services
    if services is None:
        raise ApplicationRuntimeUnavailable("Application Runtime is unavailable")
    return services


def get_registry_service(request: Request) -> AgentRegistryService:
    return get_application_container(request).registry


def get_repository_bundle(request: Request) -> Mapping[str, object]:
    return get_application_services(request).repository_bundle


def get_memory_service(request: Request) -> MemoryService:
    return get_application_services(request).memory_service


def get_memory_formation_repository(request: Request):
    return get_application_services(request).memory_formation_repository


def get_memory_formation_runtime_status(request: Request):
    return get_application_services(request).memory_formation_runtime_status


def get_memory_maintenance_runtime_status(request: Request):
    return get_application_services(request).memory_maintenance_runtime_status


def get_memory_observability_service(request: Request) -> MemoryObservabilityService:
    return get_application_services(request).memory_observability_service


def get_memory_governance_service(request: Request) -> MemoryGovernanceService:
    return get_application_services(request).memory_governance_service


def get_memory_management_service(request: Request) -> MemoryManagementService:
    return get_application_services(request).memory_management_service


def get_memory_runtime_policy(request: Request) -> MemoryRuntimePolicy:
    return get_application_services(request).memory_runtime_policy


def get_turn_repository(request: Request):
    return get_application_services(request).turn_repository


def get_turn_service(request: Request) -> TurnService:
    return get_application_services(request).turn_service


def get_turn_outbox_repository(request: Request):
    return get_application_services(request).turn_outbox_repository


def get_delegated_run_service(request: Request) -> DelegatedRunService:
    return get_application_services(request).delegated_run_service


def get_execution_ticket_service(request: Request) -> ExecutionTicketService:
    return get_application_services(request).execution_ticket_service


def get_external_execution_acceptance_store(request: Request):
    return get_application_services(request).external_execution_acceptance_store


def get_native_agent_event_service(request: Request) -> NativeAgentEventService:
    return get_application_services(request).native_agent_event_service


async def get_runtime_catalog(request: Request) -> RuntimeCatalog:
    try:
        return get_application_container(request).runtime_catalog
    except ApplicationRuntimeUnavailable as exc:
        raise RuntimeCatalogUnavailableError("Runtime Catalog is unavailable") from exc


def get_registry_snapshot_runtime(request: Request) -> RegistrySnapshotRuntime | None:
    runtime = get_application_container(request).registry_snapshot_runtime
    if runtime is not None and not isinstance(runtime, RegistrySnapshotRuntime):
        raise RegistryUnavailableError("Registry Snapshot Runtime is unavailable")
    return runtime


async def get_router_service(request: Request) -> RouterService:
    return get_application_services(request).router_service


async def get_invocation_service(request: Request) -> InvocationService:
    return get_application_services(request).invocation_service


def get_chat_history_service(request: Request) -> ChatHistoryService:
    return get_application_services(request).chat_history_service


def get_event_service(request: Request):
    return get_application_services(request).event_service


def get_execution_trace_repository(request: Request):
    return get_application_services(request).execution_trace_repository


def get_execution_trace_service(request: Request) -> ExecutionTraceService:
    return get_application_services(request).execution_trace_service


def get_memory_index_trace_service(request: Request) -> ExecutionTraceService:
    return get_application_services(request).memory_index_trace_service


def get_run_repository(request: Request):
    return get_application_services(request).repository_bundle["runs"]


def get_plan_service(request: Request) -> PlanService:
    return get_application_services(request).plan_service


async def get_plan_executor(request: Request) -> PlanExecutor:
    return get_application_services(request).plan_executor


def _memory_data_settings(
    settings: Settings,
    policy: MemoryRuntimePolicy | None = None,
    *,
    database_url: str | None = None,
    collection: str | None = None,
) -> Settings:
    return resolve_memory_data_settings(
        settings,
        policy or build_memory_runtime_policy(settings.memory_mode),
        database_url=database_url,
        collection=collection,
    )


def get_memory_data_settings(settings: Settings) -> Settings:
    """Compatibility helper for explicit configuration-only tests."""

    return _memory_data_settings(settings)


def get_structured_formation_publisher() -> None:
    return None


def get_turn_capture_service() -> None:
    return None


__all__ = [
    "build_knowledge_provider",
    "get_application_container",
    "get_application_services",
    "get_chat_history_service",
    "get_delegated_run_service",
    "get_event_service",
    "get_execution_ticket_service",
    "get_execution_trace_repository",
    "get_execution_trace_service",
    "get_external_execution_acceptance_store",
    "get_invocation_service",
    "get_memory_data_settings",
    "get_memory_formation_repository",
    "get_memory_formation_runtime_status",
    "get_memory_governance_service",
    "get_memory_index_trace_service",
    "get_memory_maintenance_runtime_status",
    "get_memory_management_service",
    "get_memory_observability_service",
    "get_memory_runtime_policy",
    "get_memory_service",
    "get_native_agent_event_service",
    "get_plan_executor",
    "get_plan_service",
    "get_registry_service",
    "get_registry_snapshot_runtime",
    "get_repository_bundle",
    "get_router_service",
    "get_run_repository",
    "get_runtime_catalog",
    "get_structured_formation_publisher",
    "get_turn_capture_service",
    "get_turn_outbox_repository",
    "get_turn_repository",
    "get_turn_service",
    "_memory_data_settings",
]
