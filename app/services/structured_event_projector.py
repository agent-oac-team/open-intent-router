import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field

from app.schemas.common import ArtifactRef, StrictBaseModel, normalize_artifact_refs
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryCandidateSemantics,
    MemoryChangeIntent,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryPolarity,
    MemorySemanticCertainty,
    MemorySemanticTarget,
    MemoryTemporalScope,
)
from app.schemas.plans import Plan, PlanStep
from app.services.memory_formation import structured_event_idempotency_key

PlanProjectionEvent = Literal["create", "update", "confirm", "cancel"]
RunProjectionEvent = Literal["create", "update", "complete", "fail"]

_TERMINAL_PLAN_STATUSES = {"completed", "failed", "cancelled"}
_MAX_SUMMARY_CHARS = 1000
_MAX_DESCRIPTION_CHARS = 300
_MAX_REF_CHARS = 500


class StructuredProjection(StrictBaseModel):
    source_type: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    authority: Literal["authoritative"] = "authoritative"
    idempotency_key: str = Field(min_length=1)
    canonical_refs: list[str] = Field(default_factory=list, max_length=50)
    candidates: list[MemoryFormationCandidate] = Field(min_length=1, max_length=50)


class StructuredEventProjector:
    def project_plan(
        self,
        plan: Plan,
        *,
        event_type: PlanProjectionEvent,
        event_id: str,
        source_version: str,
        last_activity_at: datetime,
        model_summary: str | Mapping | None = None,
    ) -> StructuredProjection:
        tenant_id, user_id = _require_owner(plan.tenant_id, plan.user_id, "Plan")
        _validate_plan_identifiers(plan)
        _validate_plan_event(plan, event_type)
        current_step = _current_step(plan)
        next_step = _next_step(plan, current_step)
        memory_key = _memory_key(tenant_id, user_id, "plan", plan.plan_id, "task_status")
        structured_value = {
            "object_type": "plan",
            "plan_id": plan.plan_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": plan.session_id,
            "status": plan.status,
            "current_step": _step_projection(current_step),
            "next_step": _step_projection(next_step),
            "event_type": event_type,
            "last_activity_at": last_activity_at.isoformat(),
            "summary": _summary(model_summary),
        }
        candidate = _candidate(
            source_type="plan",
            source_id=plan.plan_id,
            source_version=source_version,
            slot="task_status",
            operation=(
                MemoryCandidateOperation.ADD
                if event_type == "create"
                else MemoryCandidateOperation.UPDATE
            ),
            scope="task_memory",
            memory_key=memory_key,
            tenant_id=tenant_id,
            user_id=user_id,
            content=_plan_content(
                plan,
                current_step,
                next_step,
                structured_value["summary"],
                last_activity_at,
            ),
            structured_value=structured_value,
            event_id=event_id,
        )
        return _projection(
            source_type="plan",
            source_id=plan.plan_id,
            source_version=source_version,
            event_id=event_id,
            tenant_id=tenant_id,
            user_id=user_id,
            canonical_refs=[plan.plan_id, *([plan.session_id] if plan.session_id else [])],
            candidates=[candidate],
        )

    def project_run(
        self,
        run: AgentRun,
        *,
        event_type: RunProjectionEvent,
        event_id: str,
        source_version: str,
        model_summary: str | Mapping | None = None,
    ) -> StructuredProjection:
        tenant_id, user_id = _require_owner(run.tenant_id, run.user_id, "Run")
        _validate_run_identifiers(run)
        _validate_run_event(run, event_type)
        memory_key = _memory_key(tenant_id, user_id, "run", run.run_id, "execution_status")
        summary = _summary(model_summary)
        structured_value = {
            "object_type": "run",
            "run_id": run.run_id,
            "request_id": run.request_id,
            "session_id": run.session_id,
            "agent_id": run.agent_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "plan_id": run.plan_id,
            "step_id": run.step_id,
            "status": run.status,
            "event_type": event_type,
            "summary": summary,
        }
        candidate = _candidate(
            source_type="run",
            source_id=run.run_id,
            source_version=source_version,
            slot="execution_status",
            operation=(
                MemoryCandidateOperation.ADD
                if event_type == "create"
                else MemoryCandidateOperation.UPDATE
            ),
            scope="task_memory",
            memory_key=memory_key,
            tenant_id=tenant_id,
            user_id=user_id,
            content=_bounded(
                f"Run {run.run_id} status={run.status}" + (f": {summary}" if summary else ""),
                _MAX_SUMMARY_CHARS,
            ),
            structured_value=structured_value,
            event_id=event_id,
        )
        return _projection(
            source_type="run",
            source_id=run.run_id,
            source_version=source_version,
            event_id=event_id,
            tenant_id=tenant_id,
            user_id=user_id,
            canonical_refs=_canonical_refs(
                run.run_id, run.request_id, run.session_id, run.plan_id, run.step_id
            ),
            candidates=[candidate],
        )

    def project_result(
        self,
        result: AgentResult,
        *,
        run: AgentRun,
        event_id: str,
        source_version: str,
        model_summary: str | Mapping | None = None,
    ) -> StructuredProjection:
        tenant_id, user_id = _require_owner(run.tenant_id, run.user_id, "Run")
        _validate_run_identifiers(run)
        _require_identifier(result.result_id, "result_id")
        _validate_result_association(result, run)
        summary = _summary(model_summary) or _result_summary(result.output)
        result_value = {
            "object_type": "result",
            "result_id": result.result_id,
            "run_id": run.run_id,
            "request_id": run.request_id,
            "session_id": run.session_id,
            "agent_id": run.agent_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "plan_id": run.plan_id,
            "step_id": run.step_id,
            "status": result.status,
            "summary": summary,
        }
        candidates = [
            _candidate(
                source_type="result",
                source_id=result.result_id,
                source_version=source_version,
                slot="result_status",
                operation=MemoryCandidateOperation.ADD,
                scope="task_memory",
                memory_key=_memory_key(
                    tenant_id, user_id, "result", result.result_id, "result_status"
                ),
                tenant_id=tenant_id,
                user_id=user_id,
                content=_bounded(
                    f"Result {result.result_id} status={result.status}"
                    + (f": {summary}" if summary else ""),
                    _MAX_SUMMARY_CHARS,
                ),
                structured_value=result_value,
                event_id=event_id,
            )
        ]
        artifacts = _normalized_artifacts(result.artifact_refs)
        candidates.extend(
            self._artifact_candidate(
                artifact,
                result=result,
                run=run,
                tenant_id=tenant_id,
                user_id=user_id,
                event_id=event_id,
                source_version=source_version,
            )
            for artifact in artifacts
        )
        return _projection(
            source_type="result",
            source_id=result.result_id,
            source_version=source_version,
            event_id=event_id,
            tenant_id=tenant_id,
            user_id=user_id,
            canonical_refs=_canonical_refs(
                result.result_id,
                run.run_id,
                run.request_id,
                run.session_id,
                run.plan_id,
                run.step_id,
                *(artifact.artifact_id for artifact in artifacts),
            ),
            candidates=candidates,
        )

    def _artifact_candidate(
        self,
        artifact: ArtifactRef,
        *,
        result: AgentResult,
        run: AgentRun,
        tenant_id: str,
        user_id: str,
        event_id: str,
        source_version: str,
    ) -> MemoryFormationCandidate:
        _require_identifier(artifact.artifact_id, "artifact_id")
        value = {
            "object_type": "artifact",
            "artifact_id": artifact.artifact_id,
            "type": _bounded(artifact.type, 64),
            "uri": _normalized_uri(artifact.uri),
            "title": _bounded(artifact.title or "", 200) or None,
            "result_id": result.result_id,
            "run_id": run.run_id,
            "request_id": run.request_id,
            "session_id": run.session_id,
            "agent_id": run.agent_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "plan_id": run.plan_id,
            "step_id": run.step_id,
            "result_status": result.status,
        }
        return _candidate(
            source_type="artifact",
            source_id=f"{result.result_id}:{artifact.artifact_id}",
            source_version=source_version,
            slot="reference",
            operation=MemoryCandidateOperation.ADD,
            scope="artifact_reference",
            memory_key=_memory_key(
                tenant_id, user_id, "artifact", artifact.artifact_id, "reference"
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            content=_bounded(
                f"Artifact {artifact.artifact_id}: {artifact.title or artifact.type}",
                _MAX_SUMMARY_CHARS,
            ),
            structured_value=value,
            event_id=event_id,
        )


def _projection(
    *,
    source_type: str,
    source_id: str,
    source_version: str,
    event_id: str,
    tenant_id: str,
    user_id: str,
    canonical_refs: list[str],
    candidates: list[MemoryFormationCandidate],
) -> StructuredProjection:
    _require_identifier(event_id, "event_id")
    _require_identifier(source_id, "source_id")
    _require_identifier(source_version, "source_version")
    return StructuredProjection(
        source_type=source_type,
        source_id=source_id,
        source_version=source_version,
        event_id=event_id,
        tenant_id=tenant_id,
        user_id=user_id,
        idempotency_key=structured_event_idempotency_key(
            tenant_id=tenant_id,
            user_id=user_id,
            source_type=source_type,
            source_id=source_id,
            source_version=source_version,
        ),
        canonical_refs=_bounded_refs(canonical_refs),
        candidates=candidates,
    )


def _candidate(
    *,
    source_type: str,
    source_id: str,
    source_version: str,
    slot: str,
    operation: MemoryCandidateOperation,
    scope: str,
    memory_key: str,
    tenant_id: str,
    user_id: str,
    content: str,
    structured_value: dict,
    event_id: str,
) -> MemoryFormationCandidate:
    identity = "\x1f".join((tenant_id, user_id, source_type, source_id, source_version, slot))
    candidate_id = f"mfc_struct_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
    return MemoryFormationCandidate(
        candidate_id=candidate_id,
        proposed_operation=operation,
        scope=scope,
        content=content,
        structured_value=structured_value,
        semantic=MemoryCandidateSemantics(
            target=(
                MemorySemanticTarget.ARTIFACT
                if source_type == "artifact"
                else MemorySemanticTarget.TASK
            ),
            slot=slot,
            value=_semantic_projection_value(source_type, structured_value),
            temporal_scope=MemoryTemporalScope.CANONICAL,
            polarity=MemoryPolarity.AFFIRMED,
            certainty=MemorySemanticCertainty.CERTAIN,
            change_intent=(
                MemoryChangeIntent.SET
                if operation == MemoryCandidateOperation.ADD
                else MemoryChangeIntent.REPLACE
            ),
        ),
        subject_type="user",
        subject_id_hint=user_id,
        tenant_id_hint=tenant_id,
        memory_key_hint=memory_key,
        confidence=1.0,
        importance=0.8,
        sensitivity="normal",
        evidence_refs=[MemoryEvidenceRef(event_id=event_id, role="canonical")],
        reason=f"authoritative_{source_type}_{slot}_projection",
    )


def _semantic_projection_value(source_type: str, structured_value: dict):
    if source_type == "artifact":
        return {
            "artifact_id": structured_value.get("artifact_id"),
            "uri": structured_value.get("uri"),
        }
    return structured_value.get("status")


def _require_owner(tenant_id: str | None, user_id: str | None, object_type: str) -> tuple[str, str]:
    if not tenant_id or not tenant_id.strip() or not user_id or not user_id.strip():
        raise ValueError(f"{object_type} projection requires stored tenant/user ownership")
    if len(tenant_id) > 128 or len(user_id) > 128:
        raise ValueError(f"{object_type} projection ownership exceeds 128 characters")
    return tenant_id, user_id


def _validate_plan_event(plan: Plan, event_type: PlanProjectionEvent) -> None:
    expected = {"confirm": "running", "cancel": "cancelled"}.get(event_type)
    if expected is not None and plan.status != expected:
        raise ValueError(f"Plan {event_type} projection requires status={expected}")


def _validate_plan_identifiers(plan: Plan) -> None:
    _require_identifier(plan.plan_id, "plan_id")
    _require_optional_identifier(plan.session_id, "session_id")
    _require_optional_identifier(plan.current_step_id, "current_step_id")
    for step in plan.steps:
        _require_identifier(step.step_id, "step_id")
        _require_identifier(step.agent_id, "step agent_id")
        for dependency in step.depends_on:
            _require_identifier(dependency, "step dependency")
        for artifact in step.artifact_refs:
            _require_identifier(artifact.artifact_id, "step artifact_id")


def _validate_run_identifiers(run: AgentRun) -> None:
    _require_identifier(run.run_id, "run_id")
    _require_optional_identifier(run.request_id, "request_id")
    _require_identifier(run.session_id, "session_id")
    _require_identifier(run.agent_id, "agent_id")
    _require_optional_identifier(run.plan_id, "plan_id")
    _require_optional_identifier(run.step_id, "step_id")


def _validate_run_event(run: AgentRun, event_type: RunProjectionEvent) -> None:
    if event_type == "complete" and run.status != "completed":
        raise ValueError("Run complete projection requires status=completed")
    if event_type == "fail" and run.status not in {"failed", "invalid_output"}:
        raise ValueError("Run fail projection requires a failed status")


def _validate_result_association(result: AgentResult, run: AgentRun) -> None:
    if (
        result.run_id != run.run_id
        or result.session_id != run.session_id
        or result.agent_id != run.agent_id
        or not result.tenant_id
        or not result.user_id
        or result.tenant_id != run.tenant_id
        or result.user_id != run.user_id
    ):
        raise ValueError("Result does not belong to the canonical Run")
    if result.plan_id is not None and result.plan_id != run.plan_id:
        raise ValueError("Result plan relationship differs from the canonical Run")
    if result.step_id is not None and result.step_id != run.step_id:
        raise ValueError("Result step relationship differs from the canonical Run")


def _current_step(plan: Plan) -> PlanStep | None:
    if plan.status in _TERMINAL_PLAN_STATUSES:
        return next(
            (step for step in plan.steps if step.step_id == plan.current_step_id),
            None,
        )
    return next(
        (step for step in plan.steps if step.step_id == plan.current_step_id),
        next(
            (step for step in plan.steps if step.status in {"running", "blocked", "pending"}),
            None,
        ),
    )


def _next_step(plan: Plan, current: PlanStep | None) -> PlanStep | None:
    if plan.status in _TERMINAL_PLAN_STATUSES:
        return None
    completed = {step.step_id for step in plan.steps if step.status == "completed"}
    for step in plan.steps:
        if step is current or step.status != "pending":
            continue
        if all(parent in completed for parent in step.depends_on):
            return step
    return None


def _step_projection(step: PlanStep | None) -> dict | None:
    if step is None:
        return None
    return {
        "step_id": step.step_id,
        "agent_id": step.agent_id,
        "description": _bounded(step.description, _MAX_DESCRIPTION_CHARS),
        "status": step.status,
        "depends_on": _bounded_refs(step.depends_on),
        "artifact_refs": _bounded_refs([ref.artifact_id for ref in step.artifact_refs]),
    }


def _plan_content(
    plan: Plan,
    current: PlanStep | None,
    next_step: PlanStep | None,
    summary: str,
    last_activity_at: datetime,
) -> str:
    parts = [
        f"Plan {plan.plan_id} status={plan.status}",
        f"last_activity_at={last_activity_at.isoformat()}",
    ]
    if current is not None:
        parts.append(
            f"current={current.step_id}:{current.status}:{_bounded(current.description, _MAX_DESCRIPTION_CHARS)}"
        )
    if next_step is not None:
        parts.append(
            f"next={next_step.step_id}:{next_step.status}:{_bounded(next_step.description, _MAX_DESCRIPTION_CHARS)}"
        )
    if summary:
        parts.append(summary)
    return _bounded("; ".join(parts), _MAX_SUMMARY_CHARS)


def _summary(value: str | Mapping | None) -> str:
    if isinstance(value, str):
        return _bounded(value, _MAX_SUMMARY_CHARS)
    if isinstance(value, Mapping) and isinstance(value.get("summary"), str):
        return _bounded(value["summary"], _MAX_SUMMARY_CHARS)
    return ""


def _result_summary(value: dict | None) -> str:
    if not value:
        return ""
    for key in ("summary", "message", "title"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return _bounded(candidate, _MAX_SUMMARY_CHARS)
    return ""


def _normalized_artifacts(raw_refs: list[dict]) -> list[ArtifactRef]:
    result = []
    seen = set()
    for artifact in normalize_artifact_refs(raw_refs):
        if artifact.artifact_id in seen:
            continue
        seen.add(artifact.artifact_id)
        result.append(artifact)
        if len(result) >= 49:
            break
    return result


def _normalized_uri(value: str) -> str:
    parsed = urlsplit(value)
    clean_netloc = parsed.netloc
    if parsed.netloc:
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Artifact URI has an invalid port") from exc
        clean_netloc = f"{hostname}:{port}" if port is not None else hostname
    value = urlunsplit((parsed.scheme, clean_netloc, parsed.path, "", ""))
    if not value:
        raise ValueError("Artifact URI has no stable reference after redaction")
    return _bounded_ref(value)


def _memory_key(tenant_id: str, user_id: str, object_type: str, object_id: str, slot: str) -> str:
    value = ":".join(
        (
            "tenant",
            _key_segment("tenant", tenant_id),
            "user",
            _key_segment("user", user_id),
            object_type,
            _key_segment(object_type, object_id),
            slot,
        )
    )
    if len(value) > 512:
        raise ValueError("structured projection memory_key exceeds 512 characters")
    return value


def _key_segment(kind: str, value: str) -> str:
    encoded = quote(value, safe="-_.~")
    if len(encoded) <= 128:
        return encoded
    return f"{kind}-sha256-{hashlib.sha256(value.encode()).hexdigest()}"


def _canonical_refs(*values: str | None) -> list[str]:
    return _bounded_refs([value for value in values if value])


def _bounded_refs(values: list[str]) -> list[str]:
    result = []
    for value in values:
        bounded = _bounded_ref(value)
        if bounded not in result:
            result.append(bounded)
        if len(result) >= 50:
            break
    return result


def _bounded_ref(value: str) -> str:
    if len(value) <= _MAX_REF_CHARS:
        return value
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _require_identifier(value: str, field: str) -> None:
    if not value or not value.strip() or len(value) > 128:
        raise ValueError(f"{field} must be a non-empty identifier of at most 128 characters")


def _require_optional_identifier(value: str | None, field: str) -> None:
    if value is not None:
        _require_identifier(value, field)


__all__ = ["StructuredEventProjector", "StructuredProjection"]
