from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import exists, func, select, text

from app.core.redaction import redact_text, redact_value
from app.db.models import (
    CanonicalTurnModel,
    MemoryEventModel,
    MemoryFormationJobModel,
    MemoryFormationTurnModel,
    MemoryIndexOperationModel,
    MemoryItemModel,
    TurnOutboxModel,
)
from app.repositories.json_utils import loads
from app.repositories.memory_revisions import DatabaseMemoryRevisionLedgerRepository
from app.schemas.memory import (
    MemoryDebugResponse,
    MemoryEvent,
    MemoryFormationDecisionView,
    MemoryFormationJobView,
    MemoryFormationTrace,
    MemoryFormationTraceView,
    MemoryItem,
    MemoryMetricsResponse,
    MemoryRequestTraceView,
    MemoryRevisionView,
    MemoryRuntimeHealth,
    MemoryTraceLinks,
)

_ASSOCIATION_FILTERS = {
    "request_id",
    "session_id",
    "turn_id",
    "run_id",
    "formation_job_id",
    "memory_key",
    "decision_status",
}
_MODEL_USAGE_KEYS = {
    "cached_tokens",
    "completion_tokens",
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "reasoning_tokens",
    "total_tokens",
}
_METRIC_SNAPSHOT_LIMIT = 10_000


class MemoryObservabilityService:
    def __init__(
        self,
        *,
        settings,
        memory_service,
        formation_repository,
        trace_repository,
        runtime_status=None,
        maintenance_status=None,
        turn_repository=None,
        outbox_repository=None,
    ) -> None:
        self.settings = settings
        self.memory_service = memory_service
        self.memory_repository = memory_service.repository
        self.index_repository = memory_service.index_outbox
        self.formation_repository = formation_repository
        self.trace_repository = trace_repository
        self.runtime_status = runtime_status
        self.maintenance_status = maintenance_status
        self.turn_repository = turn_repository
        self.outbox_repository = outbox_repository
        revision_repository = getattr(memory_service.lifecycle_store, "revision_repository", None)
        if revision_repository is None:
            session_factory = getattr(memory_service.lifecycle_store, "session_factory", None)
            revision_repository = DatabaseMemoryRevisionLedgerRepository(session_factory)
        self.revision_repository = revision_repository

    async def debug_state(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        agent_id: str | None = None,
        scopes: list[str] | None = None,
        memory_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        formation_job_id: str | None = None,
        memory_key: str | None = None,
        decision_status: str | None = None,
        limit: int = 50,
    ) -> MemoryDebugResponse:
        limit = max(1, min(int(limit), 100))
        scopes = list(dict.fromkeys(scopes or []))[:5] or None
        event_filters = {
            "memory_id": memory_id,
            "request_id": request_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "run_id": run_id,
            "formation_job_id": formation_job_id,
            "memory_key": memory_key,
            "decision_status": decision_status,
        }
        scope_filters = list(dict.fromkeys(scopes or [None]))
        event_groups = await asyncio.gather(
            *(
                self.memory_repository.list_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    scope=scope,
                    limit=limit,
                    **event_filters,
                )
                for scope in scope_filters
            )
        )
        events = sorted(
            {event.event_id: event for group in event_groups for event in group}.values(),
            key=lambda event: event.created_at,
            reverse=True,
        )[:limit]
        if turn_id and user_id and decision_status is None:
            turns = await self.formation_repository.list_turns_by_ids(
                [turn_id], tenant_id=tenant_id, user_id=user_id
            )
            if turns:
                turn = turns[0]
                association_matches = (
                    (request_id is None or request_id == turn.request_id)
                    and (session_id is None or session_id == turn.session_id)
                    and (run_id is None or run_id == turn.run_id)
                    and (formation_job_id is None or formation_job_id == turn.claimed_job_id)
                )
                if association_matches:
                    linked_source_ids = {
                        event.payload.get("source_event_id")
                        for event in events
                        if event.event_type == "memory_recall_linked"
                        and isinstance(event.payload, dict)
                    }
                    router_events = await self.memory_repository.list_events(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        agent_id=agent_id,
                        memory_id=memory_id,
                        request_id=turn.request_id,
                        session_id=turn.session_id,
                        memory_key=memory_key,
                        scope=(scopes or [None])[0] if len(scopes or []) <= 1 else None,
                        limit=limit,
                    )
                    associated = [
                        event.model_copy(update={"turn_id": turn_id})
                        for event in router_events
                        if event.event_type == "memory_recall_used"
                        and event.turn_id is None
                        and event.event_id not in linked_source_ids
                        and (not scopes or event.scope in scopes)
                    ]
                    events = sorted(
                        {event.event_id: event for event in [*events, *associated]}.values(),
                        key=lambda event: event.created_at,
                        reverse=True,
                    )[:limit]
        trace_groups = await asyncio.gather(
            *(
                self.trace_repository.list_traces(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    scope=scope,
                    request_id=request_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    formation_job_id=formation_job_id,
                    memory_id=memory_id,
                    memory_key=memory_key,
                    decision_status=decision_status,
                    limit=limit,
                )
                for scope in scope_filters
            )
        )
        traces = sorted(
            {trace.job.job_id: trace for group in trace_groups for trace in group}.values(),
            key=lambda trace: trace.job.updated_at,
            reverse=True,
        )[:limit]
        items = await self._filtered_items(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            scopes=scopes,
            memory_id=memory_id,
            events=events,
            traces=traces,
            association_filtered=any(
                value
                for name, value in {
                    "request_id": request_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "run_id": run_id,
                    "formation_job_id": formation_job_id,
                    "memory_key": memory_key,
                    "decision_status": decision_status,
                }.items()
                if name in _ASSOCIATION_FILTERS
            ),
            limit=limit,
        )
        safe_traces = list(
            await asyncio.gather(*(self._trace_view(trace, events=events) for trace in traces))
        )
        revisions = []
        for item in items:
            if not item.tenant_id or not item.user_id:
                continue
            values = await self.revision_repository.list_for_memory(
                item.memory_id,
                tenant_id=item.tenant_id,
                user_id=item.user_id,
                subject_type=item.subject_type,
                subject_id=item.subject_id,
                limit=limit,
            )
            revisions.extend(_safe_revision(value) for value in values)
        links = _context_trace_links(events)
        request_trace = await self._request_trace(
            request_id=request_id,
            tenant_id=tenant_id,
            user_id=user_id,
            traces=safe_traces,
            items=items,
            events=events,
        )
        return MemoryDebugResponse(
            items=[_safe_item(item) for item in items],
            revisions=revisions[:limit],
            events=[_safe_event(event) for event in events],
            formation_traces=safe_traces,
            context_trace_links=links,
            request_trace=request_trace,
            metadata={
                "memory_enabled": self.settings.memory_enabled,
                "strategy_provider": self.settings.memory_strategy_provider,
                "item_count": len(items),
                "event_count": len(events),
                "revision_count": len(revisions[:limit]),
                "formation_trace_count": len(safe_traces),
                "trace_models": {
                    "context": "context_trace",
                    "formation": "formation_trace",
                    "linked_by": ["request_id", "session_id", "turn_id", "run_id", "memory_id"],
                },
            },
        )

    async def _request_trace(
        self,
        *,
        request_id: str | None,
        tenant_id: str,
        user_id: str | None,
        traces: list[MemoryFormationTraceView],
        items: list[MemoryItem],
        events: list[MemoryEvent],
    ) -> MemoryRequestTraceView | None:
        if not request_id or not user_id or self.turn_repository is None:
            return None
        turn = await self.turn_repository.get_by_request(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        if turn is None:
            return MemoryRequestTraceView(
                request_id=request_id,
                overall_stage="trace_missing",
                retryable=True,
                reason_code="canonical_turn_missing",
            )
        outboxes = await self._turn_outboxes(turn.turn_id)
        formation_turns = await self._formation_turns(request_id, tenant_id, user_id)
        memory_ids = sorted(
            {
                *[item.memory_id for item in items],
                *[memory_id for trace in traces for memory_id in trace.links.memory_ids],
            }
        )[:100]
        revision_ids = sorted(
            {
                *[item.current_revision_id for item in items if item.current_revision_id],
                *[revision_id for trace in traces for revision_id in trace.revision_ids],
            }
        )[:100]
        index_operations = []
        for memory_id in memory_ids:
            index_operations.extend(
                await self.index_repository.list_for_memory(
                    memory_id, tenant_id=tenant_id, limit=20
                )
            )
        stage, terminal, retryable, reason = _request_trace_stage(
            turn_status=turn.status.value,
            outboxes=outboxes,
            formation_turns=formation_turns,
            traces=traces,
            items=items,
            index_operations=index_operations,
            events=events,
        )
        timestamps = [turn.updated_at]
        timestamps.extend(value.updated_at for value in outboxes if value.updated_at)
        timestamps.extend(trace.job.updated_at for trace in traces)
        return MemoryRequestTraceView(
            request_id=request_id,
            overall_stage=stage,
            terminal=terminal,
            retryable=retryable,
            reason_code=_safe_error(reason),
            turn_id=turn.turn_id,
            turn_status=turn.status.value,
            run_ids=list(turn.references.run_ids[:100]),
            result_ids=list(turn.references.result_ids[:100]),
            outbox_ids=[value.outbox_id for value in outboxes[:100]],
            formation_turn_ids=[value.turn_id for value in formation_turns[:100]],
            formation_job_ids=[trace.job.job_id for trace in traces[:100]],
            memory_ids=memory_ids,
            revision_ids=revision_ids,
            index_operation_ids=sorted({value.index_operation_id for value in index_operations})[
                :100
            ],
            updated_at=max(timestamps) if timestamps else None,
        )

    async def _turn_outboxes(self, turn_id: str):
        if self.outbox_repository is None:
            return []
        values = getattr(self.outbox_repository, "events", None)
        if values is not None:
            return sorted(
                [
                    event.model_copy(deep=True)
                    for event in values.values()
                    if event.turn_id == turn_id
                ],
                key=lambda event: event.created_at or event.available_at,
            )
        session_factory = getattr(self.outbox_repository, "session_factory", None)
        if session_factory is None:
            return []
        from app.repositories.turn_outbox import _event_from_row

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(TurnOutboxModel).where(TurnOutboxModel.turn_id == turn_id)
                    )
                )
                .scalars()
                .all()
            )
            return [_event_from_row(row) for row in rows]

    async def _formation_turns(self, request_id: str, tenant_id: str, user_id: str):
        values = getattr(self.formation_repository, "turns", None)
        if values is not None:
            return [
                turn.model_copy(deep=True)
                for turn in values.values()
                if turn.request_id == request_id
                and turn.tenant_id == tenant_id
                and turn.user_id == user_id
            ]
        session_factory = getattr(self.formation_repository, "session_factory", None)
        if session_factory is None:
            return []
        from app.repositories.memory_formation import _turn_from_row

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryFormationTurnModel).where(
                            MemoryFormationTurnModel.request_id == request_id,
                            MemoryFormationTurnModel.tenant_id == tenant_id,
                            MemoryFormationTurnModel.user_id == user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [_turn_from_row(row) for row in rows]

    async def health(self) -> MemoryRuntimeHealth:
        snapshot = await self._snapshot()
        return MemoryRuntimeHealth(
            worker_state=_worker_state(self.runtime_status, self.maintenance_status, self.settings),
            pending_turn_count=snapshot["pending_turn_count"],
            outbox_pending_count=snapshot["outbox_pending_count"],
            outbox_oldest_pending_seconds=snapshot["outbox_oldest_pending_seconds"],
            trace_missing_count=snapshot["trace_missing_count"],
            queue_depth=snapshot["queue_depth"],
            oldest_pending_seconds=snapshot["oldest_pending_seconds"],
            dead_letter_count=snapshot["formation_dead_letters"],
            index_out_of_sync_count=snapshot["index_out_of_sync"],
            index_dead_letter_count=snapshot["index_dead_letters"],
            deletion_pending_count=snapshot["deletion_pending"],
            last_safe_error=(
                _safe_error(self.runtime_status.last_error_code)
                if self.runtime_status is not None and self.runtime_status.last_error_code
                else _safe_error(self.maintenance_status.last_error_code)
                if self.maintenance_status is not None and self.maintenance_status.last_error_code
                else snapshot["last_safe_error"]
            ),
        )

    async def metrics(self) -> MemoryMetricsResponse:
        pipeline = await self._canonical_pipeline_snapshot()
        session_factory = getattr(self.formation_repository, "session_factory", None)
        if session_factory is not None:
            response = await _database_metrics(session_factory)
            return response.model_copy(update=pipeline)
        data = await self._metric_rows()
        formation_series = _formation_metric_series(data)
        job_counts = Counter(f"{row['trigger']}:{row['status']}" for row in data["jobs"])
        job_scope_counts = Counter(
            f"{row['trigger']}:{scope}:{row['status']}"
            for row in data["jobs"]
            for scope in _metric_scopes(row["trace_summary"])
        )
        decision_counts = Counter(
            f"{row['event_type'].removeprefix('memory_decision_')}:{row['decision_status']}"
            for row in data["events"]
            if row["event_type"].startswith("memory_decision_") and row["decision_status"]
        )
        index_counts = Counter(row["status"] for row in data["index_operations"])
        usage_totals: defaultdict[str, int | float] = defaultdict(int)
        model_latency_total = 0
        job_latency_total = 0
        queue_latency_total = 0
        for row in data["jobs"]:
            summary = row["trace_summary"]
            latency = summary.get("model_latency_ms")
            if isinstance(latency, int) and latency >= 0:
                model_latency_total += latency
            job_latency = summary.get("job_latency_ms")
            if isinstance(job_latency, int) and job_latency >= 0:
                job_latency_total += job_latency
            queue_latency = summary.get("queue_latency_ms")
            if isinstance(queue_latency, int) and queue_latency >= 0:
                queue_latency_total += queue_latency
            usage = summary.get("usage")
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if key not in _MODEL_USAGE_KEYS:
                        continue
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        usage_totals[key] += value
        recall_used = 0
        deletion_completions = 0
        index_repairs = 0
        index_repair_latency = 0
        index_repaired_records = 0
        index_orphan_records = 0
        deletion_failures = 0
        deletion_dead_letters = 0
        for row in data["events"]:
            payload = row["payload"]
            if row["event_type"] == "memory_recall_used":
                count = payload.get("used_count")
                if isinstance(count, int) and count >= 0:
                    recall_used += count
            if row["event_type"] == "memory_deleted_tombstone":
                deletion_completions += 1
            if row["event_type"] in {"memory_deletion_retry", "memory_deletion_dead_letter"}:
                deletion_failures += 1
                deletion_dead_letters += int(row["event_type"] == "memory_deletion_dead_letter")
            if row["event_type"] == "memory_index_repair":
                index_repairs += 1
                latency = payload.get("latency_ms")
                if isinstance(latency, int) and latency >= 0:
                    index_repair_latency += latency
                index_repaired_records += sum(
                    value
                    for key in ("added_count", "updated_count", "adopted_count")
                    if isinstance((value := payload.get(key)), int) and value >= 0
                )
                orphan = payload.get("orphan_deleted_count")
                if isinstance(orphan, int) and orphan >= 0:
                    index_orphan_records += orphan
        pending = [
            row["created_at"]
            for row in data["jobs"]
            if row["status"] in {"pending", "claimed", "retry"}
        ]
        decision_total = sum(decision_counts.values())
        return MemoryMetricsResponse(
            aggregation_mode="bounded_snapshot",
            snapshot_limit=_METRIC_SNAPSHOT_LIMIT,
            jobs_by_trigger_status=dict(sorted(job_counts.items())),
            jobs_by_trigger_scope_status=dict(sorted(job_scope_counts.items())),
            formation_series=formation_series,
            decisions_by_operation_status=dict(sorted(decision_counts.items())),
            decision_rates={
                key: count / decision_total for key, count in sorted(decision_counts.items())
            }
            if decision_total
            else {},
            retries=sum(max(row["attempt_count"] - 1, 0) for row in data["jobs"]),
            dead_letters=sum(row["status"] == "dead_letter" for row in data["jobs"]),
            queue_latency_ms_total=queue_latency_total,
            job_latency_ms_total=job_latency_total,
            model_latency_ms_total=model_latency_total,
            model_usage_total=dict(sorted(usage_totals.items())),
            index_operations_by_status=dict(sorted(index_counts.items())),
            index_out_of_sync_count=sum(
                row["index_status"] == "out_of_sync" for row in data["items"]
            ),
            index_operation_latency_ms_total=sum(
                value
                for row in data["index_operations"]
                if isinstance(value := row["metadata"].get("provider_latency_ms"), int)
                and value >= 0
            ),
            index_repairs=index_repairs,
            index_repaired_records=index_repaired_records,
            index_orphan_records=index_orphan_records,
            index_repair_latency_ms_total=index_repair_latency,
            deletion_completions=deletion_completions,
            deletion_failures=deletion_failures,
            deletion_dead_letters=deletion_dead_letters,
            deletion_oldest_pending_seconds=_oldest_age(
                [
                    row["updated_at"]
                    for row in data["items"]
                    if row["lifecycle_status"] == "deletion_pending"
                ]
            ),
            recall_used=recall_used,
            oldest_pending_seconds=_oldest_age(pending),
            **pipeline,
        )

    async def _canonical_pipeline_snapshot(self) -> dict:
        empty = {
            "pending_turn_count": 0,
            "outbox_pending_count": 0,
            "outbox_oldest_pending_seconds": None,
            "trace_missing_count": 0,
        }
        if self.turn_repository is None or self.outbox_repository is None:
            return empty
        session_factory = getattr(self.turn_repository, "session_factory", None)
        if session_factory is not None:
            async with session_factory() as session:
                pending_turn_count = await session.scalar(
                    select(func.count())
                    .select_from(CanonicalTurnModel)
                    .where(
                        CanonicalTurnModel.status.in_(("pending", "routing", "running", "blocked"))
                    )
                )
                outbox_pending_count = await session.scalar(
                    select(func.count())
                    .select_from(TurnOutboxModel)
                    .where(TurnOutboxModel.status.in_(("pending", "claimed", "retry")))
                )
                oldest_outbox = await session.scalar(
                    select(func.min(TurnOutboxModel.available_at)).where(
                        TurnOutboxModel.status.in_(("pending", "claimed", "retry"))
                    )
                )
                trace_missing_count = await session.scalar(
                    select(func.count())
                    .select_from(CanonicalTurnModel)
                    .where(
                        CanonicalTurnModel.status == "completed",
                        ~exists(
                            select(TurnOutboxModel.outbox_id).where(
                                TurnOutboxModel.turn_id == CanonicalTurnModel.turn_id
                            )
                        ),
                    )
                )
            return {
                "pending_turn_count": int(pending_turn_count or 0),
                "outbox_pending_count": int(outbox_pending_count or 0),
                "outbox_oldest_pending_seconds": (
                    _oldest_age([oldest_outbox]) if oldest_outbox is not None else None
                ),
                "trace_missing_count": int(trace_missing_count or 0),
            }

        turns = getattr(self.turn_repository, "turns", {})
        outboxes = getattr(self.outbox_repository, "events", {})
        active_outboxes = [
            event for event in outboxes.values() if event.status in {"pending", "claimed", "retry"}
        ]
        outbox_turn_ids = {event.turn_id for event in outboxes.values()}
        return {
            "pending_turn_count": sum(
                turn.status.value in {"pending", "routing", "running", "blocked"}
                for turn in turns.values()
            ),
            "outbox_pending_count": len(active_outboxes),
            "outbox_oldest_pending_seconds": _oldest_age(
                [event.available_at for event in active_outboxes]
            ),
            "trace_missing_count": sum(
                turn.status.value == "completed" and turn.turn_id not in outbox_turn_ids
                for turn in turns.values()
            ),
        }

    async def _filtered_items(
        self,
        *,
        tenant_id,
        user_id,
        agent_id,
        scopes,
        memory_id,
        events,
        traces,
        association_filtered,
        limit,
    ) -> list[MemoryItem]:
        if memory_id:
            item = await self.memory_repository.get_by_id(memory_id, tenant_id=tenant_id)
            candidates = [item] if item is not None else []
        elif association_filtered:
            ids = {event.memory_id for event in events if event.memory_id}
            ids.update(
                operation.memory_id
                for trace in traces
                for operation in trace.operations
                if operation.memory_id
            )
            candidates = []
            for item_id in sorted(ids)[:limit]:
                item = await self.memory_repository.get_by_id(item_id, tenant_id=tenant_id)
                if item is not None:
                    candidates.append(item)
        else:
            candidates = await self.memory_repository.list_active(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                scopes=scopes,
                lifecycle_statuses=["active", "deletion_pending"],
                limit=limit,
            )
        return [
            item
            for item in candidates
            if (user_id is None or item.user_id == user_id)
            and (agent_id is None or item.agent_id in {None, agent_id})
            and (not scopes or str(item.scope) in set(scopes))
        ][:limit]

    async def _trace_view(self, trace: MemoryFormationTrace, *, events) -> MemoryFormationTraceView:
        provider_by_memory = {}
        for event in events:
            if event.memory_id and isinstance(event.payload, dict):
                status = event.payload.get("provider_status")
                if isinstance(status, str):
                    provider_by_memory[event.memory_id] = status[:64]
        item_by_memory = {}
        index_by_memory = {}
        for memory_id in sorted(
            {operation.memory_id for operation in trace.operations if operation.memory_id}
        ):
            item_by_memory[memory_id] = await self.memory_repository.get_by_id(
                memory_id, tenant_id=trace.job.tenant_id
            )
            index_by_memory[memory_id] = await self.index_repository.list_for_memory(
                memory_id, tenant_id=trace.job.tenant_id, limit=10
            )
        decisions = []
        provider_latencies = []
        decision_events = {}
        for event in events:
            if not event.event_type.startswith("memory_decision_"):
                continue
            payload = event.payload if isinstance(event.payload, dict) else {}
            event_operation = payload.get("operation")
            if not isinstance(event_operation, dict):
                continue
            operation_id = event_operation.get("operation_id")
            if isinstance(operation_id, str):
                decision_events[operation_id] = event
        for operation in trace.operations[:100]:
            metadata = operation.metadata if isinstance(operation.metadata, dict) else {}
            preview = metadata.get("content_preview")
            decision_event = decision_events.get(operation.operation_id)
            decision_payload = (
                decision_event.payload
                if decision_event is not None and isinstance(decision_event.payload, dict)
                else {}
            )
            pending_candidate = decision_payload.get("pending_candidate")
            proposed_operation = (
                pending_candidate.get("proposed_operation")
                if isinstance(pending_candidate, dict)
                else None
            )
            index_operations = index_by_memory.get(operation.memory_id, [])
            latest_index = next(
                (
                    value
                    for value in index_operations
                    if operation.revision_id is None or value.revision_id == operation.revision_id
                ),
                index_operations[0] if index_operations else None,
            )
            item = item_by_memory.get(operation.memory_id)
            index_status = _index_status(item, latest_index)
            if latest_index is not None:
                latency = latest_index.last_error_metadata.get("provider_latency_ms")
                if isinstance(latency, int) and latency >= 0:
                    provider_latencies.append(latency)
            decisions.append(
                MemoryFormationDecisionView(
                    decision_id=decision_event.event_id if decision_event is not None else None,
                    operation_id=operation.operation_id,
                    operation=operation.operation,
                    proposed_operation=(
                        proposed_operation if proposed_operation in {"update", "delete"} else None
                    ),
                    decision_status=operation.decision_status,
                    reason_code=operation.reason_code,
                    scope=_bounded_optional(metadata.get("scope"), 64),
                    memory_key=operation.memory_key,
                    memory_id=operation.memory_id,
                    revision_id=operation.revision_id,
                    canonical_refs=list(operation.canonical_refs[:100]),
                    content_preview=(
                        redact_text(preview, max_length=300) if isinstance(preview, str) else None
                    ),
                    content_redacted=bool(metadata.get("content_redacted"))
                    or preview == "[redacted]",
                    index_status=index_status,
                    provider_status=(
                        _provider_status(latest_index)
                        or provider_by_memory.get(operation.memory_id)
                    ),
                )
            )
        memory_ids = sorted(
            {operation.memory_id for operation in trace.operations if operation.memory_id}
        )[:100]
        links = MemoryTraceLinks(
            request_ids=list(trace.request_ids[:100]),
            session_id=trace.job.session_id,
            turn_ids=list(trace.turn_ids[:100]),
            run_ids=list(trace.run_ids[:100]),
            memory_ids=memory_ids,
        )
        return MemoryFormationTraceView(
            job=MemoryFormationJobView(
                job_id=trace.job.job_id,
                trigger=trace.job.trigger,
                status=trace.job.status,
                mode=trace.job.mode,
                first_turn_id=trace.job.first_turn_id,
                last_turn_id=trace.job.last_turn_id,
                source_refs=list(trace.job.source_refs[:100]),
                model_version=trace.job.model_version,
                prompt_version=trace.job.prompt_version,
                policy_version=trace.job.policy_version,
                attempt_count=trace.job.attempt_count,
                max_attempts=trace.job.max_attempts,
                last_error_code=_safe_error(trace.job.last_error_code),
                created_at=trace.job.created_at,
                updated_at=trace.job.updated_at,
            ),
            links=links,
            scopes=list(trace.scopes[:50]),
            decisions=decisions,
            candidate_count=trace.candidate_count,
            decision_counts={
                str(key)[:64]: int(value)
                for key, value in trace.decision_counts.items()
                if isinstance(value, int) and value >= 0
            },
            semantic_contract_version=trace.semantic_contract_version,
            semantic_validation_counts=dict(trace.semantic_validation_counts),
            semantic_verifier_counts=dict(trace.semantic_verifier_counts),
            revision_ids=sorted(
                {operation.revision_id for operation in trace.operations if operation.revision_id}
            )[:100],
            model_latency_ms=trace.model_latency_ms,
            provider_latency_ms=(
                trace.provider_latency_ms
                if trace.provider_latency_ms is not None
                else sum(provider_latencies)
                if provider_latencies
                else None
            ),
            usage=_numeric_usage(trace.usage),
        )

    async def _snapshot(self) -> dict:
        pipeline = await self._canonical_pipeline_snapshot()
        session_factory = getattr(self.formation_repository, "session_factory", None)
        if session_factory is not None:
            return {**(await _database_health_snapshot(session_factory)), **pipeline}
        data = await self._metric_rows()
        pending = [
            row["created_at"]
            for row in data["jobs"]
            if row["status"] in {"pending", "claimed", "retry"}
        ]
        errors = [
            (row["updated_at"], row["last_error_code"])
            for row in [*data["jobs"], *data["index_operations"]]
            if row["last_error_code"]
        ]
        return {
            "queue_depth": len(pending),
            "oldest_pending_seconds": _oldest_age(pending),
            "formation_dead_letters": sum(row["status"] == "dead_letter" for row in data["jobs"]),
            "index_out_of_sync": sum(row["index_status"] == "out_of_sync" for row in data["items"]),
            "index_dead_letters": sum(
                row["status"] == "dead_letter" for row in data["index_operations"]
            ),
            "deletion_pending": sum(
                row["lifecycle_status"] == "deletion_pending" for row in data["items"]
            ),
            "last_safe_error": _safe_error(max(errors)[1]) if errors else None,
            **pipeline,
        }

    async def _metric_rows(self) -> dict[str, list[dict]]:
        session_factory = getattr(self.formation_repository, "session_factory", None)
        if session_factory is None:
            return {
                "jobs": [
                    {
                        "job_id": job.job_id,
                        "trigger": job.trigger.value,
                        "status": job.status.value,
                        "attempt_count": job.attempt_count,
                        "trace_summary": job.trace_summary,
                        "last_error_code": job.last_error_code,
                        "created_at": job.created_at,
                        "updated_at": job.updated_at,
                    }
                    for job in list(self.formation_repository.jobs.values())[
                        -_METRIC_SNAPSHOT_LIMIT:
                    ]
                ],
                "index_operations": [
                    {
                        "status": operation.status.value,
                        "metadata": operation.last_error_metadata,
                        "last_error_code": operation.last_error_code,
                        "created_at": operation.created_at,
                        "updated_at": operation.updated_at,
                    }
                    for operation in list(self.index_repository.operations.values())[
                        -_METRIC_SNAPSHOT_LIMIT:
                    ]
                ],
                "items": [
                    {
                        "index_status": item.index_status.value if item.index_status else None,
                        "lifecycle_status": item.lifecycle_status.value,
                        "updated_at": item.updated_at,
                    }
                    for item in list(self.memory_repository.items.values())[
                        -_METRIC_SNAPSHOT_LIMIT:
                    ]
                ],
                "events": [
                    {
                        "event_type": event.event_type,
                        "decision_status": event.decision_status,
                        "formation_job_id": event.formation_job_id,
                        "scope": event.scope,
                        "payload": event.payload,
                    }
                    for event in self.memory_repository.events[-_METRIC_SNAPSHOT_LIMIT:]
                ],
            }
        async with session_factory() as session:
            jobs = (
                (
                    await session.execute(
                        select(MemoryFormationJobModel)
                        .order_by(MemoryFormationJobModel.updated_at.desc())
                        .limit(_METRIC_SNAPSHOT_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            index = (
                (
                    await session.execute(
                        select(MemoryIndexOperationModel)
                        .order_by(MemoryIndexOperationModel.updated_at.desc())
                        .limit(_METRIC_SNAPSHOT_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            items = (
                (
                    await session.execute(
                        select(MemoryItemModel)
                        .order_by(MemoryItemModel.updated_at.desc())
                        .limit(_METRIC_SNAPSHOT_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            events = (
                (
                    await session.execute(
                        select(MemoryEventModel)
                        .order_by(MemoryEventModel.created_at.desc())
                        .limit(_METRIC_SNAPSHOT_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "jobs": [
                {
                    "job_id": row.job_id,
                    "trigger": row.trigger,
                    "status": row.status,
                    "attempt_count": row.attempt_count,
                    "trace_summary": loads(row.trace_summary_text, {}),
                    "last_error_code": row.last_error_code,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in jobs
            ],
            "index_operations": [
                {
                    "status": row.status,
                    "metadata": loads(row.last_error_metadata_text, {}),
                    "last_error_code": row.last_error_code,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in index
            ],
            "items": [
                {
                    "index_status": row.index_status,
                    "lifecycle_status": row.lifecycle_status,
                    "updated_at": row.updated_at,
                }
                for row in items
            ],
            "events": [
                {
                    "event_type": row.event_type,
                    "decision_status": row.decision_status,
                    "formation_job_id": row.formation_job_id,
                    "scope": row.scope,
                    "payload": loads(row.payload_text, {}),
                }
                for row in events
            ],
        }


async def _database_metrics(session_factory) -> MemoryMetricsResponse:
    dialect = session_factory.kw["bind"].dialect.name
    usage_aliases = {key: f"usage_{key}" for key in sorted(_MODEL_USAGE_KEYS)}
    job_usage_sql = "".join(
        f", SUM({_json_non_negative_number('trace_summary_text', f'usage.{key}', dialect)}) "
        f"AS {alias}"
        for key, alias in usage_aliases.items()
    )
    scope_usage_sql = "".join(
        f", SUM({_json_non_negative_number('trace_summary_text', f'usage.{key}', dialect)}) "
        f"AS {alias}"
        for key, alias in usage_aliases.items()
    )
    allowed_scopes = (
        "'user_preference', 'stable_fact', 'task_memory', 'artifact_reference', 'session_summary'"
    )
    scope_cte = _job_scope_cte(dialect=dialect, allowed_scopes=allowed_scopes)
    event_payload = "payload_text"
    index_metadata = "last_error_metadata_text"

    async with session_factory() as session:
        job_rows = (
            (
                await session.execute(
                    text(
                        f"""
                        SELECT trigger, status, COUNT(*) AS job_count,
                               SUM(CASE WHEN attempt_count > 1 THEN attempt_count - 1 ELSE 0 END)
                                   AS retries,
                               SUM(CASE WHEN status = 'dead_letter' THEN 1 ELSE 0 END)
                                   AS dead_letters,
                               SUM({_json_non_negative_int("trace_summary_text", "queue_latency_ms", dialect)})
                                   AS queue_latency_ms_total,
                               SUM({_json_non_negative_int("trace_summary_text", "job_latency_ms", dialect)})
                                   AS job_latency_ms_total,
                               SUM({_json_non_negative_int("trace_summary_text", "model_latency_ms", dialect)})
                                   AS model_latency_ms_total
                               {job_usage_sql}
                        FROM memory_formation_jobs
                        GROUP BY trigger, status
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        scope_rows = (
            (
                await session.execute(
                    text(
                        f"""
                        {scope_cte}
                        SELECT trigger, status, scope, COUNT(*) AS job_count,
                               SUM({_json_non_negative_int("trace_summary_text", "queue_latency_ms", dialect)})
                                   AS queue_latency_ms_total,
                               SUM({_json_non_negative_int("trace_summary_text", "model_latency_ms", dialect)})
                                   AS model_latency_ms_total
                               {scope_usage_sql}
                        FROM job_scopes
                        GROUP BY trigger, status, scope
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        decision_rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT REPLACE(event.event_type, 'memory_decision_', '') AS operation,
                               event.decision_status AS decision_status,
                               COUNT(*) AS decision_count
                        FROM memory_events AS event
                        WHERE event.event_type LIKE 'memory_decision_%'
                          AND event.decision_status IS NOT NULL
                        GROUP BY operation, event.decision_status
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        series_decision_rows = (
            (
                await session.execute(
                    text(
                        f"""
                        SELECT job.trigger AS trigger, job.status AS job_status,
                               CASE WHEN event.scope IN ({allowed_scopes})
                                    THEN event.scope ELSE 'unknown' END AS scope,
                               REPLACE(event.event_type, 'memory_decision_', '') AS operation,
                               event.decision_status AS decision_status,
                               COUNT(*) AS decision_count
                        FROM memory_events AS event
                        JOIN memory_formation_jobs AS job
                          ON job.job_id = event.formation_job_id
                        WHERE event.event_type LIKE 'memory_decision_%'
                          AND event.decision_status IS NOT NULL
                        GROUP BY job.trigger, job.status, scope, operation,
                                 event.decision_status
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        index_rows = (
            (
                await session.execute(
                    text(
                        f"""
                        SELECT status, COUNT(*) AS operation_count,
                               SUM({_json_non_negative_int(index_metadata, "provider_latency_ms", dialect)})
                                   AS provider_latency_ms_total
                        FROM memory_index_operations
                        GROUP BY status
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        item_row = (
            (
                await session.execute(
                    text(
                        """
                    SELECT SUM(CASE WHEN index_status = 'out_of_sync' THEN 1 ELSE 0 END)
                               AS index_out_of_sync_count,
                           MIN(CASE WHEN lifecycle_status = 'deletion_pending'
                                    THEN updated_at END) AS oldest_deletion_pending
                    FROM memory_items
                    """
                    )
                )
            )
            .mappings()
            .one()
        )
        event_row = (
            (
                await session.execute(
                    text(
                        f"""
                    SELECT SUM(CASE WHEN event_type = 'memory_deleted_tombstone'
                                    THEN 1 ELSE 0 END) AS deletion_completions,
                           SUM(CASE WHEN event_type IN (
                                        'memory_deletion_retry', 'memory_deletion_dead_letter'
                                    ) THEN 1 ELSE 0 END) AS deletion_failures,
                           SUM(CASE WHEN event_type = 'memory_deletion_dead_letter'
                                    THEN 1 ELSE 0 END) AS deletion_dead_letters,
                           SUM(CASE WHEN event_type = 'memory_recall_used'
                                    THEN {_json_non_negative_int(event_payload, "used_count", dialect)}
                                    ELSE 0 END) AS recall_used,
                           SUM(CASE WHEN event_type = 'memory_index_repair'
                                    THEN 1 ELSE 0 END) AS index_repairs,
                           SUM(CASE WHEN event_type = 'memory_index_repair'
                                    THEN {_json_non_negative_int(event_payload, "latency_ms", dialect)}
                                    ELSE 0 END) AS index_repair_latency_ms_total,
                           SUM(CASE WHEN event_type = 'memory_index_repair' THEN
                                    {_json_non_negative_int(event_payload, "added_count", dialect)} +
                                    {_json_non_negative_int(event_payload, "updated_count", dialect)} +
                                    {_json_non_negative_int(event_payload, "adopted_count", dialect)}
                                    ELSE 0 END) AS index_repaired_records,
                           SUM(CASE WHEN event_type = 'memory_index_repair'
                                    THEN {_json_non_negative_int(event_payload, "orphan_deleted_count", dialect)}
                                    ELSE 0 END) AS index_orphan_records
                    FROM memory_events
                    """
                    )
                )
            )
            .mappings()
            .one()
        )
        oldest_pending = await session.scalar(
            select(func.min(MemoryFormationJobModel.created_at)).where(
                MemoryFormationJobModel.status.in_(("pending", "claimed", "retry"))
            )
        )

    jobs_by_trigger_status: dict[str, int] = {}
    retries = dead_letters = 0
    queue_latency = job_latency = model_latency = 0
    usage_totals: defaultdict[str, int | float] = defaultdict(int)
    for row in job_rows:
        jobs_by_trigger_status[f"{row['trigger']}:{row['status']}"] = int(row["job_count"])
        retries += int(row["retries"] or 0)
        dead_letters += int(row["dead_letters"] or 0)
        queue_latency += int(row["queue_latency_ms_total"] or 0)
        job_latency += int(row["job_latency_ms_total"] or 0)
        model_latency += int(row["model_latency_ms_total"] or 0)
        _merge_aggregate_usage(usage_totals, row, usage_aliases)

    formation_series: dict[str, dict] = {}
    jobs_by_trigger_scope_status: dict[str, int] = {}
    for row in scope_rows:
        key = f"{row['trigger']}:{row['scope']}:{row['status']}"
        jobs_by_trigger_scope_status[key] = int(row["job_count"])
        value = _empty_metric_series()
        value["job_count"] = int(row["job_count"])
        value["queue_latency_ms_total"] = int(row["queue_latency_ms_total"] or 0)
        value["model_latency_ms_total"] = int(row["model_latency_ms_total"] or 0)
        scoped_usage: defaultdict[str, int | float] = defaultdict(int)
        _merge_aggregate_usage(scoped_usage, row, usage_aliases)
        value["model_usage_total"] = dict(sorted(scoped_usage.items()))
        formation_series[key] = value

    decision_counts = {
        f"{row['operation']}:{row['decision_status']}": int(row["decision_count"])
        for row in decision_rows
    }
    for row in series_decision_rows:
        key = f"{row['trigger']}:{row['scope']}:{row['job_status']}"
        value = formation_series.setdefault(key, _empty_metric_series())
        decision_key = f"{row['operation']}:{row['decision_status']}"
        value["decisions_by_operation_status"][decision_key] = int(row["decision_count"])
    for value in formation_series.values():
        total = sum(value["decisions_by_operation_status"].values())
        value["decisions_by_operation_status"] = dict(
            sorted(value["decisions_by_operation_status"].items())
        )
        value["decision_rates"] = (
            {key: count / total for key, count in value["decisions_by_operation_status"].items()}
            if total
            else {}
        )

    decision_total = sum(decision_counts.values())
    return MemoryMetricsResponse(
        aggregation_mode="full_database",
        snapshot_limit=None,
        jobs_by_trigger_status=dict(sorted(jobs_by_trigger_status.items())),
        jobs_by_trigger_scope_status=dict(sorted(jobs_by_trigger_scope_status.items())),
        formation_series=dict(sorted(formation_series.items())),
        decisions_by_operation_status=dict(sorted(decision_counts.items())),
        decision_rates=(
            {key: count / decision_total for key, count in sorted(decision_counts.items())}
            if decision_total
            else {}
        ),
        retries=retries,
        dead_letters=dead_letters,
        queue_latency_ms_total=queue_latency,
        job_latency_ms_total=job_latency,
        model_latency_ms_total=model_latency,
        model_usage_total=dict(sorted(usage_totals.items())),
        index_operations_by_status=dict(
            sorted((str(row["status"]), int(row["operation_count"])) for row in index_rows)
        ),
        index_out_of_sync_count=int(item_row["index_out_of_sync_count"] or 0),
        index_operation_latency_ms_total=sum(
            int(row["provider_latency_ms_total"] or 0) for row in index_rows
        ),
        index_repairs=int(event_row["index_repairs"] or 0),
        index_repaired_records=int(event_row["index_repaired_records"] or 0),
        index_orphan_records=int(event_row["index_orphan_records"] or 0),
        index_repair_latency_ms_total=int(event_row["index_repair_latency_ms_total"] or 0),
        deletion_completions=int(event_row["deletion_completions"] or 0),
        deletion_failures=int(event_row["deletion_failures"] or 0),
        deletion_dead_letters=int(event_row["deletion_dead_letters"] or 0),
        deletion_oldest_pending_seconds=_oldest_age(
            [item_row["oldest_deletion_pending"]]
            if item_row["oldest_deletion_pending"] is not None
            else []
        ),
        recall_used=int(event_row["recall_used"] or 0),
        oldest_pending_seconds=_oldest_age([oldest_pending] if oldest_pending is not None else []),
    )


def _merge_aggregate_usage(target, row, aliases: dict[str, str]) -> None:
    for key, alias in aliases.items():
        value = row[alias]
        if value is not None and value > 0:
            target[key] += value


def _json_non_negative_int(column: str, path: str, dialect: str) -> str:
    return _json_non_negative(column, path, dialect, cast_type="BIGINT")


def _json_non_negative_number(column: str, path: str, dialect: str) -> str:
    cast_type = "REAL" if dialect == "sqlite" else "DOUBLE PRECISION"
    return _json_non_negative(column, path, dialect, cast_type=cast_type)


def _json_non_negative(column: str, path: str, dialect: str, *, cast_type: str) -> str:
    parts = path.split(".")
    if dialect == "sqlite":
        json_path = "$." + ".".join(parts)
        safe = f"CASE WHEN json_valid({column}) THEN {column} ELSE '{{}}' END"
        extracted = f"json_extract({safe}, '{json_path}')"
        return (
            f"CASE WHEN json_type({safe}, '{json_path}') IN ('integer', 'real') "
            f"AND CAST({extracted} AS REAL) >= 0 "
            f"THEN CAST({extracted} AS {cast_type}) ELSE 0 END"
        )
    access = f"({column})::jsonb"
    for part in parts[:-1]:
        access = f"({access} -> '{part}')"
    leaf = parts[-1]
    return (
        f"CASE WHEN jsonb_typeof({access} -> '{leaf}') = 'number' "
        f"AND CAST({access} ->> '{leaf}' AS DOUBLE PRECISION) >= 0 "
        f"THEN CAST({access} ->> '{leaf}' AS {cast_type}) ELSE 0 END"
    )


def _job_scope_cte(*, dialect: str, allowed_scopes: str) -> str:
    if dialect == "sqlite":
        safe = (
            "CASE WHEN json_valid(job.trace_summary_text) THEN job.trace_summary_text ELSE '{}' END"
        )
        return f"""
            WITH valid_scopes AS (
                SELECT DISTINCT job.job_id, job.trigger, job.status,
                       job.trace_summary_text, CAST(scope.value AS TEXT) AS scope
                FROM memory_formation_jobs AS job
                JOIN json_each({safe}, '$.scopes') AS scope
                WHERE CAST(scope.value AS TEXT) IN ({allowed_scopes})
            ),
            job_scopes AS (
                SELECT job_id, trigger, status, trace_summary_text, scope
                FROM valid_scopes
                UNION ALL
                SELECT job.job_id, job.trigger, job.status, job.trace_summary_text,
                       CASE WHEN COALESCE(json_type({safe}, '$.scopes'), 'null') <> 'array'
                                  AND json_extract({safe}, '$.scope') IN ({allowed_scopes})
                            THEN json_extract({safe}, '$.scope') ELSE 'unknown' END AS scope
                FROM memory_formation_jobs AS job
                WHERE NOT EXISTS (
                    SELECT 1 FROM valid_scopes WHERE valid_scopes.job_id = job.job_id
                )
            )
        """
    summary = "(job.trace_summary_text)::jsonb"
    return f"""
        WITH valid_scopes AS (
            SELECT DISTINCT job.job_id, job.trigger, job.status,
                   job.trace_summary_text, scope.value AS scope
            FROM memory_formation_jobs AS job
            JOIN LATERAL jsonb_array_elements_text(
                CASE WHEN jsonb_typeof({summary} -> 'scopes') = 'array'
                     THEN {summary} -> 'scopes' ELSE '[]'::jsonb END
            ) AS scope(value) ON TRUE
            WHERE scope.value IN ({allowed_scopes})
        ),
        job_scopes AS (
            SELECT job_id, trigger, status, trace_summary_text, scope
            FROM valid_scopes
            UNION ALL
            SELECT job.job_id, job.trigger, job.status, job.trace_summary_text,
                   CASE WHEN COALESCE(jsonb_typeof({summary} -> 'scopes'), 'null') <> 'array'
                              AND {summary} ->> 'scope' IN ({allowed_scopes})
                        THEN {summary} ->> 'scope' ELSE 'unknown' END AS scope
            FROM memory_formation_jobs AS job
            WHERE NOT EXISTS (
                SELECT 1 FROM valid_scopes WHERE valid_scopes.job_id = job.job_id
            )
        )
    """


async def _database_health_snapshot(session_factory) -> dict:
    pending_statuses = ("pending", "claimed", "retry")
    async with session_factory() as session:
        queue_depth = await session.scalar(
            select(func.count())
            .select_from(MemoryFormationJobModel)
            .where(MemoryFormationJobModel.status.in_(pending_statuses))
        )
        oldest_pending = await session.scalar(
            select(func.min(MemoryFormationJobModel.created_at)).where(
                MemoryFormationJobModel.status.in_(pending_statuses)
            )
        )
        formation_dead_letters = await session.scalar(
            select(func.count())
            .select_from(MemoryFormationJobModel)
            .where(MemoryFormationJobModel.status == "dead_letter")
        )
        index_out_of_sync = await session.scalar(
            select(func.count())
            .select_from(MemoryItemModel)
            .where(MemoryItemModel.index_status == "out_of_sync")
        )
        index_dead_letters = await session.scalar(
            select(func.count())
            .select_from(MemoryIndexOperationModel)
            .where(MemoryIndexOperationModel.status == "dead_letter")
        )
        deletion_pending = await session.scalar(
            select(func.count())
            .select_from(MemoryItemModel)
            .where(MemoryItemModel.lifecycle_status == "deletion_pending")
        )
        job_error = (
            await session.execute(
                select(
                    MemoryFormationJobModel.updated_at,
                    MemoryFormationJobModel.last_error_code,
                )
                .where(MemoryFormationJobModel.last_error_code.is_not(None))
                .order_by(MemoryFormationJobModel.updated_at.desc())
                .limit(1)
            )
        ).first()
        index_error = (
            await session.execute(
                select(
                    MemoryIndexOperationModel.updated_at,
                    MemoryIndexOperationModel.last_error_code,
                )
                .where(MemoryIndexOperationModel.last_error_code.is_not(None))
                .order_by(MemoryIndexOperationModel.updated_at.desc())
                .limit(1)
            )
        ).first()
    errors = [value for value in (job_error, index_error) if value is not None]
    latest_error = max(errors, key=lambda value: _as_utc(value[0]))[1] if errors else None
    return {
        "queue_depth": int(queue_depth or 0),
        "oldest_pending_seconds": _oldest_age([oldest_pending]) if oldest_pending else None,
        "formation_dead_letters": int(formation_dead_letters or 0),
        "index_out_of_sync": int(index_out_of_sync or 0),
        "index_dead_letters": int(index_dead_letters or 0),
        "deletion_pending": int(deletion_pending or 0),
        "last_safe_error": _safe_error(latest_error),
    }


def _safe_item(item: MemoryItem) -> MemoryItem:
    return item.model_copy(
        deep=True,
        update={
            "content": redact_text(item.content, max_length=1000),
            "structured_value": _bounded_redacted(item.structured_value),
            "metadata": _bounded_redacted(item.metadata),
        },
    )


def _safe_event(event: MemoryEvent) -> MemoryEvent:
    payload = _bounded_redacted(event.payload)
    if isinstance(payload, dict):
        pending = payload.get("pending_candidate")
        if isinstance(pending, dict):
            pending.pop("structured_value", None)
            semantic = pending.get("semantic")
            if isinstance(semantic, dict):
                semantic.pop("value", None)
    return event.model_copy(deep=True, update={"payload": payload})


def _safe_revision(revision) -> MemoryRevisionView:
    preview = redact_text(revision.content, max_length=300)
    return MemoryRevisionView(
        revision_id=revision.revision_id,
        memory_id=revision.memory_id,
        revision_no=revision.revision_no,
        memory_key=revision.memory_key,
        operation=revision.operation,
        content_preview=preview,
        content_redacted=preview != revision.content,
        confidence=revision.confidence,
        policy_version=revision.policy_version,
        supersedes_revision_id=revision.supersedes_revision_id,
        formation_job_id=revision.formation_job_id,
        created_at=revision.created_at,
    )


def _bounded_redacted(value, *, depth: int = 0):
    if depth >= 6:
        return "[truncated]"
    safe = redact_value(value)
    if isinstance(safe, dict):
        return {
            str(key)[:128]: _bounded_redacted(item, depth=depth + 1)
            for key, item in list(safe.items())[:50]
        }
    if isinstance(safe, list):
        return [_bounded_redacted(item, depth=depth + 1) for item in safe[:50]]
    if isinstance(safe, str):
        return redact_text(safe, max_length=500)
    return safe


def _numeric_usage(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in list(value.items())[:50]
        if key in _MODEL_USAGE_KEYS
        and isinstance(item, int | float)
        and not isinstance(item, bool)
        and item >= 0
    }


def _metric_scopes(summary) -> list[str]:
    if not isinstance(summary, dict):
        return ["unknown"]
    raw = summary.get("scopes")
    if not isinstance(raw, list):
        scope = summary.get("scope")
        raw = [scope] if isinstance(scope, str) else []
    allowed = {
        "user_preference",
        "stable_fact",
        "task_memory",
        "artifact_reference",
        "session_summary",
    }
    values = sorted({value for value in raw if isinstance(value, str) and value in allowed})
    return values or ["unknown"]


def _request_trace_stage(
    *,
    turn_status: str,
    outboxes,
    formation_turns,
    traces: list[MemoryFormationTraceView],
    items: list[MemoryItem],
    index_operations,
    events: list[MemoryEvent],
) -> tuple[str, bool, bool, str | None]:
    if turn_status in {"pending", "routing"}:
        return "turn_pending", False, True, None
    if turn_status in {"running", "blocked"}:
        return "turn_running", False, True, None
    if turn_status != "completed":
        return "turn_failed", True, False, f"turn_{turn_status}"

    dead_outbox = next((value for value in outboxes if value.status == "dead_letter"), None)
    if dead_outbox is not None:
        return "outbox_dead_letter", True, False, dead_outbox.last_error_code
    retry_outbox = next((value for value in outboxes if value.status == "retry"), None)
    if retry_outbox is not None:
        return "outbox_retry", False, True, retry_outbox.last_error_code
    if any(value.status in {"pending", "claimed"} for value in outboxes):
        return "outbox_pending", False, True, None

    skipped = next((event for event in events if event.event_type == "formation_skipped"), None)
    if skipped is not None:
        reason = skipped.payload.get("reason_code") if isinstance(skipped.payload, dict) else None
        return "formation_skipped", True, False, reason

    if not formation_turns:
        reason = "formation_turn_missing" if outboxes else "turn_outbox_missing"
        return "trace_missing", False, True, reason
    if not traces:
        if any(value.status == "skipped" for value in formation_turns):
            return "formation_skipped", True, False, "formation_turn_skipped"
        return "formation_pending", False, True, None

    dead_job = next((trace for trace in traces if trace.job.status == "dead_letter"), None)
    if dead_job is not None:
        return "formation_dead_letter", True, False, dead_job.job.last_error_code
    retry_job = next((trace for trace in traces if trace.job.status == "retry"), None)
    if retry_job is not None:
        return "formation_retry", False, True, retry_job.job.last_error_code
    if any(trace.job.status in {"pending", "claimed"} for trace in traces):
        return "formation_pending", False, True, None

    if items:
        dead_index = next(
            (value for value in index_operations if value.status == "dead_letter"), None
        )
        if dead_index is not None:
            return "index_dead_letter", True, False, dead_index.last_error_code
        retry_index = next((value for value in index_operations if value.status == "retry"), None)
        if retry_index is not None:
            return "index_retry", False, True, retry_index.last_error_code
        if all(str(item.index_status) == "ready" for item in items):
            return "persisted", True, False, None
        return "memory_persisted_index_pending", False, True, None

    if all(trace.candidate_count == 0 for trace in traces):
        return "completed_no_candidate", True, False, None
    accepted = any(
        decision.decision_status == "accepted" for trace in traces for decision in trace.decisions
    )
    if not accepted:
        return "policy_rejected", True, False, None
    return "trace_missing", False, True, "accepted_memory_missing"


def _formation_metric_series(data: dict[str, list[dict]]) -> dict[str, dict]:
    jobs_by_id = {row["job_id"]: row for row in data["jobs"] if isinstance(row.get("job_id"), str)}
    series: dict[str, dict] = {}
    for row in data["jobs"]:
        summary = row["trace_summary"]
        for scope in _metric_scopes(summary):
            key = f"{row['trigger']}:{scope}:{row['status']}"
            value = series.setdefault(key, _empty_metric_series())
            value["job_count"] += 1
            _add_non_negative(value, "queue_latency_ms_total", summary.get("queue_latency_ms"))
            _add_non_negative(value, "model_latency_ms_total", summary.get("model_latency_ms"))
            usage = summary.get("usage")
            if isinstance(usage, dict):
                for usage_key, amount in usage.items():
                    if (
                        usage_key in _MODEL_USAGE_KEYS
                        and isinstance(amount, int | float)
                        and not isinstance(amount, bool)
                        and amount >= 0
                    ):
                        value["model_usage_total"][usage_key] = (
                            value["model_usage_total"].get(usage_key, 0) + amount
                        )
    for row in data["events"]:
        if not row["event_type"].startswith("memory_decision_") or not row["decision_status"]:
            continue
        job = jobs_by_id.get(row.get("formation_job_id"))
        if job is None:
            continue
        scope = row.get("scope")
        if scope not in {
            "user_preference",
            "stable_fact",
            "task_memory",
            "artifact_reference",
            "session_summary",
        }:
            scope = "unknown"
        key = f"{job['trigger']}:{scope}:{job['status']}"
        value = series.setdefault(key, _empty_metric_series())
        decision_key = (
            f"{row['event_type'].removeprefix('memory_decision_')}:{row['decision_status']}"
        )
        value["decisions_by_operation_status"][decision_key] = (
            value["decisions_by_operation_status"].get(decision_key, 0) + 1
        )
    for value in series.values():
        total = sum(value["decisions_by_operation_status"].values())
        value["decision_rates"] = (
            {
                key: count / total
                for key, count in sorted(value["decisions_by_operation_status"].items())
            }
            if total
            else {}
        )
        value["model_usage_total"] = dict(sorted(value["model_usage_total"].items()))
        value["decisions_by_operation_status"] = dict(
            sorted(value["decisions_by_operation_status"].items())
        )
    return dict(sorted(series.items()))


def _empty_metric_series() -> dict:
    return {
        "job_count": 0,
        "queue_latency_ms_total": 0,
        "model_latency_ms_total": 0,
        "model_usage_total": {},
        "decisions_by_operation_status": {},
        "decision_rates": {},
    }


def _add_non_negative(target: dict, key: str, value) -> None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        target[key] += value


def _safe_error(value) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value[:128]
    if normalized.replace("_", "").replace("-", "").isalnum():
        return normalized
    return "internal_error"


def _bounded_optional(value, limit: int) -> str | None:
    return str(value)[:limit] if value is not None else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _oldest_age(values: list[datetime]) -> float | None:
    if not values:
        return None
    return max(0.0, (datetime.now(UTC) - min(_as_utc(value) for value in values)).total_seconds())


def _unique_links(values: list[MemoryTraceLinks]) -> list[MemoryTraceLinks]:
    result = []
    seen = set()
    for value in values:
        identity = (
            tuple(value.request_ids),
            value.session_id,
            tuple(value.turn_ids),
            tuple(value.run_ids),
            tuple(value.memory_ids),
            value.consumer,
            value.projection_outcome,
            value.relevance,
            value.confidence,
        )
        if identity not in seen:
            seen.add(identity)
            result.append(value)
    return result


def _context_trace_links(events: list[MemoryEvent]) -> list[MemoryTraceLinks]:
    values = []
    for event in events:
        if event.event_type not in {"memory_recall_used", "memory_recall_linked"}:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        values.append(
            MemoryTraceLinks(
                request_ids=[event.request_id] if event.request_id else [],
                session_id=event.session_id,
                turn_ids=[event.turn_id] if event.turn_id else [],
                run_ids=[event.run_id] if event.run_id else [],
                memory_ids=[event.memory_id] if event.memory_id else [],
                consumer=_bounded_optional(payload.get("consumer"), 128),
                projection_outcome=_bounded_optional(payload.get("projection_outcome"), 64),
                relevance=_bounded_score(payload.get("relevance")),
                confidence=_bounded_score(payload.get("confidence")),
            )
        )
    return _unique_links(values)


def _bounded_score(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 1 else None


def _index_status(item, operation):
    if item is not None and item.index_status is not None:
        return item.index_status
    if operation is None:
        return None
    if operation.status == "dead_letter":
        return "dead_letter"
    if operation.operation == "delete":
        return "deleted" if operation.status == "completed" else "deletion_pending"
    if operation.status == "completed":
        return "ready"
    if operation.status == "retry":
        return "out_of_sync"
    return "pending"


def _provider_status(operation) -> str | None:
    if operation is None:
        return None
    value = operation.last_error_metadata.get("provider_status")
    return str(value)[:64] if value is not None else operation.status.value


def _worker_state(status, maintenance_status, settings) -> str:
    formation_enabled = (
        settings.memory_formation_mode != "off" and settings.memory_formation_worker_enabled
    )
    maintenance_enabled = (
        settings.memory_index_worker_enabled or settings.memory_ttl_sweeper_enabled
    )
    if not formation_enabled and not maintenance_enabled:
        return "disabled"
    if status is None and maintenance_status is None:
        return "unknown"
    if status is not None and status.state == "starting":
        return "starting"
    if (status is not None and status.worker_running) or (
        maintenance_status is not None
        and (maintenance_status.index_worker_running or maintenance_status.ttl_sweeper_running)
    ):
        return "running"
    states = [
        value.state
        for value in (status, maintenance_status)
        if value is not None and value.state != "disabled"
    ]
    return states[0] if states else "disabled"
