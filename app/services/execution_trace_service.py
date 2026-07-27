import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.repositories.execution_traces import (
    ExecutionTraceRepository,
    ExecutionTraceSourceConflict,
)
from app.schemas.execution_traces import (
    ExecutionTraceEvent,
    ExecutionTraceEventDraft,
    ExecutionTraceQuery,
    ExecutionTraceRecoveredMemoryRevision,
    ExecutionTraceRecoveredState,
    ExecutionTraceSnapshot,
    ExecutionTraceWriteResult,
)

ExecutionTraceConflict = ExecutionTraceSourceConflict


@dataclass(frozen=True)
class _TraceGap:
    event: ExecutionTraceEventDraft
    failure_id: str


class ExecutionTraceService:
    def __init__(
        self,
        repository: ExecutionTraceRepository,
        *,
        canonical_turns=None,
        memory_items=None,
        index_operations=None,
    ) -> None:
        self.repository = repository
        self.canonical_turns = canonical_turns
        self.memory_items = memory_items
        self.index_operations = index_operations
        self._pending_gaps: dict[tuple[str, str, str, str], dict[str, _TraceGap]] = {}
        self._reported_gaps: dict[tuple[str, str, str, str], dict[str, _TraceGap]] = {}
        self._gap_lock = asyncio.Lock()

    async def record(self, event: ExecutionTraceEventDraft) -> ExecutionTraceWriteResult:
        stored, created = await self.repository.append(event)
        return ExecutionTraceWriteResult(event=stored, created=created)

    async def try_record(self, event: ExecutionTraceEventDraft) -> bool:
        """Project an observation fact without changing the business outcome on failure."""
        scope = _scope_for(event)
        failure_id = _failure_id_for(event)
        try:
            await self.record(event)
        except ExecutionTraceConflict:
            raise
        except Exception:
            await self._remember_gap(scope, failure_id, event)
            return False

        await self._record_repair_if_needed(scope, failure_id)
        await self._flush_pending_gaps(scope)
        await self._record_repair_if_needed(scope, failure_id)
        snapshot = await self.snapshot(_query_for(event))
        return snapshot.completeness == "complete"

    async def snapshot(self, query: ExecutionTraceQuery) -> ExecutionTraceSnapshot:
        events = await self.repository.list_events(query)
        snapshot = _snapshot(events)
        pending = await self._pending_reason_codes(_scope_for(query))
        if pending:
            snapshot = snapshot.model_copy(
                update={
                    "completeness": "incomplete",
                    "incomplete_reason_codes": sorted(
                        set((*snapshot.incomplete_reason_codes, *pending))
                    ),
                }
            )
        return await self._with_recovered_state(query, snapshot)

    async def events_after(
        self, query: ExecutionTraceQuery, *, after_offset: int
    ) -> list[ExecutionTraceEvent]:
        return await self.repository.list_events(query, after_offset=after_offset)

    async def stream(
        self,
        query: ExecutionTraceQuery,
        *,
        after_offset: int,
        poll_interval_seconds: float = 0.25,
    ) -> AsyncIterator[ExecutionTraceEvent]:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        cursor = after_offset
        while True:
            events = await self.events_after(query, after_offset=cursor)
            if events:
                for event in events:
                    cursor = event.event_offset
                    yield event
                continue
            await asyncio.sleep(poll_interval_seconds)

    async def _remember_gap(
        self,
        scope: tuple[str, str, str, str],
        failure_id: str,
        event: ExecutionTraceEventDraft,
    ) -> None:
        async with self._gap_lock:
            self._pending_gaps.setdefault(scope, {}).setdefault(
                failure_id,
                _TraceGap(event=event.model_copy(deep=True), failure_id=failure_id),
            )

    async def _record_repair_if_needed(
        self,
        scope: tuple[str, str, str, str],
        failure_id: str,
    ) -> None:
        async with self._gap_lock:
            gap = self._reported_gaps.get(scope, {}).get(failure_id)
        if gap is None:
            return
        repair = _integrity_event(gap, stage="source_repaired", status="completed", recovered=True)
        try:
            await self.record(repair)
        except Exception:
            return
        async with self._gap_lock:
            self._reported_gaps.get(scope, {}).pop(failure_id, None)

    async def _flush_pending_gaps(self, scope: tuple[str, str, str, str]) -> None:
        async with self._gap_lock:
            gaps = list(self._pending_gaps.get(scope, {}).values())
        for gap in gaps:
            integrity = _integrity_event(
                gap,
                stage="projection_write_failed",
                status="incomplete",
                recovered=False,
            )
            try:
                await self.record(integrity)
            except Exception:
                return
            async with self._gap_lock:
                self._pending_gaps.get(scope, {}).pop(gap.failure_id, None)
                self._reported_gaps.setdefault(scope, {})[gap.failure_id] = gap

    async def _pending_reason_codes(self, scope: tuple[str, str, str, str]) -> list[str]:
        async with self._gap_lock:
            return ["trace_projection_write_failed"] * len(self._pending_gaps.get(scope, {}))

    async def _with_recovered_state(
        self,
        query: ExecutionTraceQuery,
        snapshot: ExecutionTraceSnapshot,
    ) -> ExecutionTraceSnapshot:
        snapshot = await self._with_recovered_memory_state(query, snapshot)
        if snapshot.completeness != "incomplete" or self.canonical_turns is None:
            return snapshot
        try:
            turn = await self.canonical_turns.get_turn(
                turn_id=query.turn_id,
                tenant_id=query.tenant_id,
                user_id=query.user_id,
            )
        except Exception:
            return snapshot
        if turn is None or turn.session_id != query.session_id or not turn.status.is_terminal:
            return snapshot
        outcome = turn.final_response.kind if turn.final_response is not None else None
        return snapshot.model_copy(
            update={
                "recovered": True,
                "recovered_state": ExecutionTraceRecoveredState(
                    status=turn.status.value,
                    outcome=outcome,
                    state_version=turn.state_version,
                ),
            }
        )

    async def _with_recovered_memory_state(
        self,
        query: ExecutionTraceQuery,
        snapshot: ExecutionTraceSnapshot,
    ) -> ExecutionTraceSnapshot:
        if self.memory_items is None or self.index_operations is None:
            return snapshot
        latest_by_revision: dict[tuple[str, str], ExecutionTraceEvent] = {}
        for event in snapshot.events:
            if event.event_type != "memory_revision":
                continue
            memory_id = event.facts.get("memory_id")
            revision_id = event.facts.get("revision_id")
            if isinstance(memory_id, str) and isinstance(revision_id, str):
                latest_by_revision[(memory_id, revision_id)] = event
        recovered: list[ExecutionTraceRecoveredMemoryRevision] = []
        for (memory_id, revision_id), event in latest_by_revision.items():
            try:
                item = await self.memory_items.get_by_id(memory_id, tenant_id=query.tenant_id)
                if (
                    item is None
                    or item.user_id != query.user_id
                    or item.current_revision_id != revision_id
                    or item.index_status is None
                ):
                    continue
                operations = await self.index_operations.list_for_memory(
                    memory_id,
                    tenant_id=query.tenant_id,
                    limit=100,
                )
            except Exception:
                continue
            operation = next(
                (value for value in operations if value.revision_id == revision_id),
                None,
            )
            index_status = item.index_status.value
            operation_status = operation.status.value if operation is not None else None
            if (
                event.facts.get("index_status") == index_status
                and event.facts.get("index_operation_status") == operation_status
            ):
                continue
            operation_name = (
                operation.operation.value
                if operation is not None
                else str(event.facts.get("operation") or "unknown")
            )
            recovered.append(
                ExecutionTraceRecoveredMemoryRevision(
                    memory_id=memory_id,
                    revision_id=revision_id,
                    operation=operation_name,
                    index_status=index_status,
                    index_operation_status=operation_status,
                )
            )
        if not recovered:
            return snapshot
        return snapshot.model_copy(
            update={
                "recovered": True,
                "recovered_memory_revisions": recovered,
            }
        )


def _snapshot(events: list[ExecutionTraceEvent]) -> ExecutionTraceSnapshot:
    if not events:
        return ExecutionTraceSnapshot()
    open_failures: dict[str, str] = {}
    for event in events:
        if event.event_type != "trace_integrity":
            continue
        failure_id = str(event.facts.get("failure_id", ""))
        if event.status == "incomplete":
            open_failures[failure_id] = event.reason_code or str(
                event.facts.get("reason_code", "trace_incomplete")
            )
        elif event.facts.get("recovered") is True:
            open_failures.pop(failure_id, None)
    return ExecutionTraceSnapshot(
        trace_id=events[0].trace_id,
        events=events,
        watermark=events[-1].event_offset,
        completeness="incomplete" if open_failures else "complete",
        incomplete_reason_codes=sorted(set(open_failures.values())),
    )


def _scope_for(
    event: ExecutionTraceEventDraft | ExecutionTraceQuery,
) -> tuple[str, str, str, str]:
    return (
        event.tenant_id,
        event.user_id,
        event.session_id,
        event.turn_id,
    )


def _query_for(event: ExecutionTraceEventDraft) -> ExecutionTraceQuery:
    return ExecutionTraceQuery(
        tenant_id=event.tenant_id,
        user_id=event.user_id,
        session_id=event.session_id,
        turn_id=event.turn_id,
    )


def _failure_id_for(event: ExecutionTraceEventDraft) -> str:
    source_identity = "\x1f".join((event.source, event.source_event_id, str(event.source_version)))
    return hashlib.sha256(source_identity.encode("utf-8")).hexdigest()[:32]


def _integrity_event(
    gap: _TraceGap,
    *,
    stage: str,
    status: str,
    recovered: bool,
) -> ExecutionTraceEventDraft:
    event = gap.event
    return ExecutionTraceEventDraft(
        trace_id=event.trace_id,
        tenant_id=event.tenant_id,
        user_id=event.user_id,
        session_id=event.session_id,
        turn_id=event.turn_id,
        run_id=event.run_id,
        event_type="trace_integrity",
        stage=stage,
        status=status,
        source="oir:trace_integrity",
        source_event_id=f"trace-integrity:{gap.failure_id}:{stage}",
        reason_code="trace_projection_write_failed",
        facts={
            "reason_code": "trace_projection_write_failed",
            "failed_source": event.source,
            "failure_id": gap.failure_id,
            "recovered": recovered,
        },
        occurred_at=event.occurred_at,
    )
