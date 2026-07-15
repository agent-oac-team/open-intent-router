from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.db.models import (
    MemoryEventModel,
    MemoryFormationJobModel,
    MemoryFormationTurnModel,
)
from app.repositories.json_utils import loads
from app.repositories.memory_formation import _job_from_row
from app.schemas.memory import MemoryFormationTrace, MemoryLifecycleOperation

_MAX_TRACE_EVENTS = 500
_SEMANTIC_VALIDATION_OUTCOMES = frozenset({"confirmed", "pending", "current_turn", "not_evaluated"})
_SEMANTIC_VERIFIER_OUTCOMES = frozenset(
    {
        "confirmed",
        "contradicted",
        "uncertain",
        "error",
        "not_configured",
        "temporary_response_language_guard",
    }
)


class MemoryFormationTraceRepository:
    def __init__(self, formation_repository=None, event_repository=None) -> None:
        self.traces: dict[str, MemoryFormationTrace] = {}
        self.formation_repository = formation_repository
        self.event_repository = event_repository

    async def add_trace(self, trace: MemoryFormationTrace) -> MemoryFormationTrace:
        stored = trace.model_copy(deep=True)
        self.traces[trace.job.job_id] = stored
        return stored.model_copy(deep=True)

    async def list_traces(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        scope: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        formation_job_id: str | None = None,
        memory_id: str | None = None,
        memory_key: str | None = None,
        decision_status: str | None = None,
        limit: int = 50,
    ) -> list[MemoryFormationTrace]:
        limit = max(1, min(limit, 100))
        traces = dict(self.traces)
        if self.formation_repository is not None and self.event_repository is not None:
            for job in self.formation_repository.jobs.values():
                if job.job_id not in traces:
                    traces[job.job_id] = _in_memory_trace(
                        job,
                        turns=self.formation_repository.turns.values(),
                        events=self.event_repository.events,
                    )
        values = [trace for trace in traces.values() if trace.job.tenant_id == tenant_id]
        filters = (
            (user_id, lambda trace: trace.job.user_id == user_id),
            (agent_id, lambda trace: agent_id in trace.agent_ids),
            (scope, lambda trace: scope in trace.scopes),
            (request_id, lambda trace: request_id in trace.request_ids),
            (session_id, lambda trace: trace.job.session_id == session_id),
            (turn_id, lambda trace: turn_id in trace.turn_ids),
            (run_id, lambda trace: run_id in trace.run_ids),
            (formation_job_id, lambda trace: trace.job.job_id == formation_job_id),
            (
                memory_id,
                lambda trace: any(
                    operation.memory_id == memory_id for operation in trace.operations
                ),
            ),
            (
                memory_key,
                lambda trace: any(
                    operation.memory_key == memory_key for operation in trace.operations
                ),
            ),
            (
                decision_status,
                lambda trace: any(
                    str(operation.decision_status) == decision_status
                    for operation in trace.operations
                ),
            ),
        )
        for expected, predicate in filters:
            if expected:
                values = [trace for trace in values if predicate(trace)]
        return [
            trace.model_copy(deep=True)
            for trace in sorted(values, key=lambda trace: trace.job.updated_at, reverse=True)[
                :limit
            ]
        ]


class DatabaseMemoryFormationTraceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def list_traces(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        scope: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        formation_job_id: str | None = None,
        memory_id: str | None = None,
        memory_key: str | None = None,
        decision_status: str | None = None,
        limit: int = 50,
    ) -> list[MemoryFormationTrace]:
        limit = max(1, min(limit, 100))
        async with self.session_factory() as session:
            stmt = select(MemoryFormationJobModel).where(
                MemoryFormationJobModel.tenant_id == tenant_id
            )
            stmt = _apply_trace_filters(
                stmt,
                tenant_id=tenant_id,
                agent_id=agent_id,
                scope=scope,
                request_id=request_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                memory_id=memory_id,
                memory_key=memory_key,
                decision_status=decision_status,
            )
            if user_id:
                stmt = stmt.where(MemoryFormationJobModel.user_id == user_id)
            if formation_job_id:
                stmt = stmt.where(MemoryFormationJobModel.job_id == formation_job_id)
            rows = (
                (
                    await session.execute(
                        stmt.order_by(MemoryFormationJobModel.updated_at.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [await _trace_from_job(session, row) for row in rows]


def _apply_trace_filters(stmt, *, tenant_id: str, **filters):
    requested = {key: value for key, value in filters.items() if value}
    association_filters = {
        "request_id": "request_id",
        "session_id": "session_id",
        "turn_id": "turn_id",
        "run_id": "run_id",
        "agent_id": "agent_id",
    }
    for name, column_name in association_filters.items():
        if value := requested.get(name):
            turn = aliased(MemoryFormationTurnModel)
            event = aliased(MemoryEventModel)
            matches = [
                exists(
                    select(turn.turn_id).where(
                        turn.tenant_id == tenant_id,
                        turn.claimed_job_id == MemoryFormationJobModel.job_id,
                        getattr(turn, column_name) == value,
                    )
                ),
                exists(
                    select(event.event_id).where(
                        event.tenant_id == tenant_id,
                        event.formation_job_id == MemoryFormationJobModel.job_id,
                        getattr(event, column_name) == value,
                    )
                ),
            ]
            if name == "session_id":
                matches.append(MemoryFormationJobModel.session_id == value)
            stmt = stmt.where(or_(*matches))
    event_filter_names = ("memory_id", "scope", "memory_key", "decision_status")
    event_values = {name: requested[name] for name in event_filter_names if name in requested}
    if event_values:
        event = aliased(MemoryEventModel)
        event_conditions = [
            event.tenant_id == tenant_id,
            event.formation_job_id == MemoryFormationJobModel.job_id,
        ]
        event_conditions.extend(
            getattr(event, name) == value for name, value in event_values.items()
        )
        if event_values.get("decision_status") == "pending":
            terminal = aliased(MemoryEventModel)
            event_conditions.append(event.event_type.like("memory_decision_%"))
            event_conditions.append(
                ~exists(
                    select(terminal.event_id).where(
                        terminal.tenant_id == event.tenant_id,
                        terminal.decision_id == event.event_id,
                        terminal.event_type.in_(
                            (
                                "memory_pending_confirm",
                                "memory_pending_reject",
                                "memory_pending_conflict",
                            )
                        ),
                    )
                )
            )
        stmt = stmt.where(exists(select(event.event_id).where(*event_conditions)))
    return stmt


async def _trace_from_job(session, row) -> MemoryFormationTrace:
    turns = (
        (
            await session.execute(
                select(MemoryFormationTurnModel).where(
                    MemoryFormationTurnModel.claimed_job_id == row.job_id
                )
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await session.execute(
                select(MemoryEventModel)
                .where(
                    MemoryEventModel.formation_job_id == row.job_id,
                    MemoryEventModel.tenant_id == row.tenant_id,
                )
                .order_by(MemoryEventModel.created_at.desc())
                .limit(_MAX_TRACE_EVENTS)
            )
        )
        .scalars()
        .all()
    )
    operations = _resolved_operations(
        [(event.event_id, event.event_type, loads(event.payload_text, {})) for event in events]
    )
    summary = loads(row.trace_summary_text, {})
    return MemoryFormationTrace(
        job=_job_from_row(row),
        turn_ids=sorted(
            {
                value
                for value in [
                    *[turn.turn_id for turn in turns],
                    *[event.turn_id for event in events],
                ]
                if value
            }
        ),
        request_ids=sorted(
            {
                value
                for value in [
                    *[turn.request_id for turn in turns],
                    *[event.request_id for event in events],
                ]
                if value
            }
        ),
        run_ids=sorted(
            {
                value
                for value in [
                    *[turn.run_id for turn in turns],
                    *[event.run_id for event in events],
                ]
                if value
            }
        ),
        agent_ids=sorted(
            {
                value
                for value in [
                    *[turn.agent_id for turn in turns],
                    *[event.agent_id for event in events],
                ]
                if value
            }
        ),
        scopes=sorted({event.scope for event in events if event.scope}),
        operations=operations,
        candidate_count=int(summary.get("candidate_count", 0)),
        decision_counts=summary.get("decision_counts", {}),
        semantic_contract_version=_safe_semantic_version(summary.get("semantic_contract_version")),
        semantic_validation_counts=_safe_semantic_counts(
            summary.get("semantic_validation_counts"),
            allowed=_SEMANTIC_VALIDATION_OUTCOMES,
        ),
        semantic_verifier_counts=_safe_semantic_counts(
            summary.get("semantic_verifier_counts"),
            allowed=_SEMANTIC_VERIFIER_OUTCOMES,
        ),
        model_latency_ms=_non_negative_int(summary.get("model_latency_ms")),
        provider_latency_ms=_non_negative_int(summary.get("provider_latency_ms")),
        usage=summary.get("usage", {}) if isinstance(summary.get("usage"), dict) else {},
    )


def _in_memory_trace(job, *, turns, events) -> MemoryFormationTrace:
    matching_turns = [turn for turn in turns if turn.claimed_job_id == job.job_id]
    matching_events = [
        event
        for event in events
        if event.formation_job_id == job.job_id and event.tenant_id == job.tenant_id
    ][-_MAX_TRACE_EVENTS:]
    operations = _resolved_operations(
        [(event.event_id, event.event_type, event.payload) for event in matching_events]
    )
    summary = job.trace_summary if isinstance(job.trace_summary, dict) else {}
    return MemoryFormationTrace(
        job=job.model_copy(deep=True),
        turn_ids=sorted(
            {
                value
                for value in [
                    *[turn.turn_id for turn in matching_turns],
                    *[event.turn_id for event in matching_events],
                ]
                if value
            }
        ),
        request_ids=sorted(
            {
                value
                for value in [
                    *[turn.request_id for turn in matching_turns],
                    *[event.request_id for event in matching_events],
                ]
                if value
            }
        ),
        run_ids=sorted(
            {
                value
                for value in [
                    *[turn.run_id for turn in matching_turns],
                    *[event.run_id for event in matching_events],
                ]
                if value
            }
        ),
        agent_ids=sorted(
            {
                value
                for value in [
                    *[turn.agent_id for turn in matching_turns],
                    *[event.agent_id for event in matching_events],
                ]
                if value
            }
        ),
        scopes=sorted({event.scope for event in matching_events if event.scope}),
        operations=operations,
        candidate_count=int(summary.get("candidate_count", 0)),
        decision_counts=summary.get("decision_counts", {}),
        semantic_contract_version=_safe_semantic_version(summary.get("semantic_contract_version")),
        semantic_validation_counts=_safe_semantic_counts(
            summary.get("semantic_validation_counts"),
            allowed=_SEMANTIC_VALIDATION_OUTCOMES,
        ),
        semantic_verifier_counts=_safe_semantic_counts(
            summary.get("semantic_verifier_counts"),
            allowed=_SEMANTIC_VERIFIER_OUTCOMES,
        ),
        model_latency_ms=_non_negative_int(summary.get("model_latency_ms")),
        provider_latency_ms=_non_negative_int(summary.get("provider_latency_ms")),
        usage=summary.get("usage", {}) if isinstance(summary.get("usage"), dict) else {},
    )


def _operation_from_payload(payload) -> MemoryLifecycleOperation | None:
    raw = payload.get("operation") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return MemoryLifecycleOperation.model_validate(raw)
    except ValueError:
        return None


def _resolved_operations(
    events: list[tuple[str, str, object]],
) -> list[MemoryLifecycleOperation]:
    by_event_id = {}
    ordered_event_ids = []
    resolutions = []
    terminal_resolution_events = {
        "memory_pending_confirm",
        "memory_pending_reject",
        "memory_pending_conflict",
    }
    for event_id, event_type, payload in events:
        operation = _operation_from_payload(payload)
        if operation is not None:
            by_event_id[event_id] = operation
            ordered_event_ids.append(event_id)
        if (
            event_type in terminal_resolution_events
            and isinstance(payload, dict)
            and isinstance(payload.get("decision_id"), str)
        ):
            resolutions.append(payload)
    for payload in resolutions:
        decision_id = payload["decision_id"]
        operation = by_event_id.get(decision_id)
        if operation is None:
            continue
        by_event_id[decision_id] = operation.model_copy(
            update={
                "decision_status": "resolved",
                "metadata": {
                    **operation.metadata,
                    "resolution_action": str(payload.get("action") or "resolved")[:64],
                },
            }
        )
    return [by_event_id[event_id] for event_id in ordered_event_ids]


def _non_negative_int(value) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _safe_semantic_version(value) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if not all(character.isalnum() or character in "._-" for character in value):
        return None
    return value


def _safe_semantic_counts(value, *, allowed: frozenset[str]) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: count
        for key, count in value.items()
        if key in allowed and isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }
