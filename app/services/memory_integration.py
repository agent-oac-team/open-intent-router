import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from app.core.config import Settings
from app.core.memory_runtime import MemoryRuntimePolicy
from app.llm.conversation_formation import validate_conversation_candidates
from app.schemas.invocation import AgentInvocation, AgentInvocationResult
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.memory import (
    MemoryDecisionStatus,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTrigger,
    MemoryOperation,
)
from app.schemas.plans import Plan
from app.services.memory_candidate_policy import CandidatePolicyResult
from app.services.structured_event_projector import StructuredEventProjector, StructuredProjection

_MODEL_USAGE_KEYS = {
    "cached_tokens",
    "completion_tokens",
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "reasoning_tokens",
    "total_tokens",
}
_MEMORY_METRIC_SCOPES = {
    "user_preference",
    "stable_fact",
    "task_memory",
    "artifact_reference",
    "session_summary",
}


class TurnCaptureSink(Protocol):
    async def capture(
        self,
        *,
        invocation: AgentInvocation,
        result: AgentInvocationResult,
        result_id: str,
        completed_at: datetime | None = None,
    ): ...


class StructuredFormationSink(Protocol):
    async def publish_plan(self, plan: Plan, *, event_type: str, **kwargs): ...

    async def publish_run(self, run: AgentRun, *, event_type: str, **kwargs): ...

    async def publish_result(self, result: AgentResult, *, run: AgentRun, **kwargs): ...


class FormationSourceUnavailable(RuntimeError):
    error_code = "formation_source_unavailable"


class StructuredFormationPublisher:
    def __init__(
        self,
        *,
        settings: Settings,
        repository,
        projector: StructuredEventProjector | None = None,
        clock: Callable[[], datetime] | None = None,
        runtime_policy: MemoryRuntimePolicy | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_policy = runtime_policy or settings.memory_runtime_policy
        self.repository = repository
        self.projector = projector or StructuredEventProjector()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def publish_plan(
        self,
        plan: Plan,
        *,
        event_type: str,
        event_id: str | None = None,
        source_version: str | None = None,
        occurred_at: datetime | None = None,
        source_order: int | None = None,
    ) -> MemoryFormationJob | None:
        if self.runtime_policy.effective_formation_mode == "off":
            return None
        occurred = occurred_at or self.clock()
        version = source_version or _source_version(
            {"event_type": event_type, "plan": plan.model_dump(mode="json"), "at": occurred}
        )
        projection = self.projector.project_plan(
            plan,
            event_type=event_type,
            event_id=event_id or _event_id("plan", plan.plan_id, version),
            source_version=version,
            last_activity_at=occurred,
        )
        return await self._publish(
            projection,
            session_id=plan.session_id,
            source_order=(
                source_order
                if source_order is not None
                else plan.state_version
                if plan.state_version > 0
                else 1
                if event_type == "create"
                else 2
            ),
        )

    async def publish_run(
        self,
        run: AgentRun,
        *,
        event_type: str,
        event_id: str | None = None,
        source_version: str | None = None,
    ) -> MemoryFormationJob | None:
        if self.runtime_policy.effective_formation_mode == "off":
            return None
        version = source_version or _source_version(
            {"event_type": event_type, "run": run.model_dump(mode="json")}
        )
        projection = self.projector.project_run(
            run,
            event_type=event_type,
            event_id=event_id or _event_id("run", run.run_id, version),
            source_version=version,
        )
        return await self._publish(
            projection,
            session_id=run.session_id,
            source_order={"create": 1, "update": 2, "complete": 3, "fail": 3}[event_type],
        )

    async def publish_result(
        self,
        result: AgentResult,
        *,
        run: AgentRun,
        event_id: str | None = None,
        source_version: str | None = None,
    ) -> MemoryFormationJob | None:
        if self.runtime_policy.effective_formation_mode == "off":
            return None
        version = source_version or _source_version(
            {
                "result": result.model_dump(mode="json"),
                "run_identity": {
                    "run_id": run.run_id,
                    "tenant_id": run.tenant_id,
                    "user_id": run.user_id,
                },
            }
        )
        projection = self.projector.project_result(
            result,
            run=run,
            event_id=event_id or _event_id("result", result.result_id, version),
            source_version=version,
        )
        return await self._publish(
            projection,
            session_id=run.session_id,
            source_order=1,
        )

    async def _publish(
        self,
        projection: StructuredProjection,
        *,
        session_id: str | None,
        source_order: int,
    ) -> MemoryFormationJob:
        command = {
            "source_type": projection.source_type,
            "source_id": projection.source_id,
            "source_version": projection.source_version,
            "event_id": projection.event_id,
            "source_order": source_order,
            "canonical_refs": list(projection.canonical_refs),
            "candidates": [
                candidate.model_dump(mode="json") for candidate in projection.candidates
            ],
        }
        digest = hashlib.sha256(projection.idempotency_key.encode()).hexdigest()
        job = MemoryFormationJob(
            job_id=f"mfjob_struct_{digest[:32]}",
            trigger=MemoryFormationTrigger.STRUCTURED_EVENT,
            mode=self.runtime_policy.effective_formation_mode,
            tenant_id=projection.tenant_id,
            user_id=projection.user_id,
            session_id=session_id,
            source_refs=_unique_refs([projection.event_id, *projection.canonical_refs]),
            idempotency_key=projection.idempotency_key,
            model_version=self.settings.memory_formation_model_version,
            prompt_version=self.settings.memory_formation_prompt_version,
            policy_version=self.settings.memory_formation_policy_version,
            max_attempts=self.settings.memory_formation_max_attempts,
            trace_summary={"command": command},
        )
        return await self.repository.add_job(job)


class FormationReconciler:
    def __init__(
        self,
        *,
        plan_repository,
        run_repository,
        result_repository,
        publisher: StructuredFormationPublisher,
        turn_capture,
        limit: int = 100,
    ) -> None:
        self.plan_repository = plan_repository
        self.run_repository = run_repository
        self.result_repository = result_repository
        self.publisher = publisher
        self.turn_capture = turn_capture
        self.limit = limit

    async def run_once(self) -> dict[str, int]:
        counts = {"plans": 0, "runs": 0, "results": 0, "turns": 0}
        for plan in await self.plan_repository.list_formation_pending(limit=self.limit):
            try:
                await self.publisher.publish_plan(
                    plan,
                    event_type=plan.formation_event_type,
                    event_id=plan.last_event_id,
                    source_version=plan.last_event_id or f"plan-state-{plan.state_version}",
                    source_order=plan.state_version,
                    occurred_at=plan.updated_at,
                )
                await self.plan_repository.mark_formation_published(
                    plan.plan_id,
                    state_version=plan.state_version,
                )
                counts["plans"] += 1
            except Exception:
                continue
        for run in await self.run_repository.list_formation_pending(limit=self.limit):
            try:
                await publish_run_transitions(
                    publisher=self.publisher,
                    repository=self.run_repository,
                    run=run,
                )
                counts["runs"] += 1
            except Exception:
                continue
        for result in await self.result_repository.list_formation_pending(limit=self.limit):
            run = await self.run_repository.get_run(result.run_id)
            if run is None:
                continue
            if not _result_matches_run_owner(result, run):
                try:
                    await self.result_repository.mark_formation_published(result.result_id)
                    await self.result_repository.mark_turn_captured(result.result_id)
                except Exception:
                    pass
                continue
            if not run.formation_suppressed:
                try:
                    await self.publisher.publish_result(result, run=run)
                    await self.result_repository.mark_formation_published(result.result_id)
                    counts["results"] += 1
                except Exception:
                    pass
            try:
                await self.turn_capture.capture_records(run=run, result=result)
                await self.result_repository.mark_turn_captured(result.result_id)
                counts["turns"] += 1
            except Exception:
                pass
        return counts


class MemoryFormationProcessor:
    def __init__(
        self,
        *,
        repository,
        memory_repository,
        model,
        policy,
        lifecycle,
    ) -> None:
        self.repository = repository
        self.memory_repository = memory_repository
        self.model = model
        self.policy = policy
        self.lifecycle = lifecycle

    async def process(self, job: MemoryFormationJob, *, execute_lifecycle: bool) -> dict:
        model_latency_ms = None
        usage = {}
        if job.trigger == MemoryFormationTrigger.STRUCTURED_EVENT:
            turns = []
            candidates = [
                _with_structured_source(candidate, job) for candidate in _structured_candidates(job)
            ]
        else:
            turns = await self.repository.list_turns_by_ids(
                job.source_refs,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
            )
            if len(turns) != len(job.source_refs):
                raise FormationSourceUnavailable("Formation turn range is incomplete")
            existing = await self.memory_repository.list_active(
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                subject_type="user",
                subject_id=job.user_id,
                limit=100,
            )
            model_started = time.perf_counter()
            candidates = validate_conversation_candidates(
                await self.model.form(turns=turns, existing_memories=existing)
            )
            model_latency_ms = max(0, int((time.perf_counter() - model_started) * 1000))
            raw_usage = getattr(self.model, "last_usage", {})
            if isinstance(raw_usage, dict):
                usage = {
                    key: value
                    for key, value in list(raw_usage.items())[:50]
                    if key in _MODEL_USAGE_KEYS
                    and isinstance(value, int | float)
                    and not isinstance(value, bool)
                    and value >= 0
                }

        results = []
        for candidate in candidates:
            decision = await self.policy.evaluate(job=job, candidate=candidate, turns=turns)
            if job.trigger == MemoryFormationTrigger.STRUCTURED_EVENT:
                decision = await self._suppress_stale_structured_decision(
                    job=job,
                    decision=decision,
                )
            result = (
                await self.lifecycle.apply(decision)
                if execute_lifecycle
                else await self.lifecycle.observe(decision)
            )
            results.append(result)

        operation_counts = Counter(result.operation.operation.value for result in results)
        decision_counts = Counter(result.operation.decision_status.value for result in results)
        semantic_validation_counts = Counter(
            str(result.operation.metadata.get("semantic_validation", "unknown"))
            for result in results
        )
        semantic_verifier_counts = Counter(
            str(result.operation.metadata["semantic_verifier"])
            for result in results
            if result.operation.metadata.get("semantic_verifier") is not None
        )
        summary = {
            "candidate_count": len(candidates),
            "decision_count": len(results),
            "operation_counts": dict(sorted(operation_counts.items())),
            "decision_counts": dict(sorted(decision_counts.items())),
            "semantic_contract_version": "v1",
            "semantic_validation_counts": dict(sorted(semantic_validation_counts.items())),
            "semantic_verifier_counts": dict(sorted(semantic_verifier_counts.items())),
            "scopes": sorted(
                {
                    str(candidate.scope)
                    for candidate in candidates
                    if str(candidate.scope) in _MEMORY_METRIC_SCOPES
                }
            ),
            "memory_ids": [result.item.memory_id for result in results if result.item is not None],
            "revision_ids": [
                result.revision.revision_id for result in results if result.revision is not None
            ],
        }
        if model_latency_ms is not None:
            summary["model_latency_ms"] = model_latency_ms
            summary["usage"] = usage
        return summary

    async def _suppress_stale_structured_decision(
        self,
        *,
        job: MemoryFormationJob,
        decision: CandidatePolicyResult,
    ) -> CandidatePolicyResult:
        operation = decision.operation
        current = await self.memory_repository.get_current_by_key(
            tenant_id=operation.tenant_id,
            user_id=operation.user_id,
            subject_type=operation.subject_type,
            subject_id=operation.subject_id,
            scope=str(decision.candidate.scope),
            memory_key=operation.memory_key,
            include_expired=True,
        )
        if current is None:
            return decision
        incoming_order = _structured_source_order(job)
        applied_order = current.structured_value.get("_formation_source_order")
        if not isinstance(applied_order, int) or applied_order < incoming_order:
            return decision
        stale = operation.model_copy(
            update={
                "operation": MemoryOperation.NOOP,
                "decision_status": MemoryDecisionStatus.NOOP,
                "reason_code": MemoryFormationReasonCode.DUPLICATE_CANDIDATE,
                "memory_id": current.memory_id,
                "revision_id": current.current_revision_id,
            }
        )
        return CandidatePolicyResult(
            candidate=decision.candidate,
            operation=stale,
            redacted_trace={
                **decision.redacted_trace,
                "operation": "noop",
                "decision_status": "noop",
                "reason_code": "duplicate_candidate",
                "stale_source_order": incoming_order,
                "applied_source_order": applied_order,
            },
        )


def _structured_candidates(job: MemoryFormationJob) -> list[MemoryFormationCandidate]:
    command = job.trace_summary.get("command")
    if not isinstance(command, dict):
        raise FormationSourceUnavailable("Structured formation command is missing")
    values = command.get("candidates")
    if not isinstance(values, list):
        raise FormationSourceUnavailable("Structured formation candidates are missing")
    return [MemoryFormationCandidate.model_validate(value) for value in values]


def _with_structured_source(
    candidate: MemoryFormationCandidate,
    job: MemoryFormationJob,
) -> MemoryFormationCandidate:
    command = job.trace_summary["command"]
    return candidate.model_copy(
        update={
            "structured_value": {
                **candidate.structured_value,
                "_formation_source_type": command["source_type"],
                "_formation_source_id": command["source_id"],
                "_formation_source_version": command["source_version"],
                "_formation_source_order": _structured_source_order(job),
            }
        }
    )


def _structured_source_order(job: MemoryFormationJob) -> int:
    command = job.trace_summary.get("command")
    value = command.get("source_order") if isinstance(command, dict) else None
    if not isinstance(value, int) or value < 1:
        raise FormationSourceUnavailable("Structured formation source order is missing")
    return value


def _source_version(value: dict) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return f"v_{hashlib.sha256(payload.encode()).hexdigest()}"


def _event_id(source_type: str, source_id: str, source_version: str) -> str:
    identity = "\x1f".join((source_type, source_id, source_version))
    return f"mfevt_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"


def _unique_refs(values: list[str]) -> list[str]:
    refs = []
    for value in values:
        if value and value not in refs:
            refs.append(value)
        if len(refs) >= 100:
            break
    return refs


def _run_event_type(status: str) -> str:
    if status == "completed":
        return "complete"
    if status in {"failed", "invalid_output"}:
        return "fail"
    return "update" if status in {"blocked", "clarify"} else "create"


def _run_source_order(status: str) -> int:
    if status == "running":
        return 1
    if status in {"blocked", "clarify"}:
        return 2
    return 3


def _result_matches_run_owner(result: AgentResult, run: AgentRun) -> bool:
    return bool(
        result.tenant_id
        and result.user_id
        and result.run_id == run.run_id
        and result.session_id == run.session_id
        and result.agent_id == run.agent_id
        and result.tenant_id == run.tenant_id
        and result.user_id == run.user_id
        and result.plan_id == run.plan_id
        and result.step_id == run.step_id
    )


async def publish_run_transitions(*, publisher, repository, run: AgentRun) -> None:
    published_order = await repository.get_formation_published_order(run.run_id)
    if published_order < 1:
        running = run.model_copy(
            update={
                "status": "running",
                "output": None,
                "error": None,
                "latency_ms": None,
                "updated_at": run.created_at,
            }
        )
        await publisher.publish_run(running, event_type="create")
        await repository.mark_formation_published(run.run_id, source_order=1)
        published_order = 1
    target_order = _run_source_order(run.status)
    if target_order <= published_order:
        return
    await publisher.publish_run(run, event_type=_run_event_type(run.status))
    await repository.mark_formation_published(run.run_id, source_order=target_order)


__all__ = [
    "FormationSourceUnavailable",
    "FormationReconciler",
    "MemoryFormationProcessor",
    "StructuredFormationPublisher",
    "StructuredFormationSink",
    "TurnCaptureSink",
    "publish_run_transitions",
]
