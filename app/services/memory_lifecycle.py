import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.redaction import redact_text, redact_value
from app.repositories.interfaces import MemoryLifecycleTransactionStore
from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryDecisionStatus,
    MemoryEvent,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTrigger,
    MemoryIndexOperation,
    MemoryIndexOperationType,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryOperation,
    MemoryRevision,
    MemoryRevisionOperation,
    default_expiration_for_scope,
)
from app.services.memory_candidate_policy import CandidatePolicyResult

CacheInvalidator = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class MemoryLifecycleResult:
    operation: MemoryLifecycleOperation
    item: MemoryItem | None = None
    revision: MemoryRevision | None = None
    event: MemoryEvent | None = None
    index_operation: MemoryIndexOperation | None = None
    idempotent_replay: bool = False


class MemoryLifecycleService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: MemoryLifecycleTransactionStore,
        cache_invalidator: CacheInvalidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.cache_invalidator = cache_invalidator
        self.clock = clock or (lambda: datetime.now(UTC))

    async def apply(self, result: CandidatePolicyResult) -> MemoryLifecycleResult:
        operation = result.operation
        candidate = result.candidate
        if operation.operation == MemoryOperation.ADD:
            return await self._add(operation, candidate)
        if operation.operation in {MemoryOperation.UPDATE, MemoryOperation.CONSOLIDATE}:
            return await self._update(operation, candidate)
        if operation.operation == MemoryOperation.DELETE:
            return await self.request_delete(operation)
        return await self._record_decision(operation, candidate)

    async def observe(self, result: CandidatePolicyResult) -> MemoryLifecycleResult:
        operation = result.operation.model_copy(
            update={
                "decision_status": MemoryDecisionStatus.OBSERVED,
                "metadata": {
                    **result.operation.metadata,
                    "observed_operation": result.operation.operation.value,
                },
            }
        )
        event, replay = await self.store.record_decision(
            _decision_event(operation, result.candidate, now=self.clock())
        )
        return MemoryLifecycleResult(
            operation=operation,
            event=event,
            idempotent_replay=replay,
        )

    async def _add(
        self, operation: MemoryLifecycleOperation, candidate: MemoryFormationCandidate
    ) -> MemoryLifecycleResult:
        _require_accepted(operation, MemoryOperation.ADD)
        memory_id = _stable_id("mem", operation.operation_id)
        revision_id = _stable_id("mrev", f"{operation.operation_id}:revision:1")
        now = self.clock()
        revision = _revision(
            operation=operation,
            candidate=candidate,
            memory_id=memory_id,
            revision_id=revision_id,
            revision_operation=MemoryRevisionOperation.ADD,
            policy_version=self.settings.memory_formation_policy_version,
            now=now,
        )
        item = MemoryItem(
            memory_id=memory_id,
            memory_key=operation.memory_key,
            scope=candidate.scope,
            subject_type=operation.subject_type,
            subject_id=operation.subject_id,
            user_id=operation.user_id,
            tenant_id=operation.tenant_id,
            agent_id=operation.agent_id,
            content=candidate.content,
            structured_value=candidate.structured_value,
            source=operation.source or "formation",
            confidence=candidate.confidence,
            importance=candidate.importance,
            ttl_expires_at=default_expiration_for_scope(
                str(candidate.scope),
                task_days=self.settings.memory_task_ttl_days,
                session_summary_days=self.settings.memory_session_summary_ttl_days,
                artifact_reference_days=self.settings.memory_artifact_reference_ttl_days,
                now=now,
            ),
            candidate_hash=operation.candidate_hash,
            formation_job_id=operation.formation_job_id,
            index_status="pending",
            canonical_refs=list(operation.canonical_refs),
            metadata=dict(operation.metadata),
            created_at=now,
            updated_at=now,
        )
        applied = operation.model_copy(update={"memory_id": memory_id, "revision_id": revision_id})
        event = _decision_event(applied, candidate, now=now)
        index = _index_operation(
            applied,
            operation_type=MemoryIndexOperationType.ADD,
            revision_id=revision_id,
            max_attempts=self.settings.memory_formation_max_attempts,
            now=now,
        )
        (
            stored_item,
            stored_revision,
            stored_event,
            stored_index,
            replay,
        ) = await self.store.commit_add(
            item=item,
            revision=revision,
            event=event,
            index_operation=index,
        )
        return MemoryLifecycleResult(
            operation=applied,
            item=stored_item,
            revision=stored_revision,
            event=stored_event,
            index_operation=stored_index,
            idempotent_replay=replay,
        )

    async def _update(
        self, operation: MemoryLifecycleOperation, candidate: MemoryFormationCandidate
    ) -> MemoryLifecycleResult:
        if operation.decision_status != MemoryDecisionStatus.ACCEPTED:
            raise ValueError("Lifecycle UPDATE requires an accepted decision")
        memory_id = _required_memory_id(operation)
        revision_id = _stable_id("mrev", f"{operation.operation_id}:revision")
        now = self.clock()
        revision_operation = (
            MemoryRevisionOperation.CONSOLIDATE
            if operation.operation == MemoryOperation.CONSOLIDATE
            else MemoryRevisionOperation.UPDATE
        )
        revision = _revision(
            operation=operation,
            candidate=candidate,
            memory_id=memory_id,
            revision_id=revision_id,
            revision_operation=revision_operation,
            policy_version=self.settings.memory_formation_policy_version,
            now=now,
        )
        applied = operation.model_copy(update={"revision_id": revision_id})
        event = _decision_event(applied, candidate, now=now)
        index = _index_operation(
            applied,
            operation_type=MemoryIndexOperationType.UPDATE,
            revision_id=revision_id,
            max_attempts=self.settings.memory_formation_max_attempts,
            now=now,
        )
        (
            stored_item,
            stored_revision,
            stored_event,
            stored_index,
            replay,
        ) = await self.store.commit_update(
            operation=operation,
            revision=revision,
            event=event,
            index_operation=index,
            candidate_hash=operation.candidate_hash,
            confidence=candidate.confidence,
            importance=candidate.importance,
            canonical_refs=operation.canonical_refs,
        )
        return MemoryLifecycleResult(
            operation=applied,
            item=stored_item,
            revision=stored_revision,
            event=stored_event,
            index_operation=stored_index,
            idempotent_replay=replay,
        )

    async def _record_decision(
        self, operation: MemoryLifecycleOperation, candidate: MemoryFormationCandidate
    ) -> MemoryLifecycleResult:
        if operation.operation not in {
            MemoryOperation.NOOP,
            MemoryOperation.REJECT,
            MemoryOperation.PENDING,
            MemoryOperation.IGNORE,
        }:
            raise ValueError("Unsupported lifecycle decision")
        event, replay = await self.store.record_decision(
            _decision_event(operation, candidate, now=self.clock())
        )
        return MemoryLifecycleResult(operation=operation, event=event, idempotent_replay=replay)

    async def request_delete(self, operation: MemoryLifecycleOperation) -> MemoryLifecycleResult:
        _require_accepted(operation, MemoryOperation.DELETE)
        memory_id = _required_memory_id(operation)
        now = self.clock()
        event = _lifecycle_event(
            operation,
            event_type="memory_deletion_requested",
            event_id=_event_id(operation.operation_id, "delete-requested"),
            now=now,
            payload={"reason_code": operation.reason_code.value, "provider_status": "pending"},
        )
        index = _index_operation(
            operation,
            operation_type=MemoryIndexOperationType.DELETE,
            revision_id=operation.revision_id,
            max_attempts=self.settings.memory_formation_max_attempts,
            now=now,
        )
        item, stored_event, stored_index, replay = await self.store.request_delete(
            operation=operation, event=event, index_operation=index
        )
        await self._invalidate(operation.tenant_id, memory_id)
        return MemoryLifecycleResult(
            operation=operation,
            item=item,
            event=stored_event,
            index_operation=stored_index,
            idempotent_replay=replay,
        )

    async def complete_delete(self, operation: MemoryLifecycleOperation) -> MemoryLifecycleResult:
        _require_accepted(operation, MemoryOperation.DELETE)
        memory_id = _required_memory_id(operation)
        tombstone = _lifecycle_event(
            operation,
            event_type="memory_deleted_tombstone",
            event_id=_event_id(operation.operation_id, "delete-completed"),
            now=self.clock(),
            payload={
                "reason_code": operation.reason_code.value,
                "operation": "delete",
                "provider_status": "completed",
            },
            include_memory_key=False,
        )
        index_operation_id = _index_operation(
            operation,
            operation_type=MemoryIndexOperationType.DELETE,
            revision_id=operation.revision_id,
            max_attempts=self.settings.memory_formation_max_attempts,
            now=self.clock(),
        ).index_operation_id
        event, replay = await self.store.complete_delete(
            operation=operation,
            tombstone=tombstone,
            index_operation_id=index_operation_id,
        )
        await self._invalidate(operation.tenant_id, memory_id)
        return MemoryLifecycleResult(operation=operation, event=event, idempotent_replay=replay)

    async def record_delete_failure(
        self,
        operation: MemoryLifecycleOperation,
        *,
        error_code: str,
        dead_letter: bool,
    ) -> MemoryLifecycleResult:
        _require_accepted(operation, MemoryOperation.DELETE)
        safe_error = (
            error_code
            if error_code and len(error_code) <= 128 and error_code.replace("_", "").isalnum()
            else "provider_delete_error"
        )
        event = _lifecycle_event(
            operation,
            event_type=("memory_deletion_dead_letter" if dead_letter else "memory_deletion_retry"),
            event_id=_event_id(
                operation.operation_id,
                f"delete-{'dead-letter' if dead_letter else 'retry'}:{safe_error}",
            ),
            now=self.clock(),
            payload={
                "reason_code": MemoryFormationReasonCode.PROVIDER_ERROR.value,
                "provider_status": "dead_letter" if dead_letter else "retry",
                "error_code": safe_error,
            },
        )
        item, stored, replay = await self.store.record_delete_failure(
            operation=operation,
            event=event,
            dead_letter=dead_letter,
        )
        await self._invalidate(operation.tenant_id, _required_memory_id(operation))
        return MemoryLifecycleResult(
            operation=operation, item=item, event=stored, idempotent_replay=replay
        )

    async def _invalidate(self, tenant_id: str, memory_id: str) -> None:
        if self.cache_invalidator is not None:
            await self.cache_invalidator(tenant_id, memory_id)


class MemoryTtlSweeper:
    def __init__(
        self,
        *,
        lifecycle: MemoryLifecycleService,
        store: MemoryLifecycleTransactionStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self, *, limit: int = 100) -> list[MemoryLifecycleResult]:
        now = self.clock()
        expired = await self.store.list_expired(now=now, limit=limit)
        results = []
        for item in expired:
            identity = "\x1f".join((item.tenant_id or "", item.memory_id, "ttl_expired"))
            operation = MemoryLifecycleOperation(
                operation_id=_stable_id("mfop", identity),
                operation=MemoryOperation.DELETE,
                decision_status=MemoryDecisionStatus.ACCEPTED,
                reason_code=MemoryFormationReasonCode.TTL_EXPIRED,
                tenant_id=item.tenant_id or "",
                user_id=item.user_id or item.subject_id,
                subject_type=item.subject_type,
                subject_id=item.subject_id,
                memory_key=item.memory_key or f"legacy-memory:{item.memory_id}",
                candidate_hash=item.candidate_hash
                or f"sha256:{hashlib.sha256(identity.encode()).hexdigest()}",
                memory_id=item.memory_id,
                revision_id=item.current_revision_id,
                canonical_refs=list(item.canonical_refs),
            )
            results.append(await self.lifecycle.request_delete(operation))
        return results


class MemoryConsolidationService:
    def __init__(
        self,
        *,
        policy,
        lifecycle: MemoryLifecycleService,
        repository=None,
        semantic_duplicate_finder=None,
        canonical_task_loader=None,
    ) -> None:
        self.policy = policy
        self.lifecycle = lifecycle
        self.repository = repository or policy.repository
        self.semantic_duplicate_finder = semantic_duplicate_finder
        self.canonical_task_loader = canonical_task_loader

    async def run_partition(
        self,
        *,
        job: MemoryFormationJob,
        turns: list | None = None,
    ) -> list[MemoryLifecycleResult]:
        if job.trigger != MemoryFormationTrigger.CONSOLIDATION:
            raise ValueError("Consolidation requires a consolidation job")
        if not job.source_refs:
            raise ValueError("Consolidation requires a canonical source ref")
        items = await self.repository.list_active(
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            subject_type="user",
            subject_id=job.user_id,
            limit=1000,
        )
        partition = [
            item
            for item in items
            if item.tenant_id == job.tenant_id
            and item.user_id == job.user_id
            and item.subject_type == "user"
            and item.subject_id == job.user_id
        ]
        evidence = MemoryEvidenceRef(event_id=job.source_refs[0], role="canonical")
        candidates = _exact_duplicate_candidates(partition, evidence)
        candidates.extend(await self._semantic_candidates(partition, evidence))
        candidates.extend(_session_summary_candidates(partition, evidence))
        if self.canonical_task_loader is not None:
            loaded = self.canonical_task_loader(job.tenant_id, job.user_id)
            if inspect.isawaitable(loaded):
                loaded = await loaded
            candidates.extend(list(loaded))
        return await self.process(
            job=job,
            candidates=candidates,
            turns=list(turns or []),
        )

    async def _semantic_candidates(
        self, items: list[MemoryItem], evidence: MemoryEvidenceRef
    ) -> list[MemoryFormationCandidate]:
        if self.semantic_duplicate_finder is None:
            return []
        matches = self.semantic_duplicate_finder(items)
        if inspect.isawaitable(matches):
            matches = await matches
        by_id = {item.memory_id: item for item in items}
        candidates = []
        for source_id, target_id, score in matches:
            source = by_id.get(source_id)
            target = by_id.get(target_id)
            if source is None or target is None or source.memory_id == target.memory_id:
                continue
            if str(source.scope) not in {
                "user_preference",
                "stable_fact",
                "session_summary",
            } or str(source.scope) != str(target.scope):
                continue
            structured = dict(source.structured_value)
            structured.update(
                {
                    "slot": _memory_slot(target),
                    "consolidation_kind": "semantic_duplicate",
                    "potential_duplicate_id": source.memory_id,
                }
            )
            candidates.append(
                MemoryFormationCandidate(
                    candidate_id=_stable_id(
                        "mfc", f"semantic:{source.memory_id}:{target.memory_id}"
                    ),
                    proposed_operation=MemoryCandidateOperation.UPDATE,
                    scope=target.scope,
                    content=source.content,
                    structured_value=structured,
                    subject_id_hint=target.subject_id,
                    tenant_id_hint=target.tenant_id,
                    memory_key_hint=target.memory_key,
                    target_memory_id=target.memory_id,
                    confidence=min(max(float(score), 0.70), 0.89),
                    importance=max(source.importance, target.importance),
                    evidence_refs=[evidence],
                    reason="semantic potential duplicate",
                )
            )
        return candidates

    async def process(
        self,
        *,
        job: MemoryFormationJob,
        candidates: list[MemoryFormationCandidate],
        turns: list,
        execute_lifecycle: bool = True,
    ) -> list[MemoryLifecycleResult]:
        if job.trigger != MemoryFormationTrigger.CONSOLIDATION:
            raise ValueError("Consolidation requires a consolidation job")
        results = []
        for candidate in candidates:
            decision = await self.policy.evaluate(job=job, candidate=candidate, turns=turns)
            if (
                decision.operation.operation == MemoryOperation.UPDATE
                and decision.operation.decision_status == MemoryDecisionStatus.ACCEPTED
            ):
                decision = CandidatePolicyResult(
                    candidate=decision.candidate,
                    operation=decision.operation.model_copy(
                        update={"operation": MemoryOperation.CONSOLIDATE}
                    ),
                    redacted_trace=decision.redacted_trace,
                )
            if execute_lifecycle:
                results.append(await self.lifecycle.apply(decision))
            else:
                results.append(MemoryLifecycleResult(operation=decision.operation))
        return results


def _exact_duplicate_candidates(
    items: list[MemoryItem], evidence: MemoryEvidenceRef
) -> list[MemoryFormationCandidate]:
    groups: dict[tuple[str, str, str, str], list[MemoryItem]] = {}
    for item in items:
        if str(item.scope) == "session_summary" or not item.memory_key:
            continue
        semantic_value = {
            key: value
            for key, value in item.structured_value.items()
            if key
            not in {
                "change_intent",
                "delete_intent",
                "lifecycle_reason",
                "privacy_subject",
                "temporal_scope",
                "consolidation_kind",
            }
        }
        identity = (
            str(item.scope),
            _memory_slot(item),
            " ".join(item.content.split()).casefold(),
            json.dumps(semantic_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        groups.setdefault(identity, []).append(item)
    candidates = []
    for duplicates in groups.values():
        ordered = sorted(duplicates, key=lambda item: (item.created_at, item.memory_id))
        if len(ordered) < 2:
            continue
        keeper = ordered[0]
        for duplicate in ordered[1:]:
            structured = dict(duplicate.structured_value)
            structured.update(
                {
                    "slot": _memory_slot(duplicate),
                    "consolidation_kind": "exact_duplicate",
                    "lifecycle_reason": "exact_duplicate",
                    "duplicate_of": keeper.memory_id,
                }
            )
            candidates.append(
                MemoryFormationCandidate(
                    candidate_id=_stable_id(
                        "mfc", f"exact:{duplicate.memory_id}:{keeper.memory_id}"
                    ),
                    proposed_operation=MemoryCandidateOperation.DELETE,
                    scope=duplicate.scope,
                    content=duplicate.content,
                    structured_value=structured,
                    subject_id_hint=duplicate.subject_id,
                    tenant_id_hint=duplicate.tenant_id,
                    memory_key_hint=duplicate.memory_key,
                    target_memory_id=duplicate.memory_id,
                    confidence=1.0,
                    importance=duplicate.importance,
                    evidence_refs=[evidence],
                    reason="exact duplicate consolidation",
                )
            )
    return candidates


def _session_summary_candidates(
    items: list[MemoryItem], evidence: MemoryEvidenceRef
) -> list[MemoryFormationCandidate]:
    summaries = sorted(
        (item for item in items if str(item.scope) == "session_summary" and item.memory_key),
        key=lambda item: (item.created_at, item.memory_id),
    )
    if len(summaries) < 2:
        return []
    keeper = summaries[0]
    content = "\n".join(item.content for item in summaries)
    content = content[:2000]
    update_structured = dict(keeper.structured_value)
    update_structured.update(
        {
            "slot": _memory_slot(keeper),
            "consolidation_kind": "session_summary",
            "source_memory_ids": [item.memory_id for item in summaries[:50]],
        }
    )
    candidates = [
        MemoryFormationCandidate(
            candidate_id=_stable_id("mfc", f"summary:{keeper.memory_id}"),
            proposed_operation=MemoryCandidateOperation.UPDATE,
            scope=keeper.scope,
            content=content,
            structured_value=update_structured,
            subject_id_hint=keeper.subject_id,
            tenant_id_hint=keeper.tenant_id,
            memory_key_hint=keeper.memory_key,
            target_memory_id=keeper.memory_id,
            confidence=1.0,
            importance=max(item.importance for item in summaries),
            evidence_refs=[evidence],
            reason="bounded session summary compression",
        )
    ]
    for source in summaries[1:]:
        structured = dict(source.structured_value)
        structured.update(
            {
                "slot": _memory_slot(source),
                "consolidation_kind": "session_summary",
                "lifecycle_reason": "session_compacted",
                "compacted_into": keeper.memory_id,
            }
        )
        candidates.append(
            MemoryFormationCandidate(
                candidate_id=_stable_id(
                    "mfc", f"summary-delete:{source.memory_id}:{keeper.memory_id}"
                ),
                proposed_operation=MemoryCandidateOperation.DELETE,
                scope=source.scope,
                content=source.content,
                structured_value=structured,
                subject_id_hint=source.subject_id,
                tenant_id_hint=source.tenant_id,
                memory_key_hint=source.memory_key,
                target_memory_id=source.memory_id,
                confidence=1.0,
                importance=source.importance,
                evidence_refs=[evidence],
                reason="session summary compacted",
            )
        )
    return candidates


def _memory_slot(item: MemoryItem) -> str:
    slot = item.structured_value.get("slot")
    if isinstance(slot, str) and slot:
        return slot
    return (item.memory_key or item.memory_id).rsplit(":", 1)[-1]


def _revision(
    *,
    operation: MemoryLifecycleOperation,
    candidate: MemoryFormationCandidate,
    memory_id: str,
    revision_id: str,
    revision_operation: MemoryRevisionOperation,
    policy_version: str,
    now: datetime,
) -> MemoryRevision:
    return MemoryRevision(
        revision_id=revision_id,
        memory_id=memory_id,
        revision_no=1,
        memory_key=operation.memory_key,
        operation=revision_operation,
        content=candidate.content,
        structured_value=candidate.structured_value,
        evidence_refs=candidate.evidence_refs,
        confidence=candidate.confidence,
        policy_version=policy_version,
        formation_job_id=operation.formation_job_id,
        created_at=now,
    )


def _decision_event(
    operation: MemoryLifecycleOperation,
    candidate: MemoryFormationCandidate,
    *,
    now: datetime,
) -> MemoryEvent:
    sensitive = operation.reason_code == MemoryFormationReasonCode.SENSITIVE_CONTENT
    event_operation = operation.model_copy(
        update={
            "metadata": {
                "scope": str(candidate.scope),
                "content_preview": redact_text(candidate.content, max_length=300),
                "content_redacted": False,
            }
        }
    )
    if sensitive:
        event_operation = operation.model_copy(
            update={
                "memory_key": _redacted_ref(operation.memory_key),
                "metadata": {
                    "scope": str(candidate.scope),
                    "content_redacted": True,
                },
            }
        )
    payload = {
        "reason_code": operation.reason_code.value,
        "candidate_hash": operation.candidate_hash,
        "confidence": candidate.confidence,
        "importance": candidate.importance,
        "operation": redact_value(event_operation.model_dump(mode="json")),
        "content_redacted": sensitive,
        "semantic_contract_version": operation.metadata.get("semantic_contract_version"),
        "semantic_validation": operation.metadata.get("semantic_validation"),
        "semantic_verifier": operation.metadata.get("semantic_verifier"),
    }
    if operation.decision_status == MemoryDecisionStatus.PENDING and not sensitive:
        payload["pending_candidate"] = _pending_candidate_snapshot(candidate)
    return _lifecycle_event(
        event_operation,
        event_type=f"memory_decision_{operation.operation.value}",
        event_id=_event_id(operation.operation_id, "decision"),
        now=now,
        payload=payload,
        scope=str(candidate.scope),
    )


def _pending_candidate_snapshot(candidate: MemoryFormationCandidate) -> dict:
    value = candidate.model_dump(mode="json")
    value["content"] = redact_text(candidate.content, max_length=2000)
    value["structured_value"] = redact_value(candidate.structured_value)
    value["evidence_refs"] = [
        {
            **evidence.model_dump(mode="json", exclude={"quote"}),
            "quote": None,
        }
        for evidence in candidate.evidence_refs[:20]
    ]
    value["reason"] = redact_text(candidate.reason, max_length=500)
    return value


def _redacted_ref(value: str) -> str:
    return f"redacted:sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _lifecycle_event(
    operation: MemoryLifecycleOperation,
    *,
    event_type: str,
    event_id: str,
    now: datetime,
    payload: dict,
    scope: str | None = None,
    include_memory_key: bool = True,
) -> MemoryEvent:
    safe_payload = {**payload, "target_hash": _operation_target_hash(operation)}
    return MemoryEvent(
        event_id=event_id,
        event_type=event_type,
        memory_id=operation.memory_id,
        user_id=operation.user_id,
        tenant_id=operation.tenant_id,
        agent_id=operation.agent_id,
        formation_job_id=operation.formation_job_id,
        memory_key=operation.memory_key if include_memory_key else None,
        decision_status=operation.decision_status,
        scope=scope,
        payload=safe_payload,
        created_at=now,
    )


def _index_operation(
    operation: MemoryLifecycleOperation,
    *,
    operation_type: MemoryIndexOperationType,
    revision_id: str | None,
    max_attempts: int,
    now: datetime,
) -> MemoryIndexOperation:
    memory_id = _required_memory_id(operation)
    identity = f"{operation.operation_id}:{operation_type.value}:{revision_id or 'none'}"
    return MemoryIndexOperation(
        index_operation_id=_stable_id("midxop", identity),
        idempotency_key=f"lifecycle:{hashlib.sha256(identity.encode()).hexdigest()}",
        operation=operation_type,
        memory_id=memory_id,
        revision_id=revision_id,
        tenant_id=operation.tenant_id,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def _event_id(operation_id: str, suffix: str) -> str:
    return _stable_id("mevt", f"{operation_id}:{suffix}")


def _operation_target_hash(operation: MemoryLifecycleOperation) -> str:
    identity = "\x1f".join(
        (
            operation.tenant_id,
            operation.user_id,
            operation.subject_type,
            operation.subject_id,
            operation.memory_key,
            operation.candidate_hash,
            operation.memory_id or "",
            operation.revision_id or "",
        )
    )
    return f"sha256:{hashlib.sha256(identity.encode()).hexdigest()}"


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _required_memory_id(operation: MemoryLifecycleOperation) -> str:
    if not operation.memory_id:
        raise ValueError("Lifecycle operation requires memory_id")
    return operation.memory_id


def _require_accepted(operation: MemoryLifecycleOperation, expected: MemoryOperation) -> None:
    if (
        operation.operation != expected
        or operation.decision_status != MemoryDecisionStatus.ACCEPTED
    ):
        raise ValueError(f"Lifecycle {expected.value.upper()} requires an accepted decision")


__all__ = [
    "MemoryConsolidationService",
    "MemoryLifecycleResult",
    "MemoryLifecycleService",
    "MemoryTtlSweeper",
]
