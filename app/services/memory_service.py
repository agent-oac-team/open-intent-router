import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from app.core.config import Settings
from app.core.redaction import redact_text, redact_value
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory_index_operations import (
    DatabaseMemoryIndexOutboxRepository,
    MemoryIndexOutboxRepository,
)
from app.repositories.memory_lifecycle_store import (
    DatabaseMemoryLifecycleStore,
    MemoryLifecycleStore,
)
from app.repositories.memory_revisions import MemoryRevisionLedgerRepository
from app.schemas.agent_context import MemoryContext, MemoryContextItem
from app.schemas.memory import (
    MemoryCleanupResult,
    MemoryDebugResponse,
    MemoryDecisionStatus,
    MemoryEvent,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationReasonCode,
    MemoryFormationTrigger,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryOperation,
    MemoryRecallRequest,
    MemoryRecallResponse,
    MemoryWriteCandidate,
    MemoryWriteDecision,
    default_expiration_for_scope,
)
from app.services.memory_adapter import MemoryStrategyAdapter, build_memory_adapter
from app.services.memory_candidate_compatibility import adapt_structured_candidate
from app.services.memory_candidate_policy import CandidatePolicyResult
from app.services.memory_indexing import MemoryIndexOperationWorker
from app.services.memory_lifecycle import MemoryLifecycleService, MemoryTtlSweeper


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        repository: MemoryItemRepository | None = None,
        adapter: MemoryStrategyAdapter | None = None,
        ttl_sweeper: MemoryTtlSweeper | None = None,
        lifecycle_store=None,
        index_outbox=None,
        index_worker: MemoryIndexOperationWorker | None = None,
        formation_repository=None,
    ) -> None:
        self.settings = settings
        self.repository = repository or MemoryItemRepository()
        self.adapter = adapter or build_memory_adapter(settings, self.repository)
        self.formation_repository = formation_repository
        built_store, built_outbox = self._build_lifecycle_components()
        self.lifecycle_store = lifecycle_store or built_store
        self.index_outbox = index_outbox or built_outbox
        self.lifecycle = MemoryLifecycleService(settings=self.settings, store=self.lifecycle_store)
        self.index_worker = index_worker or MemoryIndexOperationWorker(
            settings=self.settings,
            adapter=self.adapter,
            repository=self.repository,
            outbox=self.index_outbox,
            lifecycle_store=self.lifecycle_store,
            owner=f"explicit-memory-index-{uuid4().hex}",
        )
        self.ttl_sweeper = ttl_sweeper or self._build_ttl_sweeper()

    async def recall(self, request: MemoryRecallRequest) -> MemoryRecallResponse:
        if not self.settings.memory_enabled or request.max_items == 0:
            return MemoryRecallResponse(
                context=MemoryContext(status="disabled", metadata={"memory_enabled": False})
            )
        try:
            items = await self.adapter.search(request)
        except Exception as exc:
            safe_error = redact_text(str(exc), max_length=500)
            return MemoryRecallResponse(
                context=MemoryContext(status="error", errors=[safe_error]),
                metadata={"error": safe_error},
            )
        active_items: list[MemoryItem] = []
        expired_count = 0
        now = datetime.now(UTC)
        for item in items:
            canonical = await self.repository.get_by_id(
                item.memory_id,
                tenant_id=request.user.tenant_id,
            )
            if canonical is None or not _canonical_recall_visible(canonical, request):
                continue
            if canonical.ttl_expires_at is not None and _as_utc(canonical.ttl_expires_at) <= now:
                expired_count += 1
                continue
            active_items.append(canonical)
        context_items = [
            item.to_context_item(relevance=_relevance(request.query, item.content))
            for item in active_items[: request.max_items]
        ]
        status = "ok" if context_items else "empty"
        summary = _memory_summary(context_items)
        conflicts = _detect_conflicts(request.query, [item.content for item in active_items])
        metadata = {
            "requested_scopes": [str(scope) for scope in request.scopes],
            "agent_id": request.agent_id,
            "conflicts": conflicts,
        }
        adapter_metadata = self._adapter_debug_metadata()
        if adapter_metadata:
            metadata["mem0"] = adapter_metadata
        if request.metadata_filters.get("defer_usage_event") is not True:
            recorded = await self.record_recall_usage(
                context_items,
                user_id=request.user.id,
                tenant_id=request.user.tenant_id,
                agent_id=request.agent_id,
                consumer=_bounded_identifier(request.metadata_filters.get("consumer"))
                or "memory_api",
                request_id=_bounded_identifier(request.metadata_filters.get("request_id")),
                session_id=_bounded_identifier(request.metadata_filters.get("session_id")),
                turn_id=_bounded_identifier(request.metadata_filters.get("turn_id")),
                run_id=_bounded_identifier(request.metadata_filters.get("run_id")),
            )
            if not recorded and context_items:
                metadata["metrics_event"] = "unavailable"
        return MemoryRecallResponse(
            context=MemoryContext(
                summary=summary,
                items=context_items,
                status=status,
                metadata=metadata,
            ),
            expired_count=expired_count,
            metadata=metadata,
        )

    async def record_recall_usage(
        self,
        items: list[MemoryContextItem],
        *,
        user_id: str,
        tenant_id: str | None,
        consumer: str,
        agent_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        if not tenant_id or not items:
            return not items
        bounded_request_id = _bounded_identifier(request_id)
        bounded_session_id = _bounded_identifier(session_id)
        bounded_turn_id = _bounded_identifier(turn_id)
        bounded_run_id = _bounded_identifier(run_id)
        bounded_consumer = _bounded_identifier(consumer) or "unknown"
        try:
            for item in items[:50]:
                canonical = await self.repository.get_by_id(item.memory_id, tenant_id=tenant_id)
                stable_ref = bounded_turn_id or bounded_run_id or bounded_request_id
                event_id = None
                if stable_ref:
                    identity = "\x1f".join(
                        (
                            tenant_id,
                            user_id,
                            bounded_session_id or "",
                            stable_ref,
                            bounded_consumer,
                            item.memory_id,
                            item.current_revision_id or "",
                        )
                    )
                    event_id = _stable_id("mevt_recall", identity)
                await self.repository.add_event(
                    MemoryEvent(
                        **({"event_id": event_id} if event_id else {}),
                        event_type="memory_recall_used",
                        memory_id=item.memory_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        request_id=bounded_request_id,
                        session_id=bounded_session_id,
                        turn_id=bounded_turn_id,
                        run_id=bounded_run_id,
                        memory_key=canonical.memory_key if canonical is not None else None,
                        scope=str(item.scope),
                        payload={
                            "used_count": 1,
                            "revision_id": item.current_revision_id,
                            "consumer": bounded_consumer,
                            "projection_outcome": "included",
                            "relevance": item.relevance,
                            "confidence": item.confidence,
                        },
                    )
                )
        except Exception:
            return False
        return True

    async def link_router_recall_usage(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        request_id: str | None,
        session_id: str,
        run_id: str,
        turn_id: str,
    ) -> int:
        if not tenant_id or not request_id:
            return 0
        try:
            events = await self.repository.list_events(
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                session_id=session_id,
                limit=100,
            )
            linked = 0
            for source in events:
                payload = source.payload if isinstance(source.payload, dict) else {}
                if (
                    source.event_type != "memory_recall_used"
                    or payload.get("consumer") != "router"
                    or not source.memory_id
                ):
                    continue
                identity = f"{turn_id}\x1f{source.event_id}"
                await self.repository.add_event(
                    MemoryEvent(
                        event_id=_stable_id("mevt_recall_link", identity),
                        event_type="memory_recall_linked",
                        memory_id=source.memory_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        memory_key=source.memory_key,
                        scope=source.scope,
                        payload={
                            "source_event_id": source.event_id,
                            "revision_id": payload.get("revision_id"),
                            "consumer": "router",
                            "projection_outcome": "included",
                            "relevance": payload.get("relevance"),
                            "confidence": payload.get("confidence"),
                        },
                    )
                )
                linked += 1
            return linked
        except Exception:
            return 0

    async def write_candidates(
        self,
        *,
        candidates: list[MemoryWriteCandidate],
        user_id: str,
        tenant_id: str | None = None,
    ) -> list[MemoryWriteDecision]:
        if not self.settings.memory_enabled:
            return [
                MemoryWriteDecision(
                    candidate=candidate, status="rejected", reason="memory_disabled"
                )
                for candidate in candidates
            ]
        decisions: list[MemoryWriteDecision] = []
        for candidate in candidates:
            allowed, reason = self._candidate_allowed(candidate)
            if not allowed:
                decisions.append(
                    MemoryWriteDecision(candidate=candidate, status="rejected", reason=reason)
                )
                continue
            extracted = await self.adapter.extract([candidate])
            for extracted_candidate in extracted:
                extracted_allowed, extracted_reason = self._candidate_allowed(extracted_candidate)
                if not extracted_allowed:
                    decisions.append(
                        MemoryWriteDecision(
                            candidate=extracted_candidate,
                            status="rejected",
                            reason=extracted_reason,
                        )
                    )
                    continue
                decisions.append(
                    await self._write_allowed_candidate(
                        extracted_candidate,
                        user_id=user_id,
                        tenant_id=tenant_id,
                    )
                )
        return decisions

    async def _write_allowed_candidate(
        self,
        candidate: MemoryWriteCandidate,
        *,
        user_id: str,
        tenant_id: str | None,
    ) -> MemoryWriteDecision:
        item: MemoryItem | None = None
        provider_status: str | None = None
        try:
            if self.settings.memory_strategy_provider == "mem0":
                if not tenant_id:
                    raise ValueError("Explicit mem0 write requires tenant_id")
                stored, provider_status = await self._write_canonical_mem0_candidate(
                    candidate,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
            else:
                item = self._legacy_item(candidate, user_id=user_id, tenant_id=tenant_id)
                stored = await self.adapter.add(item)
        except Exception as exc:
            safe_error = redact_text(str(exc), max_length=500)
            await self.repository.add_event(
                MemoryEvent(
                    event_type="memory_write_failed",
                    memory_id=item.memory_id if item is not None else None,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    agent_id=candidate.agent_id,
                    payload={
                        "scope": candidate.scope,
                        "confidence": candidate.confidence,
                        "source": candidate.source,
                        "error": safe_error,
                        "mem0": self._adapter_debug_metadata(),
                    },
                )
            )
            return MemoryWriteDecision(
                candidate=candidate,
                status="rejected",
                reason="memory_write_failed",
                metadata={"error": safe_error, "mem0": self._adapter_debug_metadata()},
            )
        await self.repository.add_event(
            MemoryEvent(
                event_type="memory_written",
                memory_id=stored.memory_id,
                user_id=user_id,
                tenant_id=tenant_id,
                agent_id=candidate.agent_id,
                payload={
                    "scope": candidate.scope,
                    "confidence": candidate.confidence,
                    "source": candidate.source,
                    "provider_status": provider_status,
                },
            )
        )
        return MemoryWriteDecision(
            candidate=candidate,
            status="accepted",
            memory_id=stored.memory_id,
            metadata={
                "ttl_expires_at": stored.ttl_expires_at.isoformat()
                if stored.ttl_expires_at
                else None,
                "memory_provider": stored.metadata.get("memory_provider"),
                "mem0_memory_id": stored.metadata.get("mem0_memory_id"),
                "mem0_status": provider_status or stored.metadata.get("mem0_status"),
                "revision_id": stored.current_revision_id,
                "index_status": (
                    stored.index_status.value if stored.index_status is not None else None
                ),
            },
        )

    def _legacy_item(
        self,
        candidate: MemoryWriteCandidate,
        *,
        user_id: str,
        tenant_id: str | None,
    ) -> MemoryItem:
        return MemoryItem(
            scope=candidate.scope,
            subject_type=candidate.subject_type,
            subject_id=candidate.subject_id or user_id,
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=candidate.agent_id,
            content=candidate.content,
            structured_value=candidate.structured_value,
            source=candidate.source,
            confidence=candidate.confidence,
            importance=candidate.importance,
            ttl_expires_at=default_expiration_for_scope(
                str(candidate.scope),
                task_days=self.settings.memory_task_ttl_days,
                session_summary_days=self.settings.memory_session_summary_ttl_days,
                artifact_reference_days=self.settings.memory_artifact_reference_ttl_days,
            ),
            metadata=candidate.metadata,
            current_revision_id=f"mrev_{uuid4().hex}",
            current_revision_no=1,
        )

    async def _write_canonical_mem0_candidate(
        self,
        candidate: MemoryWriteCandidate,
        *,
        user_id: str,
        tenant_id: str,
    ) -> tuple[MemoryItem, str]:
        token = uuid4().hex
        subject_id = candidate.subject_id or user_id
        memory_key = f"tenant:{tenant_id}:user:{user_id}:explicit:{token}"
        candidate_hash = _explicit_candidate_hash(
            candidate, tenant_id=tenant_id, user_id=user_id, subject_id=subject_id
        )
        formed = adapt_structured_candidate(
            MemoryFormationCandidate(
                candidate_id=f"mfc_{token}",
                proposed_operation="add",
                scope=candidate.scope,
                content=candidate.content,
                structured_value=candidate.structured_value,
                subject_type=candidate.subject_type,
                subject_id_hint=subject_id,
                tenant_id_hint=tenant_id,
                memory_key_hint=memory_key,
                confidence=candidate.confidence,
                importance=candidate.importance,
                evidence_refs=[
                    MemoryEvidenceRef(
                        event_id=f"explicit_{token}",
                        role="user",
                        content_hash=candidate_hash,
                    )
                ],
                reason="explicit write-candidates accepted",
            ),
            trigger=MemoryFormationTrigger.MANUAL,
        )
        operation = MemoryLifecycleOperation(
            operation_id=f"mfop_{token}",
            operation=MemoryOperation.ADD,
            decision_status=MemoryDecisionStatus.ACCEPTED,
            reason_code=MemoryFormationReasonCode.ACCEPTED_NEW,
            tenant_id=tenant_id,
            user_id=user_id,
            subject_type=candidate.subject_type,
            subject_id=subject_id,
            agent_id=candidate.agent_id,
            source=candidate.source,
            metadata=candidate.metadata,
            memory_key=memory_key,
            candidate_hash=candidate_hash,
        )
        lifecycle_result = await self.lifecycle.apply(
            CandidatePolicyResult(
                candidate=formed,
                operation=operation,
                redacted_trace={
                    "decision_status": "accepted",
                    "reason_code": "accepted_new",
                },
            )
        )
        if lifecycle_result.item is None or lifecycle_result.index_operation is None:
            raise RuntimeError("Canonical explicit ADD did not create an index operation")
        provider_status = "pending"
        target_id = lifecycle_result.index_operation.index_operation_id
        for _ in range(100):
            worker_result = await self.index_worker.run_once()
            if worker_result is None:
                break
            if worker_result.operation.index_operation_id == target_id:
                provider_status = worker_result.provider_result.status.value
                break
        stored = await self.repository.get_by_id(
            lifecycle_result.item.memory_id, tenant_id=tenant_id
        )
        if stored is None:
            raise RuntimeError("Canonical explicit ADD disappeared during indexing")
        return stored, provider_status

    async def cleanup_expired(self) -> MemoryCleanupResult:
        results = await self.ttl_sweeper.run_once()
        expired = [result.operation.memory_id for result in results]
        return MemoryCleanupResult(
            expired_memory_ids=[memory_id for memory_id in expired if memory_id],
            expired_count=len(expired),
        )

    async def debug_state(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        scopes: list[str] | None = None,
        limit: int = 50,
    ) -> MemoryDebugResponse:
        items = await self.repository.list_active(
            user_id=user_id,
            tenant_id=tenant_id,
            scopes=scopes,
            agent_id=agent_id,
            lifecycle_statuses=["active", "deletion_pending"],
            limit=limit,
        )
        events = await self.repository.list_events(
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            limit=limit,
        )
        safe_items = [
            item.model_copy(
                deep=True,
                update={
                    "content": redact_text(item.content, max_length=1000),
                    "structured_value": redact_value(item.structured_value),
                    "metadata": redact_value(item.metadata),
                },
            )
            for item in items
        ]
        safe_events = [
            event.model_copy(deep=True, update={"payload": redact_value(event.payload)})
            for event in events
        ]
        return MemoryDebugResponse(
            items=safe_items,
            events=safe_events,
            metadata={
                "memory_enabled": self.settings.memory_enabled,
                "strategy_provider": self.settings.memory_strategy_provider,
                "item_count": len(items),
                "event_count": len(events),
                "mem0": self._adapter_debug_metadata(),
                "external_id_mappings": [
                    {
                        "memory_id": item.memory_id,
                        "mem0_memory_id": item.metadata.get("mem0_memory_id"),
                    }
                    for item in safe_items
                    if item.metadata.get("mem0_memory_id")
                ],
            },
        )

    def _candidate_allowed(self, candidate: MemoryWriteCandidate) -> tuple[bool, str]:
        if candidate.confidence < 0.5:
            return False, "low_confidence"
        if not candidate.content.strip():
            return False, "empty_content"
        if candidate.metadata.get("sensitive") is True:
            return False, "sensitive"
        return True, ""

    def _adapter_debug_metadata(self) -> dict:
        debug_metadata = getattr(self.adapter, "debug_metadata", None)
        if callable(debug_metadata):
            return redact_value(debug_metadata())
        return {}

    def _build_lifecycle_components(self):
        session_factory = getattr(self.repository, "session_factory", None)
        if session_factory is not None:
            store = DatabaseMemoryLifecycleStore(session_factory)
            index = DatabaseMemoryIndexOutboxRepository(session_factory)
        else:
            revisions = MemoryRevisionLedgerRepository(self.repository)
            index = MemoryIndexOutboxRepository()
            store = MemoryLifecycleStore(
                item_repository=self.repository,
                revision_repository=revisions,
                index_repository=index,
                formation_repository=self.formation_repository,
            )
        return store, index

    def _build_ttl_sweeper(self) -> MemoryTtlSweeper:
        return MemoryTtlSweeper(lifecycle=self.lifecycle, store=self.lifecycle_store)


def _memory_summary(items) -> str:
    return "\n".join(f"- {item.content}" for item in items)


def _canonical_recall_visible(item: MemoryItem, request: MemoryRecallRequest) -> bool:
    if item.lifecycle_status != "active":
        return False
    if item.tenant_id != request.user.tenant_id:
        return False
    if item.user_id not in {None, request.user.id}:
        return False
    if item.subject_type != request.subject_type:
        return False
    if item.subject_id != (request.subject_id or request.user.id):
        return False
    if request.scopes and str(item.scope) not in {str(scope) for scope in request.scopes}:
        return False
    if request.agent_id is None:
        return item.agent_id is None
    return item.agent_id in {None, request.agent_id}


def _relevance(query: str, content: str) -> float:
    query_tokens = {part.lower() for part in query.split() if part.strip()}
    if not query_tokens:
        return 0.5
    content_tokens = {part.lower() for part in content.split() if part.strip()}
    overlap = len(query_tokens & content_tokens)
    return min(max(overlap / len(query_tokens), 0.1), 1.0)


def _detect_conflicts(query: str, contents: list[str]) -> list[dict]:
    query_lower = query.lower()
    conflicts = []
    if not any(marker in query_lower for marker in ("这次", "本次", "temporarily", "this time")):
        return conflicts
    for content in contents:
        content_lower = content.lower()
        if ("中文" in content_lower and "英文" in query_lower) or (
            "english" in query_lower and "chinese" in content_lower
        ):
            conflicts.append({"memory": content, "reason": "current_input_override"})
    return conflicts


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _explicit_candidate_hash(
    candidate: MemoryWriteCandidate,
    *,
    tenant_id: str,
    user_id: str,
    subject_id: str,
) -> str:
    identity = "\x1f".join(
        (
            tenant_id,
            user_id,
            candidate.subject_type,
            subject_id,
            str(candidate.scope),
            " ".join(candidate.content.split()),
            json.dumps(
                candidate.structured_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )
    return f"sha256:{hashlib.sha256(identity.encode()).hexdigest()}"


def _bounded_identifier(value) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:32]}"
