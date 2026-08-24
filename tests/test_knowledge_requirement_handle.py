import asyncio
import time
from datetime import UTC, datetime, timedelta
from threading import enumerate as enumerate_threads

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import InvocationError
from app.plugins.knowledge import KnowledgeProvider
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory import (
    MemoryAgentDefinitionRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.schemas.agent_context import (
    AgentKnowledgeContextSpec,
    KnowledgeCitation,
    KnowledgeContext,
    KnowledgeContextItem,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.common import UserContext
from app.schemas.invocation import InvokeRequest
from app.schemas.knowledge_provider import (
    KnowledgeProviderRequest,
    KnowledgeProviderResult,
)
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.invocation_service import InvocationService
from app.services.knowledge_context_handle import (
    KnowledgeContextHandleError,
    KnowledgeContextHandleService,
)
from app.services.memory_service import MemoryService
from app.services.registry_service import AgentRegistryService
from tests.support.v2_runtime import V2TestAdapter, attach_v2_runtime


class StubKnowledgeProvider(KnowledgeProvider):
    def __init__(
        self,
        *,
        result: KnowledgeProviderResult | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.result = result or KnowledgeProviderResult(status="empty", trace_id="ktrace-empty")
        self.failure = failure
        self.requests: list[KnowledgeProviderRequest] = []

    async def retrieve(self, request: KnowledgeProviderRequest) -> KnowledgeProviderResult:
        self.requests.append(request)
        if self.failure:
            raise self.failure
        return self.result


class SlowKnowledgeProvider(StubKnowledgeProvider):
    async def retrieve(self, request: KnowledgeProviderRequest) -> KnowledgeProviderResult:
        self.requests.append(request)
        await asyncio.sleep(1)
        return self.result


def _agent(base: AgentDefinitionV2, knowledge: dict) -> AgentDefinitionV2:
    payload = base.model_dump(mode="json")
    payload["context"] = {"knowledge": knowledge}
    return AgentDefinitionV2.model_validate(payload)


def _service(
    provider: KnowledgeProvider | None,
    *,
    handle_service: KnowledgeContextHandleService | None = None,
) -> AgentContextAssemblyService:
    settings = Settings(storage_backend="memory", memory_mode="off")
    return AgentContextAssemblyService(
        settings=settings,
        memory_service=MemoryService(
            settings=settings,
            repository=MemoryItemRepository(),
        ),
        knowledge_provider=provider,
        knowledge_context_handle_service=handle_service,
    )


def _knowledge_context() -> KnowledgeContext:
    citation = KnowledgeCitation(source_id="policy_docs")
    return KnowledgeContext(
        summary="- Refunds are available within 30 days.",
        status="ok",
        items=[
            KnowledgeContextItem(
                item_id="refund-policy",
                source_id="policy_docs",
                content="Refunds are available within 30 days.",
                score=0.97,
                citation=citation,
            )
        ],
        citations=[citation],
        source_ids=["policy_docs"],
        metadata={"trace_id": "provider-trace"},
    )


def test_knowledge_requirement_defaults_optional_and_controlled_alias_is_canonical() -> None:
    default = AgentKnowledgeContextSpec()
    controlled = AgentKnowledgeContextSpec.model_validate(
        {
            "mode": "controlled",
            "controlled_retrieval": {"query_template": "{{ input.text }}"},
        }
    )

    assert default.requirement == "optional"
    assert controlled.mode == "controlled_retrieval"


def test_disabled_required_knowledge_config_is_rejected() -> None:
    with pytest.raises(ValidationError, match="disabled knowledge mode cannot be required"):
        AgentKnowledgeContextSpec(mode="disabled", requirement="required")


@pytest.mark.parametrize(
    ("provider", "expected_code"),
    [
        (None, "knowledge_unavailable"),
        (
            StubKnowledgeProvider(failure=RuntimeError("provider down")),
            "knowledge_unavailable",
        ),
        (
            StubKnowledgeProvider(
                result=KnowledgeProviderResult(status="empty", trace_id="ktrace-empty")
            ),
            "knowledge_not_found",
        ),
    ],
)
async def test_required_prefetch_blocks_when_knowledge_is_not_usable(
    summarizer_agent,
    provider,
    expected_code,
) -> None:
    service = _service(provider)
    agent = _agent(
        summarizer_agent,
        {
            "mode": "prefetch",
            "requirement": "required",
            "source_ids": ["policy_docs"],
        },
    )

    with pytest.raises(InvocationError) as error:
        await service.assemble(
            agent=agent,
            user=UserContext(id="user-1", attributes={"tenant_id": "tenant-1"}),
            session_id="session-1",
            query="refund policy",
            invocation_input={"text": "refund policy"},
        )

    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("provider", "expected_status"),
    [
        (None, "disabled"),
        (StubKnowledgeProvider(failure=RuntimeError("provider down")), "error"),
        (
            StubKnowledgeProvider(
                result=KnowledgeProviderResult(status="empty", trace_id="ktrace-empty")
            ),
            "empty",
        ),
    ],
)
async def test_optional_prefetch_continues_with_observable_status(
    summarizer_agent,
    provider,
    expected_status,
) -> None:
    runtime = await _service(provider).assemble(
        agent=_agent(
            summarizer_agent,
            {
                "mode": "prefetch",
                "source_ids": ["policy_docs"],
            },
        ),
        user=UserContext(id="user-1", attributes={"tenant_id": "tenant-1"}),
        session_id="session-1",
        query="refund policy",
        invocation_input={"text": "refund policy"},
    )

    assert runtime.knowledge_context.status == expected_status
    if expected_status in {"disabled", "error"}:
        assert runtime.knowledge_context.errors == ["knowledge_unavailable"]


def test_handle_rejects_expiry_cross_subject_scope_trace_and_replay() -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    current = [now]
    handles = KnowledgeContextHandleService(
        ttl_seconds=30,
        clock=lambda: current[0],
    )
    binding = {
        "tenant_id": "tenant-1",
        "principal_id": "user-1",
        "agent_id": "summarizer",
        "source_ids": ["policy_docs"],
        "source_tags": ["public"],
        "trace_id": "execution-trace",
    }

    forged = "kh_forged"
    with pytest.raises(KnowledgeContextHandleError, match="invalid"):
        handles.consume(forged, **binding)

    for changed in (
        {"tenant_id": "tenant-2"},
        {"principal_id": "user-2"},
        {"agent_id": "other-agent"},
        {"source_ids": ["other"]},
        {"source_tags": ["restricted"]},
        {"trace_id": "other-trace"},
    ):
        handle = handles.issue(_knowledge_context(), **binding)
        with pytest.raises(KnowledgeContextHandleError, match="binding"):
            handles.consume(handle, **{**binding, **changed})
        assert handles.active_handle_count == 0
        with pytest.raises(KnowledgeContextHandleError, match="invalid"):
            handles.consume(handle, **binding)

    expired = handles.issue(_knowledge_context(), **binding)
    current[0] = now + timedelta(seconds=31)
    with pytest.raises(KnowledgeContextHandleError, match="expired"):
        handles.consume(expired, **binding)
    assert handles.active_handle_count == 0

    current[0] = now
    one_time = handles.issue(_knowledge_context(), **binding)
    assert handles.consume(one_time, **binding).items[0].item_id == "refund-policy"
    assert handles.active_handle_count == 0
    with pytest.raises(KnowledgeContextHandleError, match="invalid"):
        handles.consume(one_time, **binding)


def test_unconsumed_handle_erases_body_when_ttl_elapses() -> None:
    handles = KnowledgeContextHandleService(ttl_seconds=0.01)
    binding = {
        "tenant_id": "tenant-1",
        "principal_id": "user-1",
        "agent_id": "summarizer",
        "source_ids": ["policy_docs"],
        "source_tags": ["public"],
        "trace_id": "execution-trace",
    }

    handle = handles.issue(_knowledge_context(), **binding)
    deadline = time.monotonic() + 0.5
    while handles.active_handle_count and time.monotonic() < deadline:
        time.sleep(0.005)

    assert handles.active_handle_count == 0
    with pytest.raises(KnowledgeContextHandleError, match="invalid"):
        handles.consume(handle, **binding)


async def test_controlled_required_consumes_trusted_handle_and_rejects_caller_context(
    summarizer_agent,
) -> None:
    handles = KnowledgeContextHandleService(ttl_seconds=30)
    service = _service(None, handle_service=handles)
    agent = _agent(
        summarizer_agent,
        {
            "mode": "controlled",
            "requirement": "required",
            "source_ids": ["policy_docs"],
            "source_tags": ["public"],
            "controlled_retrieval": {"query_template": "{{ input.text }}"},
        },
    )
    user = UserContext(id="user-1", attributes={"tenant_id": "tenant-1"})
    caller_context = _knowledge_context().model_dump(mode="json")

    with pytest.raises(InvocationError) as missing:
        await service.assemble(
            agent=agent,
            user=user,
            session_id="session-1",
            query="refund policy",
            invocation_input={"text": "refund policy", "knowledge_context": caller_context},
            knowledge_context_trace_id="execution-trace",
        )
    assert missing.value.code == "knowledge_unavailable"

    handle = handles.issue(
        _knowledge_context(),
        tenant_id="tenant-1",
        principal_id="user-1",
        agent_id=agent.agent_id,
        source_ids=["policy_docs"],
        source_tags=["public"],
        trace_id="execution-trace",
    )
    invocation_input = {"text": "refund policy", "knowledge_context": caller_context}
    runtime = await service.assemble(
        agent=agent,
        user=user,
        session_id="session-1",
        query="refund policy",
        invocation_input=invocation_input,
        knowledge_context_handle=handle,
        knowledge_context_trace_id="execution-trace",
    )

    assert runtime.knowledge_context.items[0].item_id == "refund-policy"
    assert invocation_input["knowledge_context"]["items"][0]["item_id"] == "refund-policy"


async def test_controlled_required_route_preview_defers_handle_to_invocation(
    summarizer_agent,
) -> None:
    handles = KnowledgeContextHandleService(ttl_seconds=30)
    service = _service(None, handle_service=handles)
    agent = _agent(
        summarizer_agent,
        {
            "mode": "controlled",
            "requirement": "required",
            "source_ids": ["policy_docs"],
            "controlled_retrieval": {"query_template": "{{ input.text }}"},
        },
    )

    runtime = await service.assemble(
        agent=agent,
        user=UserContext(id="user-1", attributes={"tenant_id": "tenant-1"}),
        session_id="session-1",
        query="refund policy",
        invocation_input={"text": "refund policy"},
        caller_type="router",
    )

    assert runtime.knowledge_context.status == "disabled"
    assert handles.active_handle_count == 0


async def test_controlled_retrieval_issues_bound_handle(summarizer_agent) -> None:
    context = _knowledge_context()
    provider = StubKnowledgeProvider(
        result=KnowledgeProviderResult(
            status="ok",
            items=context.items,
            citations=context.citations,
            trace_id="provider-trace",
        )
    )
    handles = KnowledgeContextHandleService(ttl_seconds=30)
    service = _service(provider, handle_service=handles)
    agent = _agent(
        summarizer_agent,
        {
            "mode": "controlled",
            "requirement": "required",
            "source_ids": ["policy_docs"],
            "controlled_retrieval": {
                "query_template": "{{ input.text }}",
                "allowed_variables": ["input.text"],
            },
        },
    )
    user = UserContext(id="user-1", attributes={"tenant_id": "tenant-1"})

    handle = await service.issue_controlled_knowledge_handle(
        agent=agent,
        user=user,
        variables={"input": {"text": "refund policy"}},
        trace_id="execution-trace",
    )
    runtime = await service.assemble(
        agent=agent,
        user=user,
        session_id="session-1",
        query="refund policy",
        invocation_input={"text": "refund policy"},
        knowledge_context_handle=handle,
        knowledge_context_trace_id="execution-trace",
    )

    assert provider.requests[0].query == "refund policy"
    assert runtime.knowledge_context.metadata["trace_id"] == "provider-trace"
    assert handles.active_handle_count == 0


async def test_prefetch_and_controlled_retrieval_use_configurable_twelve_second_budget(
    summarizer_agent,
    monkeypatch,
) -> None:
    provider = StubKnowledgeProvider()
    settings = Settings(storage_backend="memory", memory_mode="off")
    assert settings.knowledge_prefetch_timeout_seconds == 12.0
    service = _service(provider)
    user = UserContext(id="user-1", attributes={"tenant_id": "tenant-1"})

    await service.assemble(
        agent=_agent(
            summarizer_agent,
            {"mode": "prefetch", "source_ids": ["policy_docs"]},
        ),
        user=user,
        session_id="session-1",
        query="refund policy",
        invocation_input={"text": "refund policy"},
    )
    await service.controlled_knowledge_retrieval(
        agent=_agent(
            summarizer_agent,
            {
                "mode": "controlled",
                "source_ids": ["policy_docs"],
                "controlled_retrieval": {"query_template": "{{ input.text }}"},
            },
        ),
        user=user,
        variables={"input": {"text": "refund policy"}},
    )

    assert [request.budget.timeout_seconds for request in provider.requests] == [12.0, 12.0]
    monkeypatch.setenv("KNOWLEDGE_PREFETCH_TIMEOUT_SECONDS", "7.5")
    assert Settings(_env_file=None).knowledge_prefetch_timeout_seconds == 7.5


async def test_controlled_retrieval_enforces_configured_outer_total_budget(
    summarizer_agent,
) -> None:
    provider = SlowKnowledgeProvider()
    settings = Settings(
        storage_backend="memory",
        memory_mode="off",
        knowledge_prefetch_timeout_seconds=0.01,
    )
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=MemoryService(
            settings=settings,
            repository=MemoryItemRepository(),
        ),
        knowledge_provider=provider,
    )
    user = UserContext(id="user-1", attributes={"tenant_id": "tenant-1"})
    prefetch = await service.assemble(
        agent=_agent(
            summarizer_agent,
            {"mode": "prefetch", "source_ids": ["policy_docs"]},
        ),
        user=user,
        session_id="session-1",
        query="refund policy",
        invocation_input={"text": "refund policy"},
    )

    context = await service.controlled_knowledge_retrieval(
        agent=_agent(
            summarizer_agent,
            {
                "mode": "controlled",
                "source_ids": ["policy_docs"],
                "controlled_retrieval": {"query_template": "{{ input.text }}"},
            },
        ),
        user=user,
        variables={"input": {"text": "refund policy"}},
    )

    assert prefetch.knowledge_context.status == "timeout"
    assert context.status == "error"
    assert context.errors == ["knowledge_unavailable"]
    assert [request.budget.timeout_seconds for request in provider.requests] == [0.01, 0.01]


def test_many_handles_share_one_scheduler_and_close_erases_all_bodies() -> None:
    prefix = "knowledge-context-handle-expiry"
    before = {thread.ident for thread in enumerate_threads() if thread.name.startswith(prefix)}
    handles = KnowledgeContextHandleService(ttl_seconds=30)
    binding = {
        "tenant_id": "tenant-1",
        "principal_id": "user-1",
        "agent_id": "summarizer",
        "source_ids": ["policy_docs"],
        "source_tags": ["public"],
        "trace_id": "execution-trace",
    }

    issued = [handles.issue(_knowledge_context(), **binding) for _ in range(200)]
    scheduler_threads = [
        thread
        for thread in enumerate_threads()
        if thread.name.startswith(prefix) and thread.ident not in before
    ]

    assert len(scheduler_threads) == 1
    assert handles.active_handle_count == len(issued)
    handles.close()
    assert handles.active_handle_count == 0
    assert not scheduler_threads[0].is_alive()
    with pytest.raises(KnowledgeContextHandleError, match="invalid"):
        handles.consume(issued[0], **binding)


async def test_required_failure_prevents_invoker_call(summarizer_agent) -> None:
    agent = _agent(
        summarizer_agent,
        {
            "mode": "prefetch",
            "requirement": "required",
            "source_ids": ["policy_docs"],
        },
    )
    repository = MemoryAgentDefinitionRepository()
    await repository.upsert(agent)
    registry = AgentRegistryService(
        settings=Settings(
            storage_backend="memory",
            registry_backend="database",
            memory_mode="off",
        ),
        repository=repository,
    )
    await registry.load()
    adapter = V2TestAdapter()
    catalog = await attach_v2_runtime(registry, adapter=adapter)
    service = InvocationService(
        registry=registry,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        agent_context_service=_service(None),
    )

    with pytest.raises(InvocationError) as error:
        await service.invoke(
            InvokeRequest(
                agent_id=agent.agent_id,
                session_id="session-1",
                user=UserContext(
                    id="user-1",
                    roles=["operator"],
                    attributes={"tenant_id": "tenant-1"},
                ),
                input={"text": "refund policy"},
            )
        )

    assert error.value.code == "knowledge_unavailable"
    assert adapter.invocations == []
    await catalog.aclose()


async def test_public_invoke_consumes_controlled_handle_before_invoker(
    summarizer_agent,
) -> None:
    agent = _agent(
        summarizer_agent,
        {
            "mode": "controlled",
            "requirement": "required",
            "source_ids": ["policy_docs"],
            "source_tags": ["public"],
            "controlled_retrieval": {"query_template": "{{ input.text }}"},
        },
    )
    repository = MemoryAgentDefinitionRepository()
    await repository.upsert(agent)
    registry = AgentRegistryService(
        settings=Settings(
            storage_backend="memory",
            registry_backend="database",
            memory_mode="off",
        ),
        repository=repository,
    )
    await registry.load()
    adapter = V2TestAdapter()
    catalog = await attach_v2_runtime(registry, adapter=adapter)
    handles = KnowledgeContextHandleService(ttl_seconds=30)
    context = _knowledge_context()
    provider = StubKnowledgeProvider(
        result=KnowledgeProviderResult(
            status="ok",
            items=context.items,
            citations=context.citations,
            trace_id="provider-trace",
        )
    )
    service = InvocationService(
        registry=registry,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        agent_context_service=_service(provider, handle_service=handles),
    )
    handle = await service.issue_controlled_knowledge_context_handle(
        agent_id=agent.agent_id,
        user=UserContext(
            id="user-1",
            roles=["operator"],
            attributes={"tenant_id": "tenant-1"},
        ),
        variables={"input": {"text": "refund policy"}},
        trace_id="execution-trace",
    )

    result = await service.invoke(
        InvokeRequest(
            session_id="session-1",
            agent_id=agent.agent_id,
            user=UserContext(
                id="user-1",
                roles=["operator"],
                attributes={"tenant_id": "tenant-1"},
            ),
            input={"text": "refund policy"},
            knowledge_context_handle=handle,
            knowledge_context_trace_id="execution-trace",
        )
    )

    assert result.status == "completed"
    assert adapter.invocations[0].knowledge_context.items[0].item_id == "refund-policy"
    assert adapter.invocations[0].knowledge_context_handle is None
    assert adapter.invocations[0].knowledge_context_trace_id is None
    assert handles.active_handle_count == 0
    await catalog.aclose()
