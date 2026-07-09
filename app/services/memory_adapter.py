import inspect
import logging
from datetime import datetime
from typing import Any, Protocol

from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.schemas.memory import MemoryEvent, MemoryItem, MemoryRecallRequest, MemoryWriteCandidate
from app.services.mem0_config import build_mem0_config, mem0_health_check, mem0_static_metadata

logger = logging.getLogger(__name__)


class MemoryStrategyAdapter(Protocol):
    async def search(self, request: MemoryRecallRequest) -> list[MemoryItem]: ...

    async def add(self, item: MemoryItem) -> MemoryItem: ...

    async def extract(
        self, candidates: list[MemoryWriteCandidate]
    ) -> list[MemoryWriteCandidate]: ...

    async def delete_many(
        self,
        memory_ids: list[str],
        *,
        items: list[MemoryItem] | None = None,
    ) -> None: ...


class Mem0ClientFactory(Protocol):
    def __call__(self, config: dict[str, Any]) -> Any: ...


class Mem0AdapterError(RuntimeError):
    def __init__(self, operation: str, message: str) -> None:
        self.operation = operation
        super().__init__(f"mem0_{operation}_failed: {message}")


class RepositoryMemoryAdapter:
    def __init__(self, repository: MemoryItemRepository) -> None:
        self.repository = repository

    async def search(self, request: MemoryRecallRequest) -> list[MemoryItem]:
        return await self.repository.list_active(
            user_id=request.user.id,
            tenant_id=request.user.tenant_id,
            scopes=[str(scope) for scope in request.scopes],
            subject_type=request.subject_type,
            subject_id=request.subject_id or request.user.id,
            agent_id=request.agent_id,
            limit=request.max_items,
        )

    async def add(self, item: MemoryItem) -> MemoryItem:
        return await self.repository.add(item)

    async def extract(self, candidates: list[MemoryWriteCandidate]) -> list[MemoryWriteCandidate]:
        return candidates

    async def delete_many(
        self,
        memory_ids: list[str],
        *,
        items: list[MemoryItem] | None = None,
    ) -> None:
        return None


class Mem0MemoryAdapter:
    def __init__(
        self,
        settings: Settings,
        repository: MemoryItemRepository,
        client_factory: Mem0ClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self._client_factory = client_factory or default_mem0_client_factory
        self._client = None
        self._degraded = False
        self._last_error: str | None = None
        self._last_error_operation: str | None = None

    def _memory(self):
        if self._client is not None:
            return self._client
        self._client = self._client_factory(build_mem0_config(self.settings))
        _ensure_mem0_milvus_collection_loaded(
            self._client,
            self.settings.memory_mem0_milvus_collection,
        )
        return self._client

    def debug_metadata(self) -> dict[str, Any]:
        health = mem0_health_check(self.settings)
        if self._degraded and health.get("status") == "ok":
            health = {**health, "status": "degraded"}
        return {
            **mem0_static_metadata(self.settings),
            "initialized": self._client is not None,
            "degraded": self._degraded,
            "last_error": self._last_error,
            "last_error_operation": self._last_error_operation,
            "health": health,
        }

    async def search(self, request: MemoryRecallRequest) -> list[MemoryItem]:
        try:
            client = self._memory()
            filters = _search_filters(request)
            raw = await _call_mem0_search(
                client,
                query=request.query,
                filters=filters,
                limit=request.max_items,
            )
            items = _items_from_mem0_results(
                raw,
                request=request,
                collection=self.settings.memory_mem0_milvus_collection,
            )
            await self._record_history(
                "search",
                status="ok",
                user_id=request.user.id,
                tenant_id=request.user.tenant_id,
                agent_id=request.agent_id,
                payload={
                    "query": request.query,
                    "filters": filters,
                    "hit_count": len(items),
                    "subject_type": request.subject_type,
                    "subject_id": request.subject_id or request.user.id,
                },
            )
            return items
        except Exception as exc:
            await self._record_error("search", exc, request=request)
            if self.settings.memory_mem0_fail_closed_effective:
                raise Mem0AdapterError("search", str(exc)) from exc
            return await RepositoryMemoryAdapter(self.repository).search(request)

    async def add(self, item: MemoryItem) -> MemoryItem:
        try:
            raw = await _call_mem0_add(
                self._memory(),
                payload=_mem0_payload_for_item(item),
                user_id=item.user_id or item.subject_id,
                metadata=_mem0_metadata_for_item(item, self.settings),
            )
            mem0_memory_id = _extract_mem0_memory_id(raw)
            metadata = _stored_mem0_metadata(item, self.settings, mem0_memory_id)
            stored = await self.repository.add(item.model_copy(update={"metadata": metadata}))
            await self._record_history(
                "add",
                status="ok",
                item=stored,
                mem0_memory_id=mem0_memory_id,
                payload={"raw_result_type": type(raw).__name__},
            )
            return stored
        except Exception as exc:
            await self._record_error("add", exc, item=item)
            if self.settings.memory_mem0_fail_closed_effective:
                raise Mem0AdapterError("add", str(exc)) from exc
            metadata = {
                **item.metadata,
                "memory_provider": "repository_fallback",
                "mem0_status": "degraded",
                "mem0_error": str(exc),
                "mem0_collection": self.settings.memory_mem0_milvus_collection,
            }
            stored = await self.repository.add(item.model_copy(update={"metadata": metadata}))
            await self._record_history(
                "add",
                status="fallback",
                item=stored,
                error=str(exc),
            )
            return stored

    async def extract(self, candidates: list[MemoryWriteCandidate]) -> list[MemoryWriteCandidate]:
        return candidates

    async def delete_many(
        self,
        memory_ids: list[str],
        *,
        items: list[MemoryItem] | None = None,
    ) -> None:
        if not memory_ids:
            return None
        items_by_id = {item.memory_id: item for item in items or []}
        try:
            client = self._memory()
        except Exception as exc:
            await self._record_error("delete", exc)
            if self.settings.memory_mem0_fail_closed_effective:
                raise Mem0AdapterError("delete", str(exc)) from exc
            return None
        for memory_id in memory_ids:
            item = items_by_id.get(memory_id)
            mem0_memory_id = _delete_target(memory_id, item)
            try:
                await _call_mem0_delete(client, mem0_memory_id)
                await self._record_history(
                    "delete",
                    status="ok",
                    item=item,
                    memory_id=memory_id,
                    mem0_memory_id=mem0_memory_id,
                )
            except Exception as exc:
                await self._record_error("delete", exc, item=item, memory_id=memory_id)
                if self.settings.memory_mem0_fail_closed_effective:
                    raise Mem0AdapterError("delete", str(exc)) from exc
        return None

    async def _record_error(
        self,
        operation: str,
        exc: Exception,
        *,
        item: MemoryItem | None = None,
        request: MemoryRecallRequest | None = None,
        memory_id: str | None = None,
    ) -> None:
        self._degraded = True
        self._last_error = str(exc)
        self._last_error_operation = operation
        logger.warning("mem0 %s failed: %s", operation, exc)
        await self._record_history(
            operation,
            status="error",
            item=item,
            memory_id=memory_id,
            user_id=request.user.id if request else None,
            tenant_id=request.user.tenant_id if request else None,
            agent_id=request.agent_id if request else None,
            error=str(exc),
        )

    async def _record_history(
        self,
        operation: str,
        *,
        status: str,
        item: MemoryItem | None = None,
        memory_id: str | None = None,
        mem0_memory_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            event_payload = {
                "operation": operation,
                "status": status,
                "memory_provider": "mem0",
                "collection": self.settings.memory_mem0_milvus_collection,
                "history_backend": self.settings.memory_mem0_history_backend,
                "history_canonical": "oir_memory_events_ledger",
                "embedding_model": self.settings.memory_mem0_embedding_model
                or self.settings.knowledge_embedding_model,
                "embedding_dims": self.settings.memory_mem0_embedding_dims
                or self.settings.knowledge_embedding_dim,
                "mem0_memory_id": mem0_memory_id,
                "error": error,
                **(payload or {}),
            }
            await self.repository.add_event(
                MemoryEvent(
                    event_type=f"mem0_{operation}",
                    memory_id=memory_id or (item.memory_id if item else None),
                    user_id=user_id or (item.user_id if item else None),
                    tenant_id=tenant_id or (item.tenant_id if item else None),
                    agent_id=agent_id or (item.agent_id if item else None),
                    payload=event_payload,
                )
            )
        except Exception as exc:  # pragma: no cover - best-effort observability
            logger.warning("failed to record mem0 %s history: %s", operation, exc)


def default_mem0_client_factory(config: dict[str, Any]):
    try:
        from mem0 import Memory  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional dependency
        raise RuntimeError("mem0 is not installed or could not be imported") from exc
    return Memory.from_config(config)


def _ensure_mem0_milvus_collection_loaded(client: Any, collection_name: str) -> None:
    vector_store = getattr(client, "vector_store", None)
    milvus_client = getattr(vector_store, "client", None)
    _patch_milvus_lite_output_fields(milvus_client)
    load_collection = getattr(milvus_client, "load_collection", None)
    if not callable(load_collection):
        return
    target_collection = getattr(vector_store, "collection_name", None) or collection_name
    try:
        load_collection(collection_name=target_collection)
    except TypeError:
        load_collection(target_collection)


def _patch_milvus_lite_output_fields(milvus_client: Any) -> None:
    if milvus_client is None or getattr(milvus_client, "_oir_output_fields_patched", False):
        return
    search = getattr(milvus_client, "search", None)
    if not callable(search):
        return

    def search_with_explicit_metadata(*args: Any, **kwargs: Any) -> Any:
        if _uses_star_output_fields(kwargs.get("output_fields")):
            kwargs = {**kwargs, "output_fields": ["id", "metadata"]}
        return search(*args, **kwargs)

    milvus_client.search = search_with_explicit_metadata
    milvus_client._oir_output_fields_patched = True


def _uses_star_output_fields(output_fields: Any) -> bool:
    if isinstance(output_fields, str):
        return output_fields == "*"
    if isinstance(output_fields, (list, tuple)):
        return "*" in output_fields
    return False


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_mem0_search(
    client: Any,
    *,
    query: str,
    filters: dict[str, Any],
    limit: int,
) -> Any:
    search = getattr(client, "search", None)
    if not callable(search):
        raise RuntimeError("mem0 client does not expose search()")
    try:
        return await _maybe_await(search(query, filters=filters, top_k=limit))
    except TypeError:
        return await _maybe_await(search(query=query, filters=filters, limit=limit))


async def _call_mem0_add(
    client: Any,
    *,
    payload: Any,
    user_id: str,
    metadata: dict[str, Any],
) -> Any:
    add = getattr(client, "add", None)
    if not callable(add):
        raise RuntimeError("mem0 client does not expose add()")
    return await _maybe_await(add(payload, user_id=user_id, metadata=metadata))


async def _call_mem0_delete(client: Any, memory_id: str) -> None:
    delete = getattr(client, "delete", None)
    if not callable(delete):
        raise RuntimeError("mem0 client does not expose delete()")
    try:
        await _maybe_await(delete(memory_id=memory_id))
    except TypeError:
        await _maybe_await(delete(memory_id))


def _search_filters(request: MemoryRecallRequest) -> dict[str, Any]:
    filters: dict[str, Any] = {"user_id": request.user.id}
    metadata_filters: dict[str, Any] = {
        "tenant_id": request.user.tenant_id,
        "subject_type": request.subject_type,
        "subject_id": request.subject_id or request.user.id,
        "agent_id": request.agent_id,
    }
    for key, value in metadata_filters.items():
        if value is not None:
            filters[key] = value
    scopes = [str(scope) for scope in request.scopes]
    if len(scopes) == 1:
        filters["scope"] = scopes[0]
    elif scopes:
        filters["scope"] = {"in": scopes}
    for key, value in request.metadata_filters.items():
        filters[key] = value
    return filters


def _mem0_payload_for_item(item: MemoryItem) -> Any:
    messages = item.structured_value.get("messages") or item.metadata.get("messages")
    if isinstance(messages, list) and messages:
        return messages
    return item.content


def _mem0_metadata_for_item(item: MemoryItem, settings: Settings) -> dict[str, Any]:
    metadata = {
        **item.metadata,
        "memory_id": item.memory_id,
        "scope": str(item.scope),
        "subject_type": item.subject_type,
        "subject_id": item.subject_id,
        "user_id": item.user_id,
        "tenant_id": item.tenant_id,
        "agent_id": item.agent_id,
        "visibility": item.visibility,
        "source": item.source,
        "confidence": item.confidence,
        "importance": item.importance,
        "ttl_expires_at": item.ttl_expires_at.isoformat() if item.ttl_expires_at else None,
        "memory_provider": "mem0",
        "mem0_collection": settings.memory_mem0_milvus_collection,
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _stored_mem0_metadata(
    item: MemoryItem,
    settings: Settings,
    mem0_memory_id: str | None,
) -> dict[str, Any]:
    metadata = _mem0_metadata_for_item(item, settings)
    if mem0_memory_id:
        metadata["mem0_memory_id"] = mem0_memory_id
    return metadata


def _items_from_mem0_results(
    raw: Any,
    *,
    request: MemoryRecallRequest,
    collection: str,
) -> list[MemoryItem]:
    results = _normalise_results(raw)
    items: list[MemoryItem] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        content = result.get("memory") or result.get("content") or result.get("text") or ""
        if not content:
            continue
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        mem0_memory_id = _result_mem0_id(result)
        memory_id = str(
            metadata.get("memory_id")
            or result.get("memory_id")
            or mem0_memory_id
            or f"mem0:{index}"
        )
        item_metadata = {
            **metadata,
            "memory_provider": "mem0",
            "mem0_collection": collection,
        }
        if mem0_memory_id:
            item_metadata["mem0_memory_id"] = mem0_memory_id
        items.append(
            MemoryItem(
                memory_id=memory_id,
                scope=str(
                    metadata.get("scope")
                    or (request.scopes[0] if request.scopes else "user_preference")
                ),
                subject_type=str(metadata.get("subject_type") or request.subject_type),
                subject_id=str(metadata.get("subject_id") or request.subject_id or request.user.id),
                user_id=str(metadata.get("user_id") or request.user.id),
                tenant_id=metadata.get("tenant_id") or request.user.tenant_id,
                agent_id=metadata.get("agent_id") or request.agent_id,
                content=str(content),
                source=str(metadata.get("source") or "mem0"),
                confidence=_safe_float(
                    result.get("confidence") or metadata.get("confidence") or result.get("score"),
                    0.8,
                ),
                importance=_safe_float(metadata.get("importance"), 0.5),
                visibility=str(metadata.get("visibility") or "user"),
                ttl_expires_at=_parse_datetime(metadata.get("ttl_expires_at")),
                metadata=item_metadata,
            )
        )
    return items


def _normalise_results(raw: Any) -> list[Any]:
    if isinstance(raw, dict):
        results = raw.get("results")
        if isinstance(results, list):
            return results
        memories = raw.get("memories")
        if isinstance(memories, list):
            return memories
        return [raw]
    if isinstance(raw, list):
        return raw
    return []


def _extract_mem0_memory_id(raw: Any) -> str | None:
    for result in _normalise_results(raw):
        if isinstance(result, dict):
            memory_id = _result_mem0_id(result)
            if memory_id:
                return memory_id
    if isinstance(raw, dict):
        return _result_mem0_id(raw)
    return None


def _result_mem0_id(result: dict[str, Any]) -> str | None:
    value = result.get("id") or result.get("mem0_memory_id") or result.get("memory_id")
    return str(value) if value else None


def _delete_target(memory_id: str, item: MemoryItem | None) -> str:
    if item and item.metadata.get("mem0_memory_id"):
        return str(item.metadata["mem0_memory_id"])
    return memory_id


def _safe_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, 0.0), 1.0)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_memory_adapter(
    settings: Settings,
    repository: MemoryItemRepository,
) -> MemoryStrategyAdapter:
    if settings.memory_strategy_provider == "mem0":
        return Mem0MemoryAdapter(settings, repository)
    return RepositoryMemoryAdapter(repository)
