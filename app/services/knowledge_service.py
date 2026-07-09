import asyncio

from app.core.config import Settings
from app.repositories.context_stores import KnowledgeRepository
from app.schemas.agent_context import KnowledgeContext
from app.schemas.knowledge import (
    KnowledgeDebugResponse,
    KnowledgeRetrievalLog,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    knowledge_item_from_chunk,
)
from app.services.knowledge_vector_store import KnowledgeVectorStore, build_knowledge_vector_store


class KnowledgeService:
    def __init__(
        self,
        settings: Settings,
        repository: KnowledgeRepository | None = None,
        vector_store: KnowledgeVectorStore | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or KnowledgeRepository()
        self.vector_store = vector_store or build_knowledge_vector_store(settings, self.repository)

    async def search(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
        if not self.settings.knowledge_enabled:
            return KnowledgeSearchResponse(
                context=KnowledgeContext(status="disabled", metadata={"knowledge_enabled": False})
            )
        try:
            return await asyncio.wait_for(
                self._search(request),
                timeout=self.settings.knowledge_prefetch_timeout_seconds,
            )
        except TimeoutError:
            response = KnowledgeSearchResponse(
                context=KnowledgeContext(status="timeout", errors=["knowledge_search_timeout"]),
                errors=["knowledge_search_timeout"],
            )
            await self._log(request, response, status="timeout")
            return response
        except Exception as exc:
            response = KnowledgeSearchResponse(
                context=KnowledgeContext(status="error", errors=[str(exc)]),
                errors=[str(exc)],
            )
            await self._log(request, response, status="error")
            return response

    async def _search(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
        requested_sources = request.source_ids or _settings_source_ids(self.settings)
        sources = await self.repository.get_sources(requested_sources)
        selected_source_ids: list[str] = []
        denied_source_ids: list[str] = []
        for source in sources:
            if not source.enabled or not _source_allows(source, request):
                denied_source_ids.append(source.source_id)
                continue
            if request.source_tags and not (set(request.source_tags) & set(source.tags)):
                continue
            selected_source_ids.append(source.source_id)
        if requested_sources:
            known = {source.source_id for source in sources}
            denied_source_ids.extend([sid for sid in requested_sources if sid not in known])
        hits = []
        if selected_source_ids and request.top_k > 0:
            hits = await self.vector_store.search(
                query=request.query,
                source_ids=selected_source_ids,
                limit=request.top_k,
            )
        items = [knowledge_item_from_chunk(chunk, score=score) for chunk, score in hits]
        citations = [item.citation for item in items if item.citation]
        status = "ok" if items else "empty"
        context = KnowledgeContext(
            summary=_knowledge_summary(items),
            items=items,
            citations=citations,
            source_ids=selected_source_ids,
            status=status,
            metadata={
                "caller_type": request.caller_type,
                "caller_id": request.caller_id,
                "purpose": request.purpose,
                "denied_source_ids": denied_source_ids,
            },
        )
        response = KnowledgeSearchResponse(
            context=context,
            denied_source_ids=denied_source_ids,
            selected_source_ids=selected_source_ids,
            metadata={"hit_count": len(items)},
        )
        await self._log(request, response, status=status)
        return response

    async def debug_state(
        self,
        *,
        source_ids: list[str] | None = None,
        caller_type: str | None = None,
        caller_id: str | None = None,
        purpose: str | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> KnowledgeDebugResponse:
        sources = await self.repository.get_sources(source_ids)
        chunks = await self.repository.list_chunks(source_ids=source_ids, limit=limit)
        logs = await self.repository.list_logs(
            caller_type=caller_type,
            caller_id=caller_id,
            purpose=purpose,
            tenant_id=tenant_id,
            limit=limit,
        )
        return KnowledgeDebugResponse(
            sources=sources,
            chunks=chunks,
            logs=logs,
            metadata={
                "knowledge_enabled": self.settings.knowledge_enabled,
                "vector_backend": self.settings.knowledge_vector_backend,
                "source_count": len(sources),
                "chunk_count": len(chunks),
                "log_count": len(logs),
            },
        )

    async def _log(
        self,
        request: KnowledgeSearchRequest,
        response: KnowledgeSearchResponse,
        *,
        status: str,
    ) -> None:
        await self.repository.add_log(
            KnowledgeRetrievalLog(
                query=request.query,
                caller_type=request.caller_type,
                caller_id=request.caller_id,
                purpose=request.purpose,
                user_id=request.user.id,
                tenant_id=request.user.tenant_id,
                selected_source_ids=response.selected_source_ids,
                denied_source_ids=response.denied_source_ids,
                hit_count=len(response.context.items),
                status=status,
                errors=response.errors,
                metadata=response.metadata,
            )
        )


def _source_allows(source, request: KnowledgeSearchRequest) -> bool:
    user = request.user
    if source.allow_roles and not (set(user.roles) & set(source.allow_roles)):
        return False
    if source.allow_groups and not (set(user.groups) & set(source.allow_groups)):
        return False
    if source.allow_tenants and "*" not in source.allow_tenants:
        if not user.tenant_id or user.tenant_id not in source.allow_tenants:
            return False
    return True


def _settings_source_ids(settings: Settings) -> list[str]:
    return [
        item.strip() for item in settings.knowledge_default_source_ids.split(",") if item.strip()
    ]


def _knowledge_summary(items) -> str:
    return "\n".join(f"- {item.content}" for item in items)
