import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.repositories.execution_traces import MemoryExecutionTraceRepository
from app.schemas.execution_traces import (
    ExecutionTraceEvent,
    ExecutionTraceEventDraft,
    ExecutionTraceQuery,
)
from app.schemas.memory import MemoryManagementOperationResponse
from app.schemas.turns import CanonicalTurn, TurnUserInput
from app.services.execution_trace_service import ExecutionTraceService
from host_adapters.oac.api.runtime_observation import router, runtime_observation_stream
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity.models import TrustedHostIdentity
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)


class ToggleFailTraceRepository(MemoryExecutionTraceRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_appends = False

    async def append(self, event):
        if self.fail_appends:
            raise RuntimeError("trace store unavailable")
        return await super().append(event)


class LegacyMemoryTraceRepository(MemoryExecutionTraceRepository):
    def __init__(self, *legacy_events: ExecutionTraceEvent) -> None:
        super().__init__()
        for legacy_event in legacy_events:
            self.restore(legacy_event)

    def restore(self, legacy_event: ExecutionTraceEvent) -> None:
        source_key = (
            legacy_event.source,
            legacy_event.source_event_id,
            legacy_event.source_version,
        )
        self._events.append(legacy_event)
        self._by_source[source_key] = legacy_event
        self._next_offset = max(self._next_offset, legacy_event.event_offset + 1)


def _event() -> ExecutionTraceEventDraft:
    return ExecutionTraceEventDraft(
        trace_id="trace_turn-1",
        tenant_id="oac",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
        event_type="canonical_turn",
        stage="received",
        status="running",
        source="oir:turn",
        source_event_id="turn-1:created",
        facts={"request_id": "request-1", "source": "host_chat", "input_kind": "text"},
        occurred_at=datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
    )


def _turn(
    *,
    user_id: str = "user-1",
    session_id: str = "session-1",
    turn_id: str = "turn-1",
) -> CanonicalTurn:
    now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    return CanonicalTurn(
        turn_id=turn_id,
        tenant_id="oac",
        user_id=user_id,
        session_id=session_id,
        request_id=f"request-{turn_id}",
        source="host_chat",
        user_input=TurnUserInput(text="hello"),
        created_at=now,
        updated_at=now,
    )


class TurnPort:
    def __init__(self, turn: CanonicalTurn) -> None:
        self.turn = turn

    async def get_turn(self, *, turn_id: str, tenant_id: str, user_id: str):
        if (
            turn_id == self.turn.turn_id
            and tenant_id == self.turn.tenant_id
            and user_id == self.turn.user_id
        ):
            return self.turn
        return None


class MemoryManagementPort:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def resolve_pending(self, **kwargs):
        self.calls.append(kwargs)
        return MemoryManagementOperationResponse(
            operation_id="operation-1",
            operation=kwargs["action"],
            status="completed",
            decision_id=kwargs["decision_id"],
            memory_id="memory-1",
            index_operation_id="index-1" if kwargs["action"] == "confirm" else None,
            provider_status="pending" if kwargs["action"] == "confirm" else None,
        )

    async def get_pending_decision_evidence(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "decision_id": kwargs["decision_id"],
            "previous_value": "旧偏好",
            "proposed_value": "新偏好",
        }


def _client(
    non_lifespan_test_client,
    trace_service: ExecutionTraceService,
    *,
    user_id: str,
    memory_management=None,
) -> TestClient:
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=TurnPort(_turn(user_id=user_id)),
        execution_traces=trace_service,
        memory_management=memory_management,
    )
    identity = TrustedHostIdentity(
        key_id="user-key",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id=user_id,
        groups=(),
        credential_class="oac_user",
        claims_version="oac-principal-v1",
        roles=("operator",),
        active_bundle_id="oac-operations",
        policy_version="oac-authz-v1",
        signature_version="v2",
        request_operation="runtime-observation",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    app.dependency_overrides[get_trusted_host_identity] = lambda: identity
    return non_lifespan_test_client(app)


def test_runtime_observation_resolves_only_a_pending_memory_decision_in_the_owned_trace(
    non_lifespan_test_client,
) -> None:
    first = ExecutionTraceEvent(**_event().model_dump(), event_offset=1)
    legacy_decision = ExecutionTraceEvent(
        event_offset=2,
        trace_id="trace_turn-1",
        tenant_id="oac",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
        event_type="memory_decision",
        stage="decision_pending",
        status="blocked",
        source="oir:memory",
        source_event_id="memory-decision-1",
        reason_code="ambiguous_conflict",
        schema_version=1,
        facts={
            "decision_id": "decision-1",
            "operation": "pending",
            "decision_status": "pending",
            "reason_code": "ambiguous_conflict",
            "previous_value": "旧偏好",
            "proposed_value": "新偏好",
        },
        occurred_at=datetime(2026, 7, 22, 10, 1, tzinfo=UTC),
    )
    trace_service = ExecutionTraceService(LegacyMemoryTraceRepository(first, legacy_decision))
    memory = MemoryManagementPort()
    client = _client(
        non_lifespan_test_client, trace_service, user_id="user-1", memory_management=memory
    )
    evidence_path = (
        "/api/v1/runtime-observation/sessions/session-1/turns/turn-1/"
        "memory-decisions/decision-1/evidence"
    )
    evidence = client.get(evidence_path)
    assert evidence.status_code == 200
    assert evidence.json() == {
        "decision_id": "decision-1",
        "previous_value": "旧偏好",
        "proposed_value": "新偏好",
    }
    assert (
        _client(
            non_lifespan_test_client,
            trace_service,
            user_id="user-2",
            memory_management=memory,
        )
        .get(evidence_path)
        .status_code
        == 404
    )
    memory.calls.clear()
    public_snapshot = client.get("/api/v1/runtime-observation/sessions/session-1/turns/turn-1")
    assert public_snapshot.status_code == 200
    public_decision = public_snapshot.json()["events"][1]
    assert "previous_value" not in public_decision["facts"]
    assert "proposed_value" not in public_decision["facts"]
    path = (
        "/api/v1/runtime-observation/sessions/session-1/turns/turn-1/"
        "memory-decisions/decision-1/confirm"
    )

    response = client.post(
        path,
        json={
            "idempotency_key": "confirm-decision-1",
            "reason": "确认采用新偏好",
            "expected_revision_id": "revision-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["operation"] == "confirm"
    assert memory.calls == [
        {
            "decision_id": "decision-1",
            "action": "confirm",
            "tenant_id": "oac",
            "user_id": "user-1",
            "actor": "user-1",
            "reason": "确认采用新偏好",
            "idempotency_key": "confirm-decision-1",
            "expected_revision_id": "revision-1",
            "trace_session_id": "session-1",
            "trace_turn_id": "turn-1",
        }
    ]

    missing = client.post(
        path.replace("decision-1/confirm", "decision-other/reject"),
        json={
            "idempotency_key": "reject-decision-other",
            "reason": "拒绝",
        },
    )
    assert missing.status_code == 404
    assert len(memory.calls) == 1


def test_runtime_observation_snapshot_is_scoped_to_the_trusted_host_identity(
    non_lifespan_test_client,
) -> None:
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    asyncio.run(trace_service.record(_event()))

    owner = _client(non_lifespan_test_client, trace_service, user_id="user-1")
    owner_response = owner.get("/api/v1/runtime-observation/sessions/session-1/turns/turn-1")

    assert owner_response.status_code == 200
    assert owner_response.json()["watermark"] == 1
    assert owner_response.json()["events"][0]["tenant_id"] == "oac"
    assert owner_response.json()["events"][0]["user_id"] == "user-1"

    other_user = _client(non_lifespan_test_client, trace_service, user_id="user-2")
    other_response = other_user.get("/api/v1/runtime-observation/sessions/session-1/turns/turn-1")

    assert other_response.status_code == 404
    assert other_response.json() == {"detail": "execution_trace_not_found"}


def test_ui_handoff_projects_idempotent_requested_and_confirmed_path(
    non_lifespan_test_client,
) -> None:
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    asyncio.run(trace_service.record(_event()))
    client = _client(non_lifespan_test_client, trace_service, user_id="user-1")
    path = "/api/v1/runtime-observation/sessions/session-1/turns/turn-1/handoffs"

    requested = {
        "handoff_id": "handoff-1",
        "status": "requested",
        "from_path": "/dashboard",
        "target_route": "/production",
        "reason": "open_agent",
        "occurred_at": "2026-07-22T10:01:00Z",
    }
    first = client.post(path, json=requested)
    duplicate = client.post(path, json=requested)
    completed = client.post(
        path,
        json={
            **requested,
            "status": "completed",
        },
    )

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert completed.status_code == 202
    snapshot = asyncio.run(
        trace_service.snapshot(
            ExecutionTraceQuery(
                tenant_id="oac",
                user_id="user-1",
                session_id="session-1",
                turn_id="turn-1",
            )
        )
    )
    handoffs = [event for event in snapshot.events if event.event_type == "ui_handoff"]
    assert [(event.stage, event.status) for event in handoffs] == [
        ("requested", "running"),
        ("target_opened", "completed"),
    ]
    assert all(event.facts["target_route"] == "/production" for event in handoffs)


def test_ui_handoff_reports_trace_degradation_without_reclassifying_acceptance(
    non_lifespan_test_client,
) -> None:
    repository = ToggleFailTraceRepository()
    trace_service = ExecutionTraceService(repository)
    asyncio.run(trace_service.record(_event()))
    repository.fail_appends = True
    client = _client(non_lifespan_test_client, trace_service, user_id="user-1")

    response = client.post(
        "/api/v1/runtime-observation/sessions/session-1/turns/turn-1/handoffs",
        json={
            "handoff_id": "handoff-degraded",
            "status": "requested",
            "from_path": "/dashboard",
            "target_route": "/production",
            "reason": "open_agent",
            "occurred_at": "2026-07-22T10:01:00Z",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "observation_status": "incomplete",
        "incomplete_reason_codes": ["trace_projection_write_failed"],
    }


def test_ui_handoff_rejects_unowned_turn_and_conflicting_source_identity(
    non_lifespan_test_client,
) -> None:
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    asyncio.run(trace_service.record(_event()))
    client = _client(non_lifespan_test_client, trace_service, user_id="user-1")
    path = "/api/v1/runtime-observation/sessions/session-1/turns/turn-1/handoffs"
    requested = {
        "handoff_id": "handoff-1",
        "status": "requested",
        "from_path": "/dashboard",
        "target_route": "/production",
        "reason": "open_agent",
        "occurred_at": "2026-07-22T10:01:00Z",
    }

    assert (
        client.post(
            "/api/v1/runtime-observation/sessions/session-1/turns/turn-other/handoffs",
            json=requested,
        ).status_code
        == 404
    )
    assert client.post(path, json=requested).status_code == 202
    conflict = client.post(path, json={**requested, "target_route": "/knowledge"})

    assert conflict.status_code == 409
    snapshot = asyncio.run(
        trace_service.snapshot(
            ExecutionTraceQuery(
                tenant_id="oac",
                user_id="user-1",
                session_id="session-1",
                turn_id="turn-1",
            )
        )
    )
    assert len([event for event in snapshot.events if event.event_type == "ui_handoff"]) == 1


def test_page_workflow_projects_started_provider_stage_and_terminal_result(
    non_lifespan_test_client,
) -> None:
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    asyncio.run(trace_service.record(_event()))
    client = _client(non_lifespan_test_client, trace_service, user_id="user-1")
    path = "/api/v1/runtime-observation/sessions/session-1/turns/turn-1/page-workflows"
    base = {
        "event_id": "started",
        "run_id": "analysis-run-1",
        "workflow_id": "7658193531173126184",
        "agent_id": "strategy_analysis",
        "capability": "demand_analysis",
        "occurred_at": "2026-07-22T10:02:00Z",
    }

    assert client.post(path, json={**base, "status": "started"}).status_code == 202
    assert (
        client.post(
            path,
            json={**base, "event_id": "stage-1", "status": "stage", "stage_name": "需求要素提取"},
        ).status_code
        == 202
    )
    assert (
        client.post(
            path,
            json={
                **base,
                "event_id": "completed",
                "status": "completed",
                "result_summary": "已形成需求分析",
            },
        ).status_code
        == 202
    )

    snapshot = asyncio.run(
        trace_service.snapshot(
            ExecutionTraceQuery(
                tenant_id="oac",
                user_id="user-1",
                session_id="session-1",
                turn_id="turn-1",
            )
        )
    )
    events = [event for event in snapshot.events if event.run_id == "analysis-run-1"]
    assert [(event.event_type, event.stage, event.status) for event in events] == [
        ("agent_run", "started", "running"),
        ("agent_event", "provider_stage", "running"),
        ("agent_result", "result_received", "completed"),
    ]
    assert events[1].facts["provider_stage_name"] == "需求要素提取"
    assert events[2].facts["result_summary"] == "已形成需求分析"
    assert all(event.evidence_refs[0].reference_id == base["workflow_id"] for event in events)


def test_page_workflow_rejects_unowned_turn_and_invalid_lifecycle_payload(
    non_lifespan_test_client,
) -> None:
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    asyncio.run(trace_service.record(_event()))
    client = _client(non_lifespan_test_client, trace_service, user_id="user-1")
    payload = {
        "event_id": "failed",
        "run_id": "analysis-run-1",
        "workflow_id": "7658193531173126184",
        "agent_id": "strategy_analysis",
        "capability": "demand_analysis",
        "status": "failed",
        "occurred_at": "2026-07-22T10:02:00Z",
    }

    unowned = client.post(
        "/api/v1/runtime-observation/sessions/session-1/turns/turn-other/page-workflows",
        json={**payload, "error_code": "provider_failed"},
    )
    invalid = client.post(
        "/api/v1/runtime-observation/sessions/session-1/turns/turn-1/page-workflows",
        json=payload,
    )

    assert unowned.status_code == 404
    assert invalid.status_code == 422


def test_runtime_observation_stream_starts_after_the_snapshot_watermark() -> None:
    repository = LegacyMemoryTraceRepository(
        ExecutionTraceEvent(**_event().model_dump(), event_offset=1)
    )
    trace_service = ExecutionTraceService(repository)
    identity = TrustedHostIdentity(
        key_id="user-key",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id="user-1",
        groups=(),
        credential_class="oac_user",
        claims_version="oac-principal-v1",
        roles=("operator",),
        active_bundle_id="oac-operations",
        policy_version="oac-authz-v1",
        signature_version="v2",
        request_operation="runtime-observation",
    )
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=TurnPort(_turn()),
        execution_traces=trace_service,
    )

    async def read_stream() -> tuple[str, str, str]:
        response = await runtime_observation_stream(
            session_id="session-1",
            turn_id="turn-1",
            last_event_id="1",
            identity=identity,
            ports=ports,
        )
        metadata = await anext(response.body_iterator)
        legacy_event = ExecutionTraceEvent(
            event_offset=2,
            trace_id="trace_turn-1",
            tenant_id="oac",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
            event_type="memory_decision",
            stage="decision_pending",
            status="blocked",
            source="oir:memory",
            source_event_id="memory-decision-legacy",
            schema_version=1,
            facts={
                "decision_id": "decision-legacy",
                "decision_status": "pending",
                "previous_value": "legacy private memory body",
                "proposed_value": "legacy replacement memory body",
            },
        )
        repository.restore(legacy_event)
        event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return (
            response.headers["X-OIR-Trace-Completeness"],
            str(metadata),
            str(event),
        )

    completeness, metadata, event = asyncio.run(read_stream())
    assert completeness == "complete"
    assert '"completeness":"complete"' in metadata
    assert "id: 2" in event
    assert "previous_value" not in event
    assert "proposed_value" not in event
    assert "legacy private memory body" not in event
    assert "legacy replacement memory body" not in event


def test_runtime_observation_stream_keeps_events_written_after_the_client_snapshot() -> None:
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    asyncio.run(trace_service.record(_event()))
    query = ExecutionTraceQuery(
        tenant_id="oac",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
    )
    client_snapshot = asyncio.run(trace_service.snapshot(query))
    asyncio.run(
        trace_service.record(_event().model_copy(update={"source_event_id": "turn-1:updated"}))
    )
    identity = TrustedHostIdentity(
        key_id="user-key",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id="user-1",
        groups=(),
        credential_class="oac_user",
        claims_version="oac-principal-v1",
        roles=("operator",),
        active_bundle_id="oac-operations",
        policy_version="oac-authz-v1",
        signature_version="v2",
        request_operation="runtime-observation",
    )
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=TurnPort(_turn()),
        execution_traces=trace_service,
    )

    async def read_stream() -> str:
        response = await runtime_observation_stream(
            session_id="session-1",
            turn_id="turn-1",
            last_event_id=str(client_snapshot.watermark),
            identity=identity,
            ports=ports,
        )
        await anext(response.body_iterator)
        event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return str(event)

    assert "id: 2" in asyncio.run(read_stream())


def test_runtime_observation_stream_reports_a_gap_discovered_after_connect() -> None:
    repository = ToggleFailTraceRepository()
    trace_service = ExecutionTraceService(repository)
    asyncio.run(trace_service.record(_event()))
    identity = TrustedHostIdentity(
        key_id="user-key",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id="user-1",
        groups=(),
        credential_class="oac_user",
        claims_version="oac-principal-v1",
        roles=("operator",),
        active_bundle_id="oac-operations",
        policy_version="oac-authz-v1",
        signature_version="v2",
        request_operation="runtime-observation",
    )
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=TurnPort(_turn()),
        execution_traces=trace_service,
    )

    async def read_stream() -> str:
        response = await runtime_observation_stream(
            session_id="session-1",
            turn_id="turn-1",
            last_event_id="1",
            identity=identity,
            ports=ports,
        )
        initial = await anext(response.body_iterator)
        assert '"completeness":"complete"' in str(initial)
        assert '"recovered_memory_revisions":[]' in str(initial)
        repository.fail_appends = True
        assert (
            await trace_service.try_record(
                _event().model_copy(update={"source_event_id": "turn-1:missing"})
            )
            is False
        )
        metadata = await asyncio.wait_for(anext(response.body_iterator), timeout=0.5)
        await response.body_iterator.aclose()
        return str(metadata)

    metadata = asyncio.run(read_stream())
    assert "event: execution_trace_meta" in metadata
    assert '"completeness":"incomplete"' in metadata
