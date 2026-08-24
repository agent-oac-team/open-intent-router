"""Build the complete production service graph for one Application Runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import uuid4

from app.adapters.knowledge_sys import KnowledgeSysHttpProvider, load_signing_private_key
from app.application import (
    ExternalExecutionAcceptanceApplicationPort,
    ExternalExecutorApplicationPort,
)
from app.core.config import Settings
from app.core.memory_runtime import MemoryRuntimePolicy, build_memory_runtime_policy
from app.db.managed import ManagedDatabase
from app.llm.conversation_formation import OpenAICompatibleConversationFormationModel
from app.plugins.evidence import build_evidence_provider
from app.plugins.knowledge import KnowledgeProvider
from app.repositories.canonical_invocations import (
    DatabaseCanonicalInvocationStore,
    MemoryCanonicalInvocationStore,
)
from app.repositories.context_stores import (
    DatabaseMemoryItemRepository,
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
    DatabaseDelegatedRunCancelStore,
    DatabaseDelegatedRunCompletionStore,
    DatabaseDelegatedRunFailureStore,
    DatabaseDelegatedRunMaintenanceStore,
    DatabaseDelegatedRunProgressStore,
    DatabaseDelegatedRunStartStore,
    MemoryDelegatedRunCancelStore,
    MemoryDelegatedRunCompletionStore,
    MemoryDelegatedRunFailureStore,
    MemoryDelegatedRunMaintenanceStore,
    MemoryDelegatedRunProgressStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.execution_tickets import (
    DatabaseExecutionTicketStore,
    MemoryExecutionTicketStore,
)
from app.repositories.execution_traces import (
    DatabaseExecutionTraceRepository,
    MemoryExecutionTraceRepository,
)
from app.repositories.external_execution_acceptances import (
    DatabaseExternalExecutionAcceptanceStore,
    MemoryExternalExecutionAcceptanceStore,
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
from app.runtime.application import ApplicationComposition, ApplicationContainer
from app.runtime.catalog import RuntimeCatalog
from app.runtime.invocation import InvocationRuntime
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.agent_event_service import NativeAgentEventService
from app.services.binding_resolution import BindingResolver
from app.services.chat_history_service import ChatHistoryService
from app.services.context_service import ContextService
from app.services.delegated_run_service import DelegatedRunService
from app.services.delegated_run_timeout_runtime import DelegatedRunTimeoutRuntime
from app.services.event_service import EventService
from app.services.execution_ticket_service import ExecutionTicketService
from app.services.execution_trace_service import ExecutionTraceService
from app.services.external_execution_service import ExternalExecutionService
from app.services.invocation_service import InvocationService
from app.services.knowledge_context_handle import KnowledgeContextHandleService
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
    TurnOutboxFormationConsumer,
    UnavailableFormationJobProcessor,
)
from app.services.memory_governance import MemoryGovernanceService
from app.services.memory_integration import MemoryFormationProcessor
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
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime
from app.services.router_service import RouterService
from app.services.task_continuation import TaskMemoryPlanResolver
from app.services.turn_service import TurnService


@dataclass(frozen=True, slots=True)
class _RouterDependencies:
    settings: Settings = field(repr=False)
    registry: AgentRegistryService
    repositories: Mapping[str, object]
    memory_service: MemoryService
    runtime_policy: MemoryRuntimePolicy
    chat_history: ChatHistoryService
    event_service: EventService
    plan_service: PlanService
    agent_context: AgentContextAssemblyService
    plan_resolver: TaskMemoryPlanResolver
    turn_service: TurnService
    catalog: RuntimeCatalog


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    """Preassembled production capabilities with no Engine or Session Factory."""

    repository_bundle: Mapping[str, object]
    context_repository_bundle: Mapping[str, object]
    memory_runtime_policy: MemoryRuntimePolicy
    memory_service: MemoryService
    memory_formation_repository: object
    memory_trace_repository: object
    memory_formation_runtime_status: MemoryFormationRuntimeStatus
    memory_maintenance_runtime_status: MemoryMaintenanceRuntimeStatus
    memory_observability_service: MemoryObservabilityService
    memory_governance_service: MemoryGovernanceService
    memory_management_service: MemoryManagementService
    knowledge_context_handle_service: KnowledgeContextHandleService
    knowledge_provider: KnowledgeProvider | None
    agent_context_service: AgentContextAssemblyService
    router_service: RouterService
    invocation_runtime: InvocationRuntime
    invocation_service: InvocationService
    plan_service: PlanService
    plan_executor: PlanExecutor
    turn_repository: object
    turn_service: TurnService
    turn_outbox_repository: object
    delegated_run_service: DelegatedRunService
    execution_ticket_service: ExecutionTicketService
    external_execution_acceptance_store: object
    native_agent_event_service: NativeAgentEventService
    canonical_invocation_store: object
    chat_history_service: ChatHistoryService
    event_service: EventService
    execution_trace_repository: object
    execution_trace_service: ExecutionTraceService
    memory_index_trace_service: ExecutionTraceService
    task_memory_plan_resolver: TaskMemoryPlanResolver
    external_executor: ExternalExecutorApplicationPort | None
    external_execution_service: ExternalExecutionService | None
    _router_dependencies: _RouterDependencies = field(repr=False)

    def router_for_snapshot(self, snapshot_runtime: RegistrySnapshotRuntime) -> RouterService:
        """Build the bounded Router capability needed by trusted snapshot routing."""

        return _build_router_service(self._router_dependencies, snapshot_runtime)


ExternalExecutorFactory = Callable[
    [ExternalExecutionAcceptanceApplicationPort], ExternalExecutorApplicationPort
]


def build_application_service_composition(
    *,
    settings: Settings,
    external_executor: ExternalExecutorApplicationPort | None = None,
    external_executor_factory: ExternalExecutorFactory | None = None,
    memory_runtime_policy: MemoryRuntimePolicy | None = None,
    memory_data_settings: Settings | None = None,
    execution_ticket_secret: str | None = None,
    external_execution_ticket_ttl_seconds: int = 900,
) -> ApplicationComposition:
    """Select the complete production graph and its owned database targets."""

    runtime_policy = memory_runtime_policy or build_memory_runtime_policy(settings.memory_mode)
    selected_memory_settings = memory_data_settings or resolve_memory_data_settings(
        settings, runtime_policy
    )
    return ApplicationComposition(
        required_database_targets=_required_database_targets_for_full_graph(),
        memory_database_settings=selected_memory_settings,
        container_builder=lambda catalog, databases: build_application_container(
            settings=settings,
            catalog=catalog,
            databases=databases,
            external_executor=external_executor,
            external_executor_factory=external_executor_factory,
            memory_runtime_policy=runtime_policy,
            memory_data_settings=selected_memory_settings,
            execution_ticket_secret=execution_ticket_secret,
            external_execution_ticket_ttl_seconds=external_execution_ticket_ttl_seconds,
        ),
    )


def _required_database_targets_for_full_graph() -> frozenset[str]:
    """Derive physical targets from the selected full-service graph.

    The full Core graph always selects Registry/turn/plan services (``core``),
    plus MemoryService and its formation and maintenance runtimes (``memory``).
    Memory ``off`` only changes their policy; it does not remove those consumers.
    """

    consumers_by_target = {
        "core": ("registry", "turns", "plans", "runs", "execution_tickets"),
        "memory": ("memory_service", "formation_runtime", "maintenance_runtime"),
    }
    return frozenset(target for target, consumers in consumers_by_target.items() if consumers)


def resolve_memory_data_settings(
    settings: Settings,
    runtime_policy: MemoryRuntimePolicy,
    *,
    database_url: str | None = None,
    collection: str | None = None,
) -> Settings:
    """Derive the selected Memory target without consulting process globals."""

    if runtime_policy.execution_plane == "state_rehearsal":
        resolved_database_url = database_url
        if not resolved_database_url:
            raise ValueError("State Rehearsal requires an isolated Memory database URL")
        if resolved_database_url == settings.database_url:
            raise ValueError("State Rehearsal Memory database must differ from canonical database")
        if not collection or collection == settings.memory_milvus_collection:
            raise ValueError("State Rehearsal Memory collection must be isolated")
        return settings.model_copy(
            update={
                "database_url": resolved_database_url,
                "memory_milvus_collection": collection,
                "memory_mem0_history_database_url": resolved_database_url,
            }
        )
    memory_url = settings.effective_memory_database_url
    if memory_url and memory_url != settings.database_url:
        return settings.model_copy(update={"database_url": memory_url})
    return settings


def build_application_container(
    *,
    settings: Settings,
    catalog: RuntimeCatalog,
    databases: Mapping[str, ManagedDatabase],
    external_executor: ExternalExecutorApplicationPort | None = None,
    external_executor_factory: ExternalExecutorFactory | None = None,
    memory_runtime_policy: MemoryRuntimePolicy | None = None,
    memory_data_settings: Settings | None = None,
    execution_ticket_secret: str | None = None,
    external_execution_ticket_ttl_seconds: int = 900,
    external_execution_acceptance_store: ExternalExecutionAcceptanceApplicationPort | None = None,
) -> ApplicationContainer:
    """Assemble every production Core capability for exactly one lifespan."""

    runtime_policy = memory_runtime_policy or build_memory_runtime_policy(settings.memory_mode)
    selected_memory_settings = memory_data_settings or resolve_memory_data_settings(
        settings, runtime_policy
    )
    if external_executor is not None and external_executor_factory is not None:
        raise ValueError("Specify either an External Executor or its factory, not both")
    is_database = settings.storage_backend == "database"
    core_factory = databases["core"].session_factory if is_database else None
    memory_factory = databases["memory"].session_factory if is_database else None
    acceptance_store = external_execution_acceptance_store or (
        DatabaseExternalExecutionAcceptanceStore(core_factory)
        if is_database
        else MemoryExternalExecutionAcceptanceStore()
    )
    selected_external_executor = (
        external_executor_factory(acceptance_store)
        if external_executor_factory is not None
        else external_executor
    )

    repository_bundle = (
        _repository_bundle(core_factory) if is_database else _memory_repository_bundle()
    )
    context_repository_bundle = (
        MappingProxyType({"memory_items": DatabaseMemoryItemRepository(memory_factory)})
        if is_database
        else MappingProxyType({"memory_items": MemoryItemRepository()})
    )
    registry = AgentRegistryService(
        settings=settings,
        repository=(
            DatabaseAgentDefinitionRepository(core_factory)
            if is_database
            else MemoryAgentDefinitionRepository()
        ),
        file_source=FileRegistrySource(settings.registry_file_path),
    )

    formation_repository = (
        DatabaseMemoryFormationTurnJobRepository(memory_factory)
        if is_database
        else MemoryFormationTurnJobRepository()
    )
    turn_repository = (
        DatabaseTurnRepository(core_factory) if is_database else MemoryTurnRepository()
    )
    turn_outbox_repository = (
        DatabaseTurnOutboxRepository(core_factory) if is_database else MemoryTurnOutboxRepository()
    )
    turn_service = TurnService(
        turn_repository,
        route_completion_store=(
            DatabaseRouteTurnCompletionStore(core_factory)
            if is_database
            else MemoryRouteTurnCompletionStore(
                turn_repository=turn_repository,
                outbox_repository=turn_outbox_repository,
            )
        ),
    )
    execution_trace_repository = (
        DatabaseExecutionTraceRepository(core_factory)
        if is_database
        else MemoryExecutionTraceRepository()
    )
    memory_index_trace_service = ExecutionTraceService(
        execution_trace_repository,
        canonical_turns=turn_service,
    )
    memory_service = MemoryService(
        selected_memory_settings,
        repository=context_repository_bundle["memory_items"],
        formation_repository=formation_repository,
        runtime_policy=runtime_policy,
        execution_traces=memory_index_trace_service,
        turns=turn_service,
    )
    memory_trace_repository = (
        DatabaseMemoryFormationTraceRepository(memory_factory)
        if is_database
        else MemoryFormationTraceRepository(
            formation_repository=formation_repository,
            event_repository=memory_service.repository,
        )
    )
    execution_trace_service = ExecutionTraceService(
        execution_trace_repository,
        canonical_turns=turn_service,
        memory_items=memory_service.repository,
        index_operations=memory_service.index_outbox,
    )
    formation_status = MemoryFormationRuntimeStatus()
    maintenance_status = MemoryMaintenanceRuntimeStatus()
    memory_observability = MemoryObservabilityService(
        settings=settings,
        memory_service=memory_service,
        formation_repository=formation_repository,
        trace_repository=memory_trace_repository,
        runtime_status=formation_status,
        maintenance_status=maintenance_status,
        turn_repository=turn_repository,
        outbox_repository=turn_outbox_repository,
        runtime_policy=runtime_policy,
    )
    memory_governance = MemoryGovernanceService(memory_service=memory_service)
    memory_management = MemoryManagementService(
        memory_service=memory_service,
        execution_traces=execution_trace_service,
    )

    plan_service = PlanService(
        repository_bundle["plans"],
        run_repository=repository_bundle["runs"],
        structured_formation=None,
        runtime_policy=runtime_policy,
    )
    task_memory_plan_resolver = TaskMemoryPlanResolver(
        memory_service=memory_service,
        plan_service=plan_service,
    )
    knowledge_provider = build_knowledge_provider(settings)
    knowledge_context_handle = KnowledgeContextHandleService(
        ttl_seconds=settings.knowledge_context_handle_ttl_seconds
    )
    agent_context = AgentContextAssemblyService(
        settings=settings,
        memory_service=memory_service,
        knowledge_provider=knowledge_provider,
        runtime_policy=runtime_policy,
        knowledge_context_handle_service=knowledge_context_handle,
    )
    chat_history = ChatHistoryService(
        repository_bundle["messages"],
        host_limit=settings.router_max_host_history_messages,
        agent_limit=settings.router_max_agent_history_messages,
    )
    event_service = EventService(repository_bundle["events"])
    router_dependencies = _RouterDependencies(
        settings=settings,
        registry=registry,
        repositories=repository_bundle,
        memory_service=memory_service,
        runtime_policy=runtime_policy,
        chat_history=chat_history,
        event_service=event_service,
        plan_service=plan_service,
        agent_context=agent_context,
        plan_resolver=task_memory_plan_resolver,
        turn_service=turn_service,
        catalog=catalog,
    )
    snapshot_runtime = RegistrySnapshotRuntime(
        RegistrySnapshotBuilder(catalog, external_executor=selected_external_executor)
    )
    router_service = _build_router_service(router_dependencies, snapshot_runtime)
    canonical_invocation_store = (
        DatabaseCanonicalInvocationStore(core_factory)
        if is_database
        else MemoryCanonicalInvocationStore(
            run_repository=repository_bundle["runs"],
            result_repository=repository_bundle["results"],
            turn_repository=turn_repository,
            outbox_repository=turn_outbox_repository,
        )
    )
    invocation_runtime = InvocationRuntime()
    invocation_service = InvocationService(
        registry=registry,
        run_repository=repository_bundle["runs"],
        result_repository=repository_bundle["results"],
        agent_context_service=agent_context,
        plan_service=plan_service,
        turn_capture=None,
        structured_formation=None,
        memory_service=memory_service,
        canonical_invocation_store=canonical_invocation_store,
        runtime_policy=runtime_policy,
        memory_formation_policy_version=settings.memory_formation_policy_version,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
        execution_traces=execution_trace_service,
        invocation_runtime=invocation_runtime,
    )
    plan_executor = PlanExecutor(
        plan_service=plan_service,
        registry=registry,
        invocation_service=invocation_service,
        snapshot_runtime=snapshot_runtime,
    )
    delegated_runs = _delegated_run_service(
        is_database=is_database,
        factory=core_factory,
        repositories=repository_bundle,
        turn_repository=turn_repository,
        turn_outbox_repository=turn_outbox_repository,
    )
    execution_tickets = ExecutionTicketService(
        DatabaseExecutionTicketStore(core_factory) if is_database else MemoryExecutionTicketStore(),
        secret=(
            execution_ticket_secret
            if execution_ticket_secret is not None
            else settings.execution_ticket_secret
        ),
    )
    native_agent_events = NativeAgentEventService(
        tickets=execution_tickets,
        delegated_runs=delegated_runs,
        run_repository=repository_bundle["runs"],
        ticket_lease_seconds=settings.execution_ticket_lease_seconds,
    )
    external_execution = (
        ExternalExecutionService(
            external_executor=selected_external_executor,
            delegated_runs=delegated_runs,
            tickets=execution_tickets,
            ticket_ttl_seconds=external_execution_ticket_ttl_seconds,
        )
        if selected_external_executor is not None
        else None
    )
    formation_runtime = _build_formation_runtime(
        settings=settings,
        memory_settings=selected_memory_settings,
        runtime_policy=runtime_policy,
        formation_repository=formation_repository,
        memory_service=memory_service,
        turn_repository=turn_repository,
        turn_outbox_repository=turn_outbox_repository,
        execution_trace_service=execution_trace_service,
        status=formation_status,
    )
    maintenance_runtime = MemoryMaintenanceRuntime(
        settings=settings,
        memory_service=memory_service,
        status=maintenance_status,
        runtime_policy=runtime_policy,
    )
    timeout_runtime = DelegatedRunTimeoutRuntime(
        delegated_runs,
        interval_seconds=settings.delegated_run_timeout_interval_seconds,
        batch_size=settings.delegated_run_timeout_batch_size,
    )
    services = ApplicationServices(
        repository_bundle=repository_bundle,
        context_repository_bundle=context_repository_bundle,
        memory_runtime_policy=runtime_policy,
        memory_service=memory_service,
        memory_formation_repository=formation_repository,
        memory_trace_repository=memory_trace_repository,
        memory_formation_runtime_status=formation_status,
        memory_maintenance_runtime_status=maintenance_status,
        memory_observability_service=memory_observability,
        memory_governance_service=memory_governance,
        memory_management_service=memory_management,
        knowledge_context_handle_service=knowledge_context_handle,
        knowledge_provider=knowledge_provider,
        agent_context_service=agent_context,
        router_service=router_service,
        invocation_runtime=invocation_runtime,
        invocation_service=invocation_service,
        plan_service=plan_service,
        plan_executor=plan_executor,
        turn_repository=turn_repository,
        turn_service=turn_service,
        turn_outbox_repository=turn_outbox_repository,
        delegated_run_service=delegated_runs,
        execution_ticket_service=execution_tickets,
        external_execution_acceptance_store=acceptance_store,
        native_agent_event_service=native_agent_events,
        canonical_invocation_store=canonical_invocation_store,
        chat_history_service=chat_history,
        event_service=event_service,
        execution_trace_repository=execution_trace_repository,
        execution_trace_service=execution_trace_service,
        memory_index_trace_service=memory_index_trace_service,
        task_memory_plan_resolver=task_memory_plan_resolver,
        external_executor=selected_external_executor,
        external_execution_service=external_execution,
        _router_dependencies=router_dependencies,
    )
    return ApplicationContainer(
        registry=registry,
        runtime_catalog=catalog,
        registry_snapshot_runtime=snapshot_runtime,
        services=services,
        _background_runtimes=(formation_runtime, maintenance_runtime, timeout_runtime),
    )


def build_knowledge_provider(settings: Settings) -> KnowledgeProvider | None:
    if not settings.knowledge_provider_base_url:
        return None
    private_key = load_signing_private_key(
        pem=settings.knowledge_provider_jwt_private_key,
        file_path=settings.knowledge_provider_jwt_private_key_file,
    )
    return KnowledgeSysHttpProvider(
        base_url=settings.knowledge_provider_base_url,
        signing_private_key=private_key,
        signing_key_id=settings.knowledge_provider_jwt_key_id,
        issuer=settings.knowledge_provider_jwt_issuer,
        audience=settings.knowledge_provider_jwt_audience,
        deadline_seconds=settings.knowledge_provider_deadline_seconds,
        token_ttl_seconds=settings.knowledge_provider_jwt_ttl_seconds,
        circuit_window_seconds=settings.knowledge_provider_circuit_window_seconds,
        circuit_failure_threshold=settings.knowledge_provider_circuit_failure_threshold,
        circuit_open_seconds=settings.knowledge_provider_circuit_open_seconds,
    )


def _repository_bundle(factory) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "messages": DatabaseMessageRepository(factory),
            "events": DatabaseEventRepository(factory),
            "runs": DatabaseRunRepository(factory),
            "results": DatabaseResultRepository(factory),
            "plans": DatabasePlanRepository(factory),
            "route_logs": DatabaseRouteLogRepository(factory),
        }
    )


def _memory_repository_bundle() -> Mapping[str, object]:
    return MappingProxyType(
        {
            "messages": MemoryMessageRepository(),
            "events": MemoryEventRepository(),
            "runs": MemoryRunRepository(),
            "results": MemoryResultRepository(),
            "plans": MemoryPlanRepository(),
            "route_logs": MemoryRouteLogRepository(),
        }
    )


def _delegated_run_service(
    *,
    is_database: bool,
    factory,
    repositories: Mapping[str, object],
    turn_repository,
    turn_outbox_repository,
) -> DelegatedRunService:
    if is_database:
        return DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            progress_store=DatabaseDelegatedRunProgressStore(factory),
            completion_store=DatabaseDelegatedRunCompletionStore(factory),
            failure_store=DatabaseDelegatedRunFailureStore(factory),
            cancel_store=DatabaseDelegatedRunCancelStore(factory),
            maintenance_store=DatabaseDelegatedRunMaintenanceStore(factory),
        )
    return DelegatedRunService(
        MemoryDelegatedRunStartStore(
            run_repository=repositories["runs"],
            turn_repository=turn_repository,
            plan_repository=repositories["plans"],
        ),
        progress_store=MemoryDelegatedRunProgressStore(
            run_repository=repositories["runs"],
            event_repository=repositories["events"],
            plan_repository=repositories["plans"],
        ),
        completion_store=MemoryDelegatedRunCompletionStore(
            run_repository=repositories["runs"],
            result_repository=repositories["results"],
            event_repository=repositories["events"],
            turn_repository=turn_repository,
            outbox_repository=turn_outbox_repository,
            plan_repository=repositories["plans"],
        ),
        failure_store=MemoryDelegatedRunFailureStore(
            run_repository=repositories["runs"],
            event_repository=repositories["events"],
            turn_repository=turn_repository,
            outbox_repository=turn_outbox_repository,
            plan_repository=repositories["plans"],
        ),
        cancel_store=MemoryDelegatedRunCancelStore(
            run_repository=repositories["runs"],
            event_repository=repositories["events"],
            turn_repository=turn_repository,
            outbox_repository=turn_outbox_repository,
            plan_repository=repositories["plans"],
        ),
        maintenance_store=MemoryDelegatedRunMaintenanceStore(
            run_repository=repositories["runs"],
            event_repository=repositories["events"],
            turn_repository=turn_repository,
            outbox_repository=turn_outbox_repository,
            plan_repository=repositories["plans"],
        ),
    )


def _build_formation_runtime(
    *,
    settings: Settings,
    memory_settings: Settings,
    runtime_policy: MemoryRuntimePolicy,
    formation_repository,
    memory_service: MemoryService,
    turn_repository,
    turn_outbox_repository,
    execution_trace_service: ExecutionTraceService,
    status: MemoryFormationRuntimeStatus,
) -> MemoryFormationRuntime:
    if runtime_policy.effective_formation_mode == "off":
        runtime_repository = MemoryFormationTurnJobRepository()
        processor = UnavailableFormationJobProcessor()
    else:
        runtime_repository = formation_repository
        hard_rules = MemoryCandidateHardRules(repository=memory_service.repository)
        processor = MemoryFormationProcessor(
            repository=formation_repository,
            memory_repository=memory_service.repository,
            model=OpenAICompatibleConversationFormationModel(memory_settings),
            policy=MemoryCandidatePolicy(
                settings=memory_settings,
                repository=memory_service.repository,
                hard_rules=hard_rules,
                semantic_validator=MemoryCandidateSemanticValidator(),
                safety_filter=TemporaryLanguageSafetyFilter(),
                verifier=None,
            ),
            lifecycle=memory_service.lifecycle,
            execution_traces=execution_trace_service,
            turns=turn_repository,
        )
    reconciler = TurnOutboxFormationConsumer(
        settings=settings,
        outbox_repository=turn_outbox_repository,
        turn_repository=turn_repository,
        builder=TurnCapsuleBuilder(settings),
        coordinator=FormationTriggerCoordinator(
            settings=settings,
            repository=runtime_repository,
            runtime_policy=runtime_policy,
        ),
        event_repository=memory_service.repository,
        owner=f"turn-outbox-formation-{uuid4().hex}",
        runtime_policy=runtime_policy,
    )
    return MemoryFormationRuntime(
        settings=memory_settings,
        worker=FormationJobWorker(
            settings=settings,
            repository=runtime_repository,
            processor=processor,
            owner=f"formation-worker-{uuid4().hex}",
            runtime_policy=runtime_policy,
        ),
        sweeper=FormationIdleSweeper(
            settings=settings,
            repository=runtime_repository,
            runtime_policy=runtime_policy,
        ),
        reconciler=reconciler,
        status=status,
        runtime_policy=runtime_policy,
    )


def _build_router_service(
    dependencies: _RouterDependencies,
    snapshot_runtime: RegistrySnapshotRuntime,
) -> RouterService:
    return RouterService(
        settings=dependencies.settings,
        registry=dependencies.registry,
        context_service=ContextService(
            dependencies.settings,
            memory_service=dependencies.memory_service,
            runtime_policy=dependencies.runtime_policy,
        ),
        chat_history_service=dependencies.chat_history,
        result_repository=dependencies.repositories["results"],
        event_service=dependencies.event_service,
        route_log_repository=dependencies.repositories["route_logs"],
        evidence_provider=build_evidence_provider(dependencies.settings),
        plan_service=dependencies.plan_service,
        agent_context_service=dependencies.agent_context,
        plan_continuation_resolver=dependencies.plan_resolver,
        turn_service=dependencies.turn_service,
        runtime_policy=dependencies.runtime_policy,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(dependencies.catalog),
    )
