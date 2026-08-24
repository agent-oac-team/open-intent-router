import asyncio
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.repositories.database import DatabaseResultRepository, DatabaseRunRepository
from app.repositories.invocation_completion import (
    DatabaseInvocationCompletionStore,
    MemoryInvocationCompletionStore,
)
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalog,
)
from app.runtime.invocation import (
    AgentCallEnvelope,
    RawInvocationFailure,
    RawInvocationOutcome,
    RuntimeAdapterBinding,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.common import ArtifactRef
from app.schemas.invocation import InvokeRequest
from app.schemas.logs import AgentResult, AgentRun
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime

_FAILURE_CASES = (
    ("unavailable", "invocation_unavailable", True),
    ("deadline_exceeded", "invocation_deadline_exceeded", False),
    ("rejected", "invocation_rejected", False),
    ("remote_failure", "invocation_remote_failure", False),
    ("invalid_response", "invocation_invalid_response", False),
)


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


@dataclass
class _RuntimeOutcomeAdapter:
    response: object
    calls: list[tuple[RuntimeAdapterBinding, AgentCallEnvelope]] = field(default_factory=list)

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> object:
        self.calls.append((binding, envelope))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@dataclass
class _SessionScopeTracker:
    """Wrap a fixture session factory to make external-call scope observable."""

    factory: object
    active_scopes: int = 0
    raise_after_next_exit: bool = False
    raise_on_next_flush: bool = False

    def __call__(self):
        return _TrackedSession(self.factory(), self)


class _TrackedSession:
    def __init__(self, session: object, tracker: _SessionScopeTracker) -> None:
        self._session = session
        self._tracker = tracker

    async def __aenter__(self):
        await self._session.__aenter__()
        self._tracker.active_scopes += 1
        return self

    async def __aexit__(self, *args: object) -> object:
        try:
            outcome = await self._session.__aexit__(*args)
        finally:
            self._tracker.active_scopes -= 1
        if self._tracker.raise_after_next_exit:
            self._tracker.raise_after_next_exit = False
            raise RuntimeError("simulated completion acknowledgement loss")
        return outcome

    def __getattr__(self, name: str) -> object:
        return getattr(self._session, name)

    async def flush(self, *args: object, **kwargs: object) -> object:
        if self._tracker.raise_on_next_flush:
            self._tracker.raise_on_next_flush = False
            raise OperationalError("flush", {}, RuntimeError("simulated transient flush failure"))
        return await self._session.flush(*args, **kwargs)


class _FailOnceResultRepository(MemoryResultRepository):
    def __init__(self) -> None:
        super().__init__()
        self.failures = 0

    async def add_result(self, result: AgentResult) -> AgentResult:
        if self.failures == 0:
            self.failures += 1
            raise RuntimeError("simulated transient result persistence failure")
        return await super().add_result(result)


@dataclass
class _SessionScopeCheckingAdapter(_RuntimeOutcomeAdapter):
    tracker: _SessionScopeTracker | None = None
    observed_active_scopes: list[int] = field(default_factory=list)

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        connector: object,
        envelope: AgentCallEnvelope,
    ) -> object:
        assert self.tracker is not None
        self.observed_active_scopes.append(self.tracker.active_scopes)
        return await super().execute(binding, connector, envelope)


def _definition() -> AgentDefinitionV2:
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "runtime-failure-agent",
            "name": "Runtime Failure Agent",
            "description": "Exercises accepted Runtime failure convergence.",
            "revision": 1,
            "access_policy": {"allow_roles": ["operator"]},
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
            "handling": {
                "kind": "invocation",
                "adapter_key": "runtime_failure",
                "config": {"operation": "test"},
            },
        }
    )


def _request() -> InvokeRequest:
    return InvokeRequest.model_validate(
        {
            "request_id": "runtime-failure-request",
            "session_id": "runtime-failure-session",
            "agent_id": "runtime-failure-agent",
            "user": {
                "id": "operator-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1"},
            },
            "input": {"text": "run the accepted invocation"},
        }
    )


async def _runtime_service(
    *,
    adapter: _RuntimeOutcomeAdapter,
    runs,
    results,
) -> tuple[RuntimeCatalog, InvocationService]:
    descriptor = RuntimeAdapterDescriptor(
        key="runtime_failure",
        contract_version="runtime-failure-contract-v1",
        implementation_version="runtime-failure-implementation-v1",
        config_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {"operation": {"const": "test"}},
            "additionalProperties": False,
        },
        capability=RuntimeAdapterCapability(
            invocation=True,
            v2_invocation=True,
            invocation_runtime=True,
        ),
        factory=lambda _context: adapter,
        health_check=_healthy,
        lifecycle=RuntimeAdapterLifecycle(activate=_noop, dispose=_noop),
    )
    catalog = await RuntimeCatalog.activate(
        [descriptor],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="runtime-failure-test")
    if isinstance(runs, MemoryRunRepository) and isinstance(results, MemoryResultRepository):
        completion_store = MemoryInvocationCompletionStore(
            run_repository=runs,
            result_repository=results,
        )
    elif isinstance(runs, DatabaseRunRepository) and isinstance(results, DatabaseResultRepository):
        completion_store = DatabaseInvocationCompletionStore(runs.session_factory)
    else:  # pragma: no cover - helper has only the two supported repository families
        raise TypeError("Runtime failure tests require an Invocation Completion Store")
    return catalog, InvocationService(
        registry=object(),
        run_repository=runs,
        result_repository=results,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
        completion_store=completion_store,
    )


@pytest.mark.parametrize(
    ("category", "code", "retryable"),
    _FAILURE_CASES,
)
async def test_accepted_runtime_failure_categories_converge_to_one_safe_terminal_result(
    category: str,
    code: str,
    retryable: bool,
) -> None:
    adapter = _RuntimeOutcomeAdapter(
        RawInvocationOutcome(
            failure=RawInvocationFailure(
                category=category,
                code=code,
                retryable=retryable,
            )
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.model_dump() == {
        "code": code,
        "message": "Agent invocation could not be completed.",
        "details": {"category": category, "retryable": retryable},
    }
    assert len(adapter.calls) == 1
    assert len(runs.runs) == len(results.results) == 1
    stored_run = await runs.get_run(result.run_id)
    assert stored_run is not None
    assert stored_run.status == "failed"
    assert stored_run.error == result.error.model_dump()
    assert results.results[0].run_id == result.run_id
    assert results.results[0].error == result.error.model_dump()

    await catalog.aclose()


async def test_accepted_runtime_exception_is_redacted_and_still_has_one_terminal_result() -> None:
    secret = "https://private.example/run?token=secret-marker"
    adapter = _RuntimeOutcomeAdapter(RuntimeError(secret))
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.model_dump() == {
        "code": "invocation_remote_failure",
        "message": "Agent invocation could not be completed.",
        "details": {"category": "remote_failure", "retryable": False},
    }
    persisted = "\n".join(
        [
            result.model_dump_json(),
            (await runs.get_run(result.run_id)).model_dump_json(),
            results.results[0].model_dump_json(),
        ]
    )
    assert secret not in persisted
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


async def test_malformed_raw_usage_cannot_leak_credentials_to_an_accepted_result() -> None:
    secret = "Bearer usage-secret-marker"
    adapter = _RuntimeOutcomeAdapter(
        RawInvocationOutcome.model_construct(
            output={"summary": "unused"},
            usage={"authorization": secret, "diagnostic": secret},
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        result = await service.invoke(_request())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert not emitted
    persisted = "\n".join(
        [
            result.model_dump_json(),
            (await runs.get_run(result.run_id)).model_dump_json(),
            results.results[0].model_dump_json(),
        ]
    )
    assert secret not in persisted

    await catalog.aclose()


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": "3"},
        {"input_tokens": 1_000_000_001},
    ],
)
async def test_untrusted_or_unbounded_usage_becomes_one_safe_invalid_response(
    usage: dict[str, object],
) -> None:
    adapter = _RuntimeOutcomeAdapter(
        RawInvocationOutcome.model_construct(
            output={"summary": "unused"},
            usage=usage,
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        result = await service.invoke(_request())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert not emitted
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


async def test_untrusted_artifact_child_model_becomes_one_safe_invalid_response() -> None:
    secret = "artifact-child-secret-marker"
    adapter = _RuntimeOutcomeAdapter(
        RawInvocationOutcome.model_construct(
            output={"summary": "unused"},
            artifact_refs=[
                ArtifactRef.model_construct(
                    artifact_id=42,
                    type="document",
                    uri=f"data:text/plain,{secret}",
                    metadata={"body": secret},
                )
            ],
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        result = await service.invoke(_request())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert not emitted
    persisted = "\n".join(
        [
            result.model_dump_json(),
            (await runs.get_run(result.run_id)).model_dump_json(),
            results.results[0].model_dump_json(),
        ]
    )
    assert secret not in persisted

    await catalog.aclose()


async def test_accepted_adapter_cancellation_converges_to_one_safe_terminal_result() -> None:
    secret = "cancelled adapter credential=secret-marker"
    adapter = _RuntimeOutcomeAdapter(asyncio.CancelledError(secret))
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_remote_failure"
    assert len(runs.runs) == len(results.results) == 1
    stored_run = await runs.get_run(result.run_id)
    assert stored_run is not None
    assert stored_run.status == "failed"
    persisted = "\n".join(
        [
            result.model_dump_json(),
            stored_run.model_dump_json(),
            results.results[0].model_dump_json(),
        ]
    )
    assert secret not in persisted

    await catalog.aclose()


async def test_runtime_usage_projects_only_closed_numeric_usage_facts() -> None:
    adapter = _RuntimeOutcomeAdapter(
        RawInvocationOutcome(
            output={"summary": "safe usage"},
            usage={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())

    assert result.status == "completed"
    assert result.usage["input_tokens"] == 3
    assert result.usage["output_tokens"] == 5
    assert result.usage["total_tokens"] == 8
    assert set(result.usage) == {"input_tokens", "output_tokens", "total_tokens", "latency_ms"}

    await catalog.aclose()


@pytest.mark.parametrize("malformed", [{"summary": "not a raw outcome"}, object()])
async def test_accepted_malformed_adapter_outcome_becomes_invalid_response(
    malformed: object,
) -> None:
    adapter = _RuntimeOutcomeAdapter(malformed)
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert result.error.details == {"category": "invalid_response", "retryable": False}
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


@pytest.mark.parametrize(("category", "code", "retryable"), _FAILURE_CASES)
async def test_accepted_runtime_failure_persists_one_database_run_and_result(
    tmp_path,
    managed_database,
    category: str,
    code: str,
    retryable: bool,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime-failure.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    adapter = _RuntimeOutcomeAdapter(
        RawInvocationOutcome(
            failure=RawInvocationFailure(
                category=category,
                code=code,
                retryable=retryable,
            )
        )
    )
    runs = DatabaseRunRepository(session_factory)
    results = DatabaseResultRepository(session_factory)
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())
    stored_run = await runs.get_run(result.run_id)
    stored_results = await results.list_recent(
        "runtime-failure-session",
        tenant_id="tenant-1",
        user_id="operator-1",
    )

    assert result.status == "failed"
    assert stored_run is not None and stored_run.status == "failed"
    assert len(stored_results) == 1
    assert stored_results[0].run_id == result.run_id
    assert result.error is not None
    assert result.error.code == code
    assert result.error.details == {"category": category, "retryable": retryable}
    assert stored_results[0].error == result.error.model_dump()

    await catalog.aclose()


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (RuntimeError("remote token=database-secret-marker"), "invocation_remote_failure"),
        ({"summary": "remote token=database-secret-marker"}, "invocation_invalid_response"),
    ],
)
async def test_database_exception_and_malformed_outcome_are_redacted_and_terminal(
    tmp_path,
    managed_database,
    response: object,
    code: str,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / f'{code}.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    adapter = _RuntimeOutcomeAdapter(response)
    runs = DatabaseRunRepository(session_factory)
    results = DatabaseResultRepository(session_factory)
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())
    stored_run = await runs.get_run(result.run_id)
    stored_results = await results.list_recent(
        "runtime-failure-session",
        tenant_id="tenant-1",
        user_id="operator-1",
    )

    assert result.status == "failed"
    assert result.error is not None and result.error.code == code
    assert stored_run is not None
    assert len(stored_results) == 1
    persisted = "\n".join(
        [
            result.model_dump_json(),
            stored_run.model_dump_json(),
            stored_results[0].model_dump_json(),
        ]
    )
    assert "database-secret-marker" not in persisted

    await catalog.aclose()


async def test_database_adapter_call_happens_outside_start_and_finish_session_scopes(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime-transaction-boundary.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    tracker = _SessionScopeTracker(session_factory)
    adapter = _SessionScopeCheckingAdapter(
        RawInvocationOutcome(output={"summary": "outside transaction"}),
        tracker=tracker,
    )
    runs = DatabaseRunRepository(tracker)  # type: ignore[arg-type]
    results = DatabaseResultRepository(tracker)  # type: ignore[arg-type]
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())

    assert result.status == "completed"
    assert adapter.observed_active_scopes == [0]
    assert tracker.active_scopes == 0

    await catalog.aclose()


async def test_accepted_memory_completion_retries_persistence_without_reinvoking_adapter() -> None:
    adapter = _RuntimeOutcomeAdapter(RawInvocationOutcome(output={"summary": "recovered"}))
    runs = MemoryRunRepository()
    results = _FailOnceResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())

    assert result.status == "completed"
    assert adapter.calls and len(adapter.calls) == 1
    assert results.failures == 1
    assert len(runs.runs) == len(results.results) == 1
    assert (await runs.get_run(result.run_id)).status == "completed"

    await catalog.aclose()


async def test_accepted_database_completion_retries_flush_without_reinvoking_adapter(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime-completion-retry.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    tracker = _SessionScopeTracker(session_factory)
    adapter = _RuntimeOutcomeAdapter(RawInvocationOutcome(output={"summary": "recovered"}))
    runs = DatabaseRunRepository(tracker)  # type: ignore[arg-type]
    results = DatabaseResultRepository(tracker)  # type: ignore[arg-type]
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)
    tracker.raise_on_next_flush = True

    result = await service.invoke(_request())

    stored_results = await results.list_recent(
        "runtime-failure-session",
        tenant_id="tenant-1",
        user_id="operator-1",
    )
    assert result.status == "completed"
    assert len(adapter.calls) == 1
    assert len(stored_results) == 1
    assert stored_results[0].run_id == result.run_id

    await catalog.aclose()


async def test_repeated_direct_requests_are_fresh_runs_with_one_result_each() -> None:
    adapter = _RuntimeOutcomeAdapter(RawInvocationOutcome(output={"summary": "fresh"}))
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    first = await service.invoke(_request())
    second = await service.invoke(_request())

    assert first.run_id != second.run_id
    assert len(adapter.calls) == 2
    assert len(runs.runs) == len(results.results) == 2
    assert {item.run_id for item in results.results} == {first.run_id, second.run_id}

    await catalog.aclose()


async def test_database_completion_store_converges_concurrent_finish_attempts_to_one_result(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime-completion-race.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    runs = DatabaseRunRepository(session_factory)
    results = DatabaseResultRepository(session_factory)
    started_at = datetime.now(UTC)
    accepted_run = AgentRun(
        run_id="run-runtime-completion-race",
        request_id="runtime-completion-request",
        session_id="runtime-completion-session",
        agent_id="runtime-failure-agent",
        user_id="operator-1",
        tenant_id="tenant-1",
        status="running",
        invoker_type="runtime_failure",
        handling_kind="invocation",
        created_at=started_at,
        updated_at=started_at,
    )
    await runs.add_run(accepted_run)
    completed_run = accepted_run.model_copy(
        update={
            "status": "failed",
            "error": {
                "code": "invocation_remote_failure",
                "message": "Agent invocation could not be completed.",
                "details": {"category": "remote_failure", "retryable": False},
            },
            "updated_at": datetime.now(UTC),
        }
    )

    completion_result = AgentResult(
        result_id="result-runtime-completion-race",
        run_id=accepted_run.run_id,
        session_id=accepted_run.session_id,
        agent_id=accepted_run.agent_id,
        user_id=accepted_run.user_id,
        tenant_id=accepted_run.tenant_id,
        status="failed",
        message="Agent invocation could not be completed.",
        error=completed_run.error,
        created_at=datetime.now(UTC),
    )

    first_store = DatabaseInvocationCompletionStore(session_factory)
    second_store = DatabaseInvocationCompletionStore(session_factory)
    first, second = await asyncio.gather(
        first_store.complete(run=completed_run, result=completion_result),
        second_store.complete(run=completed_run, result=completion_result),
    )

    assert first[0].status == second[0].status == "failed"
    assert first[1].result_id == second[1].result_id
    persisted = await results.list_recent(
        accepted_run.session_id,
        tenant_id="tenant-1",
        user_id="operator-1",
    )
    assert len(persisted) == 1
    assert persisted[0].run_id == accepted_run.run_id


async def test_database_completion_store_recovers_after_commit_acknowledgement_loss(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime-completion-ack-loss.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    tracker = _SessionScopeTracker(session_factory)
    runs = DatabaseRunRepository(tracker)  # type: ignore[arg-type]
    results = DatabaseResultRepository(tracker)  # type: ignore[arg-type]
    started_at = datetime.now(UTC)
    accepted_run = AgentRun(
        run_id="run-runtime-completion-ack-loss",
        request_id="runtime-completion-ack-request",
        session_id="runtime-completion-ack-session",
        agent_id="runtime-failure-agent",
        user_id="operator-1",
        tenant_id="tenant-1",
        status="running",
        invoker_type="runtime_failure",
        handling_kind="invocation",
        created_at=started_at,
        updated_at=started_at,
    )
    await runs.add_run(accepted_run)
    completed_run = accepted_run.model_copy(
        update={
            "status": "failed",
            "error": {
                "code": "invocation_remote_failure",
                "message": "Agent invocation could not be completed.",
                "details": {"category": "remote_failure", "retryable": False},
            },
            "updated_at": datetime.now(UTC),
        }
    )
    completion_result = AgentResult(
        result_id="result-runtime-completion-ack-loss",
        run_id=accepted_run.run_id,
        session_id=accepted_run.session_id,
        agent_id=accepted_run.agent_id,
        user_id=accepted_run.user_id,
        tenant_id=accepted_run.tenant_id,
        status="failed",
        message="Agent invocation could not be completed.",
        error=completed_run.error,
        created_at=datetime.now(UTC),
    )
    store = DatabaseInvocationCompletionStore(tracker)  # type: ignore[arg-type]
    tracker.raise_after_next_exit = True

    stored_run, stored_result = await store.complete(
        run=completed_run,
        result=completion_result,
    )

    assert stored_run.status == "failed"
    assert stored_result.result_id == completion_result.result_id
    persisted = await results.list_recent(
        accepted_run.session_id,
        tenant_id="tenant-1",
        user_id="operator-1",
    )
    assert len(persisted) == 1
    assert persisted[0].result_id == completion_result.result_id


async def test_database_completion_replays_after_runtime_rebuild_without_adapter_reinvocation(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime-completion-rebuild.db'}",
    )
    await managed_database.initialize_schema(settings)
    first_factory = await managed_database.session_factory(settings)
    adapter = _RuntimeOutcomeAdapter(RawInvocationOutcome(output={"summary": "durable"}))
    runs = DatabaseRunRepository(first_factory)
    results = DatabaseResultRepository(first_factory)
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)

    result = await service.invoke(_request())
    accepted_run = await runs.get_run(result.run_id)
    persisted_results = await results.list_recent(
        "runtime-failure-session",
        tenant_id="tenant-1",
        user_id="operator-1",
    )
    assert accepted_run is not None
    assert len(persisted_results) == 1
    await catalog.aclose()

    rebuilt_factory = await managed_database.session_factory(settings)
    replayed_run, replayed_result = await DatabaseInvocationCompletionStore(
        rebuilt_factory
    ).complete(
        run=accepted_run,
        result=persisted_results[0],
    )

    assert replayed_run.run_id == result.run_id
    assert replayed_result.result_id == persisted_results[0].result_id
    assert len(adapter.calls) == 1


async def test_invalid_runtime_output_schema_becomes_a_safe_invalid_response() -> None:
    secret = "remote payload token=secret-marker"
    adapter = _RuntimeOutcomeAdapter(RawInvocationOutcome(output={"summary": secret}))
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    catalog, service = await _runtime_service(adapter=adapter, runs=runs, results=results)
    definition_payload = _definition().model_dump(mode="json")
    definition_payload["output_schema"] = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "integer"}},
    }
    definition = AgentDefinitionV2.model_validate(definition_payload)
    service.snapshot_runtime.load([definition], source="runtime-failure-invalid-output")

    result = await service.invoke(_request())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    persisted = "\n".join(
        [
            result.model_dump_json(),
            (await runs.get_run(result.run_id)).model_dump_json(),
            results.results[0].model_dump_json(),
        ]
    )
    assert secret not in persisted
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


def test_raw_failure_contract_rejects_unknown_categories_and_diagnostics() -> None:
    with pytest.raises(ValueError):
        RawInvocationFailure.model_validate(
            {
                "category": "other",
                "code": "remote_payload_error",
                "retryable": True,
            }
        )
    with pytest.raises(ValueError):
        RawInvocationOutcome.model_validate(
            {
                "message": "Adapter private diagnostics",
                "failure": {
                    "category": "remote_failure",
                    "code": "invocation_remote_failure",
                    "retryable": False,
                },
            }
        )
    with pytest.raises(ValueError):
        RawInvocationFailure.model_validate(
            {
                "category": "remote_failure",
                "code": "invocation_remote_failure",
                "retryable": False,
                "diagnostics": "token=secret-marker",
            }
        )
