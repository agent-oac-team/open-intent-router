from datetime import UTC, datetime

from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.schemas.agent_context import MemoryContext
from app.schemas.memory import (
    MemoryCleanupResult,
    MemoryDebugResponse,
    MemoryEvent,
    MemoryItem,
    MemoryRecallRequest,
    MemoryRecallResponse,
    MemoryWriteCandidate,
    MemoryWriteDecision,
    default_expiration_for_scope,
)
from app.services.memory_adapter import MemoryStrategyAdapter, build_memory_adapter


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        repository: MemoryItemRepository | None = None,
        adapter: MemoryStrategyAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or MemoryItemRepository()
        self.adapter = adapter or build_memory_adapter(settings, self.repository)

    async def recall(self, request: MemoryRecallRequest) -> MemoryRecallResponse:
        if not self.settings.memory_enabled or request.max_items == 0:
            return MemoryRecallResponse(
                context=MemoryContext(status="disabled", metadata={"memory_enabled": False})
            )
        try:
            items = await self.adapter.search(request)
        except Exception as exc:
            return MemoryRecallResponse(
                context=MemoryContext(status="error", errors=[str(exc)]),
                metadata={"error": str(exc)},
            )
        active_items: list[MemoryItem] = []
        expired_count = 0
        now = datetime.now(UTC)
        for item in items:
            if item.ttl_expires_at is not None and item.ttl_expires_at <= now:
                expired_count += 1
                continue
            active_items.append(item)
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
        item = MemoryItem(
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
        )
        try:
            stored = await self.adapter.add(item)
        except Exception as exc:
            await self.repository.add_event(
                MemoryEvent(
                    event_type="memory_write_failed",
                    memory_id=item.memory_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    agent_id=candidate.agent_id,
                    payload={
                        "scope": candidate.scope,
                        "confidence": candidate.confidence,
                        "source": candidate.source,
                        "error": str(exc),
                        "mem0": self._adapter_debug_metadata(),
                    },
                )
            )
            return MemoryWriteDecision(
                candidate=candidate,
                status="rejected",
                reason="memory_write_failed",
                metadata={"error": str(exc), "mem0": self._adapter_debug_metadata()},
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
                "mem0_status": stored.metadata.get("mem0_status"),
            },
        )

    async def cleanup_expired(self) -> MemoryCleanupResult:
        list_expired = getattr(self.repository, "list_expired", None)
        if callable(list_expired):
            expired_items = await list_expired()
            expired = [item.memory_id for item in expired_items]
            await self.adapter.delete_many(expired, items=expired_items)
            expired = await self.repository.expire_before()
        else:
            expired = await self.repository.expire_before()
            await self.adapter.delete_many(expired)
        for memory_id in expired:
            await self.repository.add_event(
                MemoryEvent(event_type="memory_expired", memory_id=memory_id)
            )
        return MemoryCleanupResult(expired_memory_ids=expired, expired_count=len(expired))

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
            limit=limit,
        )
        events = await self.repository.list_events(
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            limit=limit,
        )
        return MemoryDebugResponse(
            items=items,
            events=events,
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
                    for item in items
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
            return debug_metadata()
        return {}


def _memory_summary(items) -> str:
    return "\n".join(f"- {item.content}" for item in items)


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
