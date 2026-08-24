from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.execution_traces import (
    DatabaseExecutionTraceRepository,
    ExecutionTraceRepository,
    MemoryExecutionTraceRepository,
)
from app.repositories.memory_index_operations import MemoryIndexOutboxRepository
from app.schemas.execution_traces import (
    ExecutionTraceEvent,
    ExecutionTraceEventDraft,
    ExecutionTraceQuery,
    trace_id_for_turn,
)
from app.schemas.memory import MemoryIndexOperation, MemoryItem
from app.schemas.turns import CanonicalTurn, TurnSemanticResponse, TurnStatus, TurnUserInput
from app.services.execution_trace_service import ExecutionTraceConflict, ExecutionTraceService
from tests.support.database import raw_engine_scope


class _LegacyMemoryExecutionTraceRepository(MemoryExecutionTraceRepository):
    def __init__(self, *events: ExecutionTraceEvent) -> None:
        super().__init__()
        for event in events:
            self.restore(event)

    def restore(self, event: ExecutionTraceEvent) -> None:
        source_key = (event.source, event.source_event_id, event.source_version)
        self._events.append(event)
        self._by_source[source_key] = event
        self._next_offset = max(self._next_offset, event.event_offset + 1)


def _agent_progress(
    *,
    source_event_id: str = "provider-run-1:stage-1",
    facts: dict[str, object] | None = None,
) -> ExecutionTraceEventDraft:
    return ExecutionTraceEventDraft(
        trace_id="trace_turn-1",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
        event_type="agent_event",
        stage="provider_progress",
        status="running",
        source="provider:workflow",
        source_event_id=source_event_id,
        source_version=1,
        facts=facts
        or {
            "capability": "demand_analysis",
            "provider_stage_name": "需求要素提取",
        },
        occurred_at=datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
    )


async def test_trace_writer_replays_the_original_source_event_without_new_offset() -> None:
    service = ExecutionTraceService(MemoryExecutionTraceRepository())

    first = await service.record(_agent_progress())
    replay = await service.record(_agent_progress())
    snapshot = await service.snapshot(
        ExecutionTraceQuery(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
        )
    )

    assert first.created is True
    assert replay.created is False
    assert replay.event.event_offset == first.event.event_offset == 1
    assert snapshot.watermark == 1
    assert snapshot.completeness == "complete"
    assert [event.event_offset for event in snapshot.events] == [1]


async def test_trace_writer_rejects_conflicting_source_replay() -> None:
    service = ExecutionTraceService(MemoryExecutionTraceRepository())
    await service.record(_agent_progress())

    with pytest.raises(ExecutionTraceConflict):
        await service.record(
            _agent_progress(source_event_id="provider-run-1:stage-1").model_copy(
                update={"status": "completed"}
            )
        )


def test_trace_schema_rejects_memory_body_values_and_legacy_writes() -> None:
    with pytest.raises(ValidationError, match="cannot be written to a trace"):
        ExecutionTraceEventDraft(
            trace_id="trace_turn-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
            event_type="memory_decision",
            stage="decision_pending",
            status="pending",
            source="oir:memory_decision",
            source_event_id="decision-1",
            facts={
                "decision_id": "decision-1",
                "decision_status": "pending",
                "previous_value": "private memory body",
                "proposed_value": "replacement memory body",
            },
        )

    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        ExecutionTraceEventDraft(
            trace_id="trace_turn-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
            event_type="memory_decision",
            stage="decision_pending",
            status="pending",
            source="oir:memory_decision",
            source_event_id="decision-legacy-write",
            schema_version=1,
            facts={"decision_id": "decision-legacy-write"},
        )

    legacy = ExecutionTraceEvent(
        event_offset=1,
        trace_id="trace_turn-1",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
        event_type="memory_decision",
        stage="decision_pending",
        status="pending",
        source="oir:memory_decision",
        source_event_id="decision-legacy",
        schema_version=1,
        facts={
            "decision_id": "decision-legacy",
            "decision_status": "pending",
            "previous_value": "legacy private memory body",
            "proposed_value": "legacy replacement memory body",
        },
    )
    assert legacy.schema_version == 1

    with pytest.raises(ValidationError, match="require schema version 1"):
        ExecutionTraceEvent.model_validate(
            legacy.model_dump()
            | {
                "schema_version": 2,
                "source_event_id": "decision-invalid-v2",
            }
        )


async def test_trace_writer_treats_sanitized_v2_as_a_replay_of_legacy_v1() -> None:
    occurred_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    legacy = ExecutionTraceEvent(
        event_offset=1,
        trace_id="trace_turn-1",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
        event_type="memory_decision",
        stage="decision_pending",
        status="pending",
        source="oir:memory_decision",
        source_event_id="decision-legacy",
        schema_version=1,
        facts={
            "decision_id": "decision-legacy",
            "decision_status": "pending",
            "previous_value": "legacy private memory body",
            "proposed_value": "legacy replacement memory body",
        },
        occurred_at=occurred_at,
    )
    service = ExecutionTraceService(_LegacyMemoryExecutionTraceRepository(legacy))
    replay = await service.record(
        ExecutionTraceEventDraft(
            trace_id="trace_turn-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
            event_type="memory_decision",
            stage="decision_pending",
            status="pending",
            source="oir:memory_decision",
            source_event_id="decision-legacy",
            facts={
                "decision_id": "decision-legacy",
                "decision_status": "pending",
            },
            occurred_at=occurred_at,
        )
    )

    assert replay.created is False
    assert replay.event.event_offset == 1
    assert replay.event.schema_version == 1


async def test_trace_repository_revalidates_copied_drafts_before_writing() -> None:
    service = ExecutionTraceService(MemoryExecutionTraceRepository())
    invalid = _agent_progress().model_copy(update={"schema_version": 1})

    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        await service.record(invalid)


async def test_database_trace_writer_uses_one_ordered_idempotent_event_table(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}",
    )
    await managed_database.initialize_schema(settings)
    service = ExecutionTraceService(
        DatabaseExecutionTraceRepository(await managed_database.session_factory(settings))
    )

    first = await service.record(_agent_progress())
    second = await service.record(_agent_progress(source_event_id="provider-run-1:stage-2"))
    replay = await service.record(_agent_progress())
    snapshot = await service.snapshot(
        ExecutionTraceQuery(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
        )
    )

    assert first.event.event_offset == 1
    assert second.event.event_offset == 2
    assert replay.created is False
    assert replay.event.event_offset == 1
    assert [event.event_offset for event in snapshot.events] == [1, 2]

    async with raw_engine_scope(settings.database_url) as engine:
        async with engine.begin() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: sync_connection.dialect.get_table_names(sync_connection)
            )
    assert "execution_trace_events" in tables


async def test_trace_stream_resumes_strictly_after_snapshot_watermark() -> None:
    service = ExecutionTraceService(MemoryExecutionTraceRepository())
    await service.record(_agent_progress())
    query = ExecutionTraceQuery(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
    )
    snapshot = await service.snapshot(query)
    stream = service.stream(query, after_offset=snapshot.watermark, poll_interval_seconds=0.001)

    await service.record(_agent_progress(source_event_id="provider-run-1:stage-2"))
    event = await anext(stream)
    await stream.aclose()

    assert snapshot.watermark == 1
    assert event.event_offset == 2


async def test_trace_projection_failure_keeps_business_flow_and_marks_repair_state() -> None:
    service = ExecutionTraceService(_FailFirstTraceAppend())
    query = ExecutionTraceQuery(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
    )

    assert await service.try_record(_agent_progress()) is False
    initial = await service.snapshot(query)
    assert initial.completeness == "incomplete"
    assert initial.incomplete_reason_codes == ["trace_projection_write_failed"]

    assert (
        await service.try_record(_agent_progress(source_event_id="provider-run-1:stage-2")) is False
    )
    incomplete = await service.snapshot(query)
    assert incomplete.completeness == "incomplete"
    assert [event.event_type for event in incomplete.events] == [
        "agent_event",
        "trace_integrity",
    ]

    assert await service.try_record(_agent_progress()) is True
    repaired = await service.snapshot(query)
    assert repaired.completeness == "complete"
    assert repaired.recovered is False
    assert [event.event_type for event in repaired.events] == [
        "agent_event",
        "trace_integrity",
        "agent_event",
        "trace_integrity",
    ]


async def test_incomplete_trace_recovers_terminal_canonical_state_after_service_restart() -> None:
    repository = MemoryExecutionTraceRepository()
    initial = ExecutionTraceService(repository)
    await initial.record(_agent_progress())
    await initial.record(
        ExecutionTraceEventDraft(
            trace_id="trace_turn-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
            event_type="trace_integrity",
            stage="projection_write_failed",
            status="incomplete",
            source="oir:trace_integrity",
            source_event_id="trace-gap-1",
            reason_code="trace_projection_write_failed",
            facts={
                "reason_code": "trace_projection_write_failed",
                "failed_source": "provider:workflow",
                "failure_id": "failure-1",
                "recovered": False,
            },
            occurred_at=datetime(2026, 7, 22, 10, 1, tzinfo=UTC),
        )
    )
    now = datetime(2026, 7, 22, 10, 2, tzinfo=UTC)
    terminal = CanonicalTurn(
        turn_id="turn-1",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host_chat",
        status=TurnStatus.COMPLETED,
        state_version=4,
        user_input=TurnUserInput(text="hello"),
        final_response=TurnSemanticResponse(kind="agent_result", text="done"),
        created_at=now,
        updated_at=now,
        completed_at=now,
    )
    restarted = ExecutionTraceService(repository, canonical_turns=_TurnPort(terminal))

    snapshot = await restarted.snapshot(
        ExecutionTraceQuery(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
        )
    )

    assert snapshot.completeness == "incomplete"
    assert snapshot.recovered is True
    assert snapshot.recovered_state is not None
    assert snapshot.recovered_state.model_dump() == {
        "source": "canonical_turn",
        "status": "completed",
        "outcome": "agent_result",
        "state_version": 4,
    }
    assert [event.event_offset for event in snapshot.events] == [1, 2]


async def test_trace_snapshot_recovers_current_memory_index_state_without_faking_events() -> None:
    repository = MemoryExecutionTraceRepository()
    initial = ExecutionTraceService(repository)
    occurred_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    await initial.record(
        ExecutionTraceEventDraft(
            trace_id="trace_turn-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
            event_type="memory_revision",
            stage="revision_recorded",
            status="completed",
            source="oir:memory_revision",
            source_event_id="memory-revision:rev-1:turn-1",
            facts={
                "memory_id": "memory-1",
                "revision_id": "rev-1",
                "operation": "add",
                "index_status": "pending",
                "index_operation_status": "pending",
            },
            occurred_at=occurred_at,
        )
    )
    memory_items = MemoryItemRepository()
    await memory_items.add(
        MemoryItem(
            memory_id="memory-1",
            scope="user_preference",
            subject_id="user-1",
            user_id="user-1",
            tenant_id="tenant-1",
            content="private preference",
            current_revision_id="rev-1",
            current_revision_no=1,
            index_status="ready",
        )
    )
    index_operations = MemoryIndexOutboxRepository()
    await index_operations.add(
        MemoryIndexOperation(
            idempotency_key="add:memory-1:rev-1",
            operation="add",
            memory_id="memory-1",
            revision_id="rev-1",
            tenant_id="tenant-1",
            status="completed",
            attempt_count=1,
            updated_at=datetime(2026, 7, 22, 10, 1, tzinfo=UTC),
        )
    )
    restarted = ExecutionTraceService(
        repository,
        memory_items=memory_items,
        index_operations=index_operations,
    )

    snapshot = await restarted.snapshot(
        ExecutionTraceQuery(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
        )
    )

    assert snapshot.completeness == "complete"
    assert snapshot.recovered is True
    assert [event.event_offset for event in snapshot.events] == [1]
    assert [value.model_dump() for value in snapshot.recovered_memory_revisions] == [
        {
            "source": "canonical_memory",
            "memory_id": "memory-1",
            "revision_id": "rev-1",
            "operation": "add",
            "index_status": "ready",
            "index_operation_status": "completed",
        }
    ]
    assert "private preference" not in snapshot.model_dump_json()


class _TurnPort:
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


def test_trace_facts_reject_raw_provider_payloads() -> None:
    with pytest.raises(ValidationError, match="facts"):
        _agent_progress(facts={"provider_payload": {"unsafe": "raw"}})


def test_trace_facts_reject_nested_raw_provider_payloads() -> None:
    with pytest.raises(ValidationError, match="prohibited"):
        _agent_progress(facts={"result_summary": {"raw_payload": "unsafe"}})


def test_trace_id_accepts_a_maximum_length_canonical_turn_id() -> None:
    turn_id = "t" * 128

    event = ExecutionTraceEventDraft(
        **(
            _agent_progress().model_dump()
            | {"turn_id": turn_id, "trace_id": trace_id_for_turn(turn_id)}
        )
    )

    assert event.trace_id == f"trace_{turn_id}"
    assert len(event.trace_id) == 134


class _FailFirstTraceAppend:
    def __init__(self) -> None:
        self._repository: ExecutionTraceRepository = MemoryExecutionTraceRepository()
        self._failed = False

    async def append(self, event: ExecutionTraceEventDraft):
        if not self._failed and event.source_event_id == "provider-run-1:stage-1":
            self._failed = True
            raise RuntimeError("transient trace store failure")
        return await self._repository.append(event)

    async def list_events(self, query: ExecutionTraceQuery, *, after_offset: int = 0):
        return await self._repository.list_events(query, after_offset=after_offset)
