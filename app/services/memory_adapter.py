import hashlib
import inspect
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.schemas.memory import (
    MemoryEvent,
    MemoryIndexOperation,
    MemoryIndexOperationType,
    MemoryIndexStatus,
    MemoryItem,
    MemoryRecallRequest,
    MemoryWriteCandidate,
)
from app.services.mem0_config import build_mem0_config, mem0_health_check, mem0_static_metadata

logger = logging.getLogger(__name__)


class MemoryProviderOperationStatus(StrEnum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    SUPERSEDED = "superseded"
    RETRYABLE_ERROR = "retryable_error"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class MemoryProviderRecord:
    external_memory_id: str
    memory_id: str | None
    revision_id: str | None
    tenant_id: str | None
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryProviderScanResult:
    status: MemoryProviderOperationStatus
    records: tuple[MemoryProviderRecord, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class MemoryIndexOperationResult:
    operation: MemoryIndexOperationType
    status: MemoryProviderOperationStatus
    memory_id: str
    external_memory_id: str | None = None
    adopted: bool = False
    duplicate_external_ids: tuple[str, ...] = ()
    error_code: str | None = None

    @property
    def completed(self) -> bool:
        return self.status in {
            MemoryProviderOperationStatus.SUCCESS,
            MemoryProviderOperationStatus.NOT_FOUND,
            MemoryProviderOperationStatus.SUPERSEDED,
        }


class MemoryStrategyAdapter(Protocol):
    async def search(self, request: MemoryRecallRequest) -> list[MemoryItem]: ...

    async def add(self, item: MemoryItem) -> MemoryItem: ...

    async def update(self, item: MemoryItem, *, external_memory_id: str) -> MemoryItem: ...

    async def execute_index_operation(
        self,
        operation: MemoryIndexOperation,
        *,
        item: MemoryItem | None,
    ) -> MemoryIndexOperationResult: ...

    async def scan_provider_records(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        memory_id: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> MemoryProviderScanResult: ...

    async def delete_provider_record(
        self, *, memory_id: str, external_memory_id: str
    ) -> MemoryIndexOperationResult: ...

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
        items = await self.repository.list_active(
            user_id=request.user.id,
            tenant_id=request.user.tenant_id,
            scopes=[str(scope) for scope in request.scopes],
            subject_type=request.subject_type,
            subject_id=request.subject_id or request.user.id,
            agent_id=request.agent_id,
            limit=request.max_items,
        )
        return [item for item in items if _agent_visible(item, request.agent_id)]

    async def add(self, item: MemoryItem) -> MemoryItem:
        return await self.repository.add(item)

    async def update(self, item: MemoryItem, *, external_memory_id: str) -> MemoryItem:
        return await self.repository.add(item)

    async def execute_index_operation(
        self,
        operation: MemoryIndexOperation,
        *,
        item: MemoryItem | None,
    ) -> MemoryIndexOperationResult:
        return MemoryIndexOperationResult(
            operation=operation.operation,
            status=MemoryProviderOperationStatus.DEGRADED,
            memory_id=operation.memory_id,
            external_memory_id=operation.external_memory_id,
            error_code="repository_fallback",
        )

    async def scan_provider_records(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        memory_id: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> MemoryProviderScanResult:
        return MemoryProviderScanResult(
            status=MemoryProviderOperationStatus.DEGRADED,
            error_code="repository_fallback",
        )

    async def delete_provider_record(
        self, *, memory_id: str, external_memory_id: str
    ) -> MemoryIndexOperationResult:
        return MemoryIndexOperationResult(
            operation=MemoryIndexOperationType.DELETE,
            status=MemoryProviderOperationStatus.DEGRADED,
            memory_id=memory_id,
            external_memory_id=external_memory_id,
            error_code="repository_fallback",
        )

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
            filter_sets = _search_filter_sets(request)
            candidates: list[tuple[str, str | None]] = []
            for filters in filter_sets:
                raw = await _call_mem0_search(
                    client,
                    query=request.query,
                    filters=filters,
                    limit=request.max_items,
                )
                candidates.extend(_canonical_refs_from_mem0_results(raw))
            candidates = _dedupe_provider_refs(candidates)
            canonical = await self.repository.get_active_by_ids(
                [memory_id for memory_id, _external_id in candidates],
                tenant_id=request.user.tenant_id,
                user_id=request.user.id,
                subject_type=request.subject_type,
                subject_id=request.subject_id or request.user.id,
                scopes=[str(scope) for scope in request.scopes] or None,
            )
            by_id = {item.memory_id: item for item in canonical}
            items = []
            for memory_id, external_id in candidates:
                item = by_id.get(memory_id)
                if item is None or not _agent_visible(item, request.agent_id):
                    continue
                metadata = {
                    **item.metadata,
                    "memory_provider": "mem0",
                    "mem0_collection": self.settings.memory_mem0_milvus_collection,
                }
                if external_id:
                    metadata["mem0_memory_id"] = external_id
                items.append(item.model_copy(update={"metadata": metadata}))
            items = items[: request.max_items]
            await self._record_history(
                "search",
                status="ok",
                user_id=request.user.id,
                tenant_id=request.user.tenant_id,
                agent_id=request.agent_id,
                payload={
                    "query_hash": _hash_ref(request.query),
                    "filters": filter_sets[0] if len(filter_sets) == 1 else filter_sets,
                    "hit_count": len(items),
                    "stale_or_orphan_count": max(0, len(candidates) - len(items)),
                    "subject_type": request.subject_type,
                    "subject_id": request.subject_id or request.user.id,
                },
            )
            return items
        except Exception as exc:
            await self._record_error("search", exc, request=request)
            if self.settings.memory_mem0_fail_closed_effective:
                raise Mem0AdapterError("search", _safe_exception(exc)) from exc
            return await RepositoryMemoryAdapter(self.repository).search(request)

    async def add(self, item: MemoryItem) -> MemoryItem:
        try:
            raw = await _call_mem0_add(
                self._memory(),
                payload=item.content,
                user_id=item.user_id or item.subject_id,
                metadata=_mem0_metadata_for_item(item, self.settings),
            )
            mem0_memory_id = _extract_mem0_memory_id(raw)
            if not mem0_memory_id:
                raise RuntimeError("mem0 add did not return an external memory ID")
            metadata = _stored_mem0_metadata(item, self.settings, mem0_memory_id)
            stored = await self.repository.add(
                item.model_copy(
                    update={"metadata": metadata, "index_status": MemoryIndexStatus.READY}
                )
            )
            await self._record_history(
                "add",
                status="ok",
                item=stored,
                mem0_memory_id=mem0_memory_id,
                payload={"raw_result_type": type(raw).__name__},
            )
            return stored
        except Exception as exc:
            safe_error = _safe_exception(exc)
            await self._record_error("add", exc, item=item)
            if self.settings.memory_mem0_fail_closed_effective:
                raise Mem0AdapterError("add", safe_error) from exc
            metadata = {
                **item.metadata,
                "memory_provider": "repository_fallback",
                "mem0_status": "degraded",
                "mem0_error": safe_error,
                "mem0_collection": self.settings.memory_mem0_milvus_collection,
            }
            stored = await self.repository.add(
                item.model_copy(
                    update={
                        "metadata": metadata,
                        "index_status": MemoryIndexStatus.OUT_OF_SYNC,
                    }
                )
            )
            await self._record_history(
                "add",
                status="fallback",
                item=stored,
                error=safe_error,
            )
            return stored

    async def update(self, item: MemoryItem, *, external_memory_id: str) -> MemoryItem:
        try:
            await _call_mem0_update(
                self._memory(),
                memory_id=external_memory_id,
                data=item.content,
                metadata=_mem0_metadata_for_item(item, self.settings),
            )
            metadata = _stored_mem0_metadata(item, self.settings, external_memory_id)
            stored = await self.repository.add(
                item.model_copy(
                    update={"metadata": metadata, "index_status": MemoryIndexStatus.READY}
                )
            )
            await self._record_history(
                "update",
                status="ok",
                item=stored,
                mem0_memory_id=external_memory_id,
            )
            return stored
        except Exception as exc:
            safe_error = _safe_exception(exc)
            await self._record_error("update", exc, item=item)
            if self.settings.memory_mem0_fail_closed_effective:
                raise Mem0AdapterError("update", safe_error) from exc
            metadata = {
                **item.metadata,
                "memory_provider": "repository_fallback",
                "mem0_status": "degraded",
                "mem0_error": safe_error,
                "mem0_collection": self.settings.memory_mem0_milvus_collection,
            }
            stored = await self.repository.add(
                item.model_copy(
                    update={
                        "metadata": metadata,
                        "index_status": MemoryIndexStatus.OUT_OF_SYNC,
                    }
                )
            )
            await self._record_history("update", status="fallback", item=stored, error=safe_error)
            return stored

    async def execute_index_operation(
        self,
        operation: MemoryIndexOperation,
        *,
        item: MemoryItem | None,
    ) -> MemoryIndexOperationResult:
        if operation.operation != MemoryIndexOperationType.DELETE and item is None:
            return _provider_error_result(operation, "canonical_memory_not_found")
        try:
            if operation.operation == MemoryIndexOperationType.ADD:
                return await self._execute_add(operation, item)
            if operation.operation == MemoryIndexOperationType.UPDATE:
                return await self._execute_update(operation, item)
            return await self._execute_delete(operation, item)
        except Exception as exc:
            await self._record_error("index", exc, item=item, memory_id=operation.memory_id)
            return _provider_error_result(operation, _safe_error_code(exc))

    async def scan_provider_records(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        memory_id: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> MemoryProviderScanResult:
        filters: dict[str, Any] = {"tenant_id": tenant_id}
        if user_id:
            filters["user_id"] = user_id
        if memory_id:
            filters["memory_id"] = memory_id
        try:
            raw = await _call_mem0_get_all(
                self._memory(), filters=filters, limit=limit, offset=offset
            )
            return MemoryProviderScanResult(
                status=MemoryProviderOperationStatus.SUCCESS,
                records=tuple(_provider_records_from_raw(raw)),
            )
        except Exception as exc:
            await self._record_error("scan", exc)
            return MemoryProviderScanResult(
                status=MemoryProviderOperationStatus.RETRYABLE_ERROR,
                error_code=_safe_error_code(exc),
            )

    async def delete_provider_record(
        self, *, memory_id: str, external_memory_id: str
    ) -> MemoryIndexOperationResult:
        try:
            await _call_mem0_delete(self._memory(), external_memory_id)
            return MemoryIndexOperationResult(
                operation=MemoryIndexOperationType.DELETE,
                status=MemoryProviderOperationStatus.SUCCESS,
                memory_id=memory_id,
                external_memory_id=external_memory_id,
            )
        except Exception as exc:
            if _is_not_found_error(exc):
                return MemoryIndexOperationResult(
                    operation=MemoryIndexOperationType.DELETE,
                    status=MemoryProviderOperationStatus.NOT_FOUND,
                    memory_id=memory_id,
                    external_memory_id=external_memory_id,
                )
            await self._record_error("delete", exc, memory_id=memory_id)
            return MemoryIndexOperationResult(
                operation=MemoryIndexOperationType.DELETE,
                status=MemoryProviderOperationStatus.RETRYABLE_ERROR,
                memory_id=memory_id,
                external_memory_id=external_memory_id,
                error_code=_safe_error_code(exc),
            )

    async def _execute_add(
        self, operation: MemoryIndexOperation, item: MemoryItem
    ) -> MemoryIndexOperationResult:
        scan = await self.scan_provider_records(
            tenant_id=operation.tenant_id,
            user_id=item.user_id or item.subject_id,
            memory_id=operation.memory_id,
        )
        if scan.status != MemoryProviderOperationStatus.SUCCESS:
            return _provider_error_result(operation, scan.error_code or "provider_scan_failed")
        records = _matching_provider_records(scan.records, item)
        if records:
            keeper = _preferred_provider_record(records, item)
            duplicate_ids = tuple(
                record.external_memory_id
                for record in records
                if record.external_memory_id != keeper.external_memory_id
            )
            for external_id in duplicate_ids:
                deleted = await self.delete_provider_record(
                    memory_id=item.memory_id, external_memory_id=external_id
                )
                if not deleted.completed:
                    return _provider_error_result(
                        operation, deleted.error_code or "duplicate_delete_failed"
                    )
            if keeper.revision_id != item.current_revision_id or keeper.content != item.content:
                await _call_mem0_update(
                    self._memory(),
                    memory_id=keeper.external_memory_id,
                    data=item.content,
                    metadata=_mem0_metadata_for_item(item, self.settings),
                )
            return MemoryIndexOperationResult(
                operation=operation.operation,
                status=MemoryProviderOperationStatus.SUCCESS,
                memory_id=operation.memory_id,
                external_memory_id=keeper.external_memory_id,
                adopted=True,
                duplicate_external_ids=duplicate_ids,
            )
        raw = await _call_mem0_add(
            self._memory(),
            payload=item.content,
            user_id=item.user_id or item.subject_id,
            metadata=_mem0_metadata_for_item(item, self.settings),
        )
        external_id = _extract_mem0_memory_id(raw)
        if not external_id:
            return _provider_error_result(operation, "provider_id_missing")
        return MemoryIndexOperationResult(
            operation=operation.operation,
            status=MemoryProviderOperationStatus.SUCCESS,
            memory_id=operation.memory_id,
            external_memory_id=external_id,
        )

    async def _execute_update(
        self, operation: MemoryIndexOperation, item: MemoryItem
    ) -> MemoryIndexOperationResult:
        external_id = operation.external_memory_id or _mapped_external_id(item)
        adopted = False
        if not external_id:
            scan = await self.scan_provider_records(
                tenant_id=operation.tenant_id,
                user_id=item.user_id or item.subject_id,
                memory_id=operation.memory_id,
            )
            if scan.status != MemoryProviderOperationStatus.SUCCESS:
                return _provider_error_result(operation, scan.error_code or "provider_scan_failed")
            records = _matching_provider_records(scan.records, item)
            if not records:
                return _provider_error_result(operation, "external_mapping_missing")
            keeper = _preferred_provider_record(records, item)
            external_id = keeper.external_memory_id
            adopted = True
            for duplicate in records:
                if duplicate.external_memory_id == external_id:
                    continue
                deleted = await self.delete_provider_record(
                    memory_id=item.memory_id,
                    external_memory_id=duplicate.external_memory_id,
                )
                if not deleted.completed:
                    return _provider_error_result(
                        operation, deleted.error_code or "duplicate_delete_failed"
                    )
        await _call_mem0_update(
            self._memory(),
            memory_id=external_id,
            data=item.content,
            metadata=_mem0_metadata_for_item(item, self.settings),
        )
        return MemoryIndexOperationResult(
            operation=operation.operation,
            status=MemoryProviderOperationStatus.SUCCESS,
            memory_id=operation.memory_id,
            external_memory_id=external_id,
            adopted=adopted,
        )

    async def _execute_delete(
        self, operation: MemoryIndexOperation, item: MemoryItem | None
    ) -> MemoryIndexOperationResult:
        external_ids: list[str] = []
        mapped = operation.external_memory_id or _mapped_external_id(item)
        if mapped:
            external_ids.append(mapped)
        scan = await self.scan_provider_records(
            tenant_id=operation.tenant_id,
            user_id=(item.user_id or item.subject_id) if item is not None else None,
            memory_id=operation.memory_id,
        )
        if scan.status != MemoryProviderOperationStatus.SUCCESS:
            return _provider_error_result(operation, scan.error_code or "provider_scan_failed")
        external_ids.extend(record.external_memory_id for record in scan.records)
        external_ids = list(dict.fromkeys(external_ids))
        if not external_ids:
            return MemoryIndexOperationResult(
                operation=operation.operation,
                status=MemoryProviderOperationStatus.NOT_FOUND,
                memory_id=operation.memory_id,
            )
        saw_success = False
        for external_id in external_ids:
            result = await self.delete_provider_record(
                memory_id=operation.memory_id, external_memory_id=external_id
            )
            if not result.completed:
                return _provider_error_result(
                    operation, result.error_code or "provider_delete_failed"
                )
            saw_success = saw_success or result.status == MemoryProviderOperationStatus.SUCCESS
        return MemoryIndexOperationResult(
            operation=operation.operation,
            status=(
                MemoryProviderOperationStatus.SUCCESS
                if saw_success
                else MemoryProviderOperationStatus.NOT_FOUND
            ),
            memory_id=operation.memory_id,
            external_memory_id=external_ids[0],
            duplicate_external_ids=tuple(external_ids[1:]),
        )

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
                raise Mem0AdapterError("delete", _safe_exception(exc)) from exc
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
                    raise Mem0AdapterError("delete", _safe_exception(exc)) from exc
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
        safe_error = _safe_exception(exc)
        self._degraded = True
        self._last_error = safe_error
        self._last_error_operation = operation
        logger.warning("mem0 %s failed: %s", operation, safe_error)
        await self._record_history(
            operation,
            status="error",
            item=item,
            memory_id=memory_id,
            user_id=request.user.id if request else None,
            tenant_id=request.user.tenant_id if request else None,
            agent_id=request.agent_id if request else None,
            error=safe_error,
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
    return await _maybe_await(add(payload, user_id=user_id, metadata=metadata, infer=False))


async def _call_mem0_update(
    client: Any,
    *,
    memory_id: str,
    data: str,
    metadata: dict[str, Any],
) -> Any:
    update_memory = getattr(client, "update", None)
    if not callable(update_memory):
        raise RuntimeError("mem0 client does not expose update()")
    return await _maybe_await(update_memory(memory_id=memory_id, data=data, metadata=metadata))


async def _call_mem0_get_all(
    client: Any,
    *,
    filters: dict[str, Any],
    limit: int,
    offset: int = 0,
) -> Any:
    get_all = getattr(client, "get_all", None)
    if not callable(get_all):
        raise RuntimeError("mem0 client does not expose get_all()")
    if not any(key in filters for key in ("user_id", "agent_id", "run_id")):
        vector_store = getattr(client, "vector_store", None)
        milvus_client = getattr(vector_store, "client", None)
        query = getattr(milvus_client, "query", None)
        create_filter = getattr(vector_store, "_create_filter", None)
        if callable(query) and callable(create_filter):
            return await _maybe_await(
                query(
                    collection_name=vector_store.collection_name,
                    filter=create_filter(filters),
                    limit=limit,
                    offset=offset,
                    output_fields=["id", "metadata"],
                )
            )
        list_records = getattr(vector_store, "list", None)
        if callable(list_records) and offset == 0:
            return await _maybe_await(list_records(filters=filters, top_k=limit))
    raw = await _maybe_await(get_all(filters=filters, top_k=limit + offset))
    return _slice_provider_results(raw, offset=offset, limit=limit)


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
    }
    for key, value in metadata_filters.items():
        if value is not None:
            filters[key] = value
    reserved = {
        "agent_id",
        "consumer",
        "defer_usage_event",
        "request_id",
        "run_id",
        "scope",
        "session_id",
        "subject_id",
        "subject_type",
        "tenant_id",
        "turn_id",
        "user_id",
    }
    for key, value in request.metadata_filters.items():
        if key not in reserved:
            filters[key] = value
    scopes = [str(scope) for scope in request.scopes]
    if len(scopes) == 1:
        filters["scope"] = scopes[0]
    return filters


def _search_filter_sets(request: MemoryRecallRequest) -> list[dict[str, Any]]:
    scopes = [str(scope) for scope in request.scopes]
    if len(scopes) <= 1:
        return [_search_filters(request)]
    return [_search_filters(request.model_copy(update={"scopes": [scope]})) for scope in scopes]


def _dedupe_provider_refs(items: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    deduped: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in items:
        if item[0] in seen:
            continue
        seen.add(item[0])
        deduped.append(item)
    return deduped


def _mem0_metadata_for_item(item: MemoryItem, settings: Settings) -> dict[str, Any]:
    metadata = {
        **_safe_provider_metadata(item.metadata),
        "memory_id": item.memory_id,
        "revision_id": item.current_revision_id,
        "revision_no": item.current_revision_no,
        "memory_key": item.memory_key,
        "candidate_hash": item.candidate_hash,
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
        "lifecycle_status": str(item.lifecycle_status),
        "index_status": str(item.index_status) if item.index_status is not None else None,
        "formation_job_id": item.formation_job_id,
        "canonical_refs": list(item.canonical_refs[:50]),
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


def _canonical_refs_from_mem0_results(raw: Any) -> list[tuple[str, str | None]]:
    refs: list[tuple[str, str | None]] = []
    for result in _normalise_results(raw):
        if not isinstance(result, dict):
            continue
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        memory_id = metadata.get("memory_id")
        if isinstance(memory_id, str) and memory_id:
            refs.append((memory_id, _result_mem0_id(result)))
    return refs


def _provider_records_from_raw(raw: Any) -> list[MemoryProviderRecord]:
    records = []
    for result in _normalise_results(raw):
        if not isinstance(result, dict):
            continue
        external_id = _result_mem0_id(result)
        if not external_id:
            continue
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        records.append(
            MemoryProviderRecord(
                external_memory_id=external_id,
                memory_id=_optional_string(metadata.get("memory_id")),
                revision_id=_optional_string(metadata.get("revision_id")),
                tenant_id=_optional_string(metadata.get("tenant_id")),
                content=str(
                    result.get("memory") or result.get("content") or metadata.get("data") or ""
                ),
                metadata=dict(metadata),
            )
        )
    return records


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
        if len(raw) == 1 and isinstance(raw[0], (list, tuple)):
            return [_normalise_provider_result(value) for value in raw[0]]
        return [_normalise_provider_result(value) for value in raw]
    return []


def _slice_provider_results(raw: Any, *, offset: int, limit: int) -> Any:
    if offset == 0:
        return raw
    if isinstance(raw, dict):
        for key in ("results", "memories"):
            values = raw.get(key)
            if isinstance(values, list):
                return {**raw, key: values[offset : offset + limit]}
    if isinstance(raw, list):
        return raw[offset : offset + limit]
    return raw


def _normalise_provider_result(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        return value
    dumped = model_dump()
    payload = dumped.get("payload")
    if isinstance(payload, dict):
        dumped["metadata"] = payload
        dumped["memory"] = payload.get("data", "")
    return dumped


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


def _mapped_external_id(item: MemoryItem | None) -> str | None:
    if item is None:
        return None
    return _optional_string(item.metadata.get("mem0_memory_id"))


def _matching_provider_records(
    records: tuple[MemoryProviderRecord, ...], item: MemoryItem
) -> list[MemoryProviderRecord]:
    return [
        record
        for record in records
        if record.memory_id == item.memory_id and record.tenant_id == item.tenant_id
    ]


def _preferred_provider_record(
    records: list[MemoryProviderRecord], item: MemoryItem
) -> MemoryProviderRecord:
    mapped = _mapped_external_id(item)
    return sorted(
        records,
        key=lambda record: (
            record.external_memory_id != mapped,
            record.revision_id != item.current_revision_id,
            record.external_memory_id,
        ),
    )[0]


def _safe_provider_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "authority",
        "artifact_id",
        "plan_id",
        "request_id",
        "result_id",
        "run_id",
        "session_id",
        "slot",
        "source_trace",
        "structured_event_version",
        "turn_id",
    }
    safe: dict[str, Any] = {}
    for key in allowed:
        value = metadata.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
    return safe


def _provider_error_result(
    operation: MemoryIndexOperation, error_code: str
) -> MemoryIndexOperationResult:
    return MemoryIndexOperationResult(
        operation=operation.operation,
        status=MemoryProviderOperationStatus.RETRYABLE_ERROR,
        memory_id=operation.memory_id,
        external_memory_id=operation.external_memory_id,
        error_code=error_code[:128],
    )


def _safe_error_code(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    return f"provider_{name}"[:128]


def _safe_exception(exc: Exception) -> str:
    return _safe_error_code(exc)


def _hash_ref(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _is_not_found_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 404:
        return True
    text = str(exc).casefold()
    return "not found" in text or "does not exist" in text


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _agent_visible(item: MemoryItem, agent_id: str | None) -> bool:
    if agent_id is None:
        return item.agent_id is None
    return item.agent_id in {None, agent_id}


def build_memory_adapter(
    settings: Settings,
    repository: MemoryItemRepository,
) -> MemoryStrategyAdapter:
    if settings.memory_strategy_provider == "mem0":
        return Mem0MemoryAdapter(settings, repository)
    return RepositoryMemoryAdapter(repository)


__all__ = [
    "Mem0AdapterError",
    "Mem0MemoryAdapter",
    "MemoryIndexOperationResult",
    "MemoryProviderOperationStatus",
    "MemoryProviderRecord",
    "MemoryProviderScanResult",
    "MemoryStrategyAdapter",
    "RepositoryMemoryAdapter",
    "build_memory_adapter",
]
