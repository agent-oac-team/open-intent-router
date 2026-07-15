import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.schemas.memory import (
    MemoryEvent,
    MemoryIndexOperation,
    MemoryIndexOperationStatus,
    MemoryIndexOperationType,
    MemoryIndexStatus,
    MemoryItem,
)
from app.services.memory_adapter import (
    MemoryIndexOperationResult,
    MemoryProviderOperationStatus,
    MemoryProviderRecord,
    MemoryStrategyAdapter,
)


@dataclass(frozen=True)
class MemoryIndexWorkerResult:
    operation: MemoryIndexOperation
    provider_result: MemoryIndexOperationResult
    completed: bool


@dataclass(frozen=True)
class MemoryIndexRepairResult:
    tenant_id: str
    status: str
    canonical_count: int = 0
    added_count: int = 0
    updated_count: int = 0
    adopted_count: int = 0
    duplicate_deleted_count: int = 0
    orphan_deleted_count: int = 0
    delete_finalized_count: int = 0
    compensation_retry_count: int = 0
    failed_count: int = 0
    error_code: str | None = None


class MemoryIndexOperationWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        adapter: MemoryStrategyAdapter,
        repository,
        outbox,
        lifecycle_store,
        owner: str,
        claim_tenant_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.repository = repository
        self.outbox = outbox
        self.lifecycle_store = lifecycle_store
        self.owner = owner
        self.claim_tenant_id = claim_tenant_id
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self) -> MemoryIndexWorkerResult | None:
        now = self.clock()
        operation = await self.outbox.claim(
            owner=self.owner,
            now=now,
            lease_seconds=self.settings.memory_formation_lease_seconds,
            tenant_id=self.claim_tenant_id,
        )
        if operation is None:
            return None
        item = await self.repository.get_by_id(operation.memory_id, tenant_id=operation.tenant_id)
        if operation.operation != MemoryIndexOperationType.DELETE and (
            item is None or item.lifecycle_status != "active" or _expired(item, now)
        ):
            return await self._complete_superseded(operation, item)
        provider_started = time.perf_counter()
        provider_result = await self.adapter.execute_index_operation(operation, item=item)
        provider_latency_ms = max(0, int((time.perf_counter() - provider_started) * 1000))
        operation = operation.model_copy(
            update={
                "last_error_metadata": {
                    **operation.last_error_metadata,
                    "provider_latency_ms": provider_latency_ms,
                }
            }
        )
        if provider_result.completed:
            return await self._complete(operation, item, provider_result)
        return await self._fail(operation, item, provider_result)

    async def _complete_superseded(
        self, operation: MemoryIndexOperation, item: MemoryItem | None
    ) -> MemoryIndexWorkerResult:
        now = self.clock()
        provider_result = MemoryIndexOperationResult(
            operation=operation.operation,
            status=MemoryProviderOperationStatus.SUPERSEDED,
            memory_id=operation.memory_id,
            error_code="canonical_not_indexable",
        )
        completed = await self.outbox.complete(
            operation.index_operation_id,
            owner=self.owner,
            lease_token=_required_lease_token(operation),
            now=now,
        )
        await self.repository.add_event(
            _index_event(
                completed,
                item=item,
                provider_status=MemoryProviderOperationStatus.SUPERSEDED.value,
                now=now,
                error_code="canonical_not_indexable",
            )
        )
        return MemoryIndexWorkerResult(
            operation=completed,
            provider_result=provider_result,
            completed=True,
        )

    async def _complete(
        self,
        operation: MemoryIndexOperation,
        item: MemoryItem | None,
        provider_result: MemoryIndexOperationResult,
    ) -> MemoryIndexWorkerResult:
        now = self.clock()
        if operation.operation != MemoryIndexOperationType.DELETE:
            if item is None or provider_result.external_memory_id is None:
                return await self._fail(
                    operation,
                    item,
                    MemoryIndexOperationResult(
                        operation=operation.operation,
                        status=MemoryProviderOperationStatus.RETRYABLE_ERROR,
                        memory_id=operation.memory_id,
                        error_code="provider_mapping_missing",
                    ),
                )
            updated = await self.repository.set_index_state(
                memory_id=operation.memory_id,
                tenant_id=operation.tenant_id,
                expected_revision_id=item.current_revision_id,
                status=MemoryIndexStatus.READY,
                external_memory_id=provider_result.external_memory_id,
                updated_at=now,
            )
            if updated is None:
                return await self._fail(
                    operation,
                    item,
                    MemoryIndexOperationResult(
                        operation=operation.operation,
                        status=MemoryProviderOperationStatus.RETRYABLE_ERROR,
                        memory_id=operation.memory_id,
                        external_memory_id=provider_result.external_memory_id,
                        error_code="canonical_revision_changed",
                    ),
                )
        completed = await self.outbox.complete(
            operation.index_operation_id,
            owner=self.owner,
            lease_token=_required_lease_token(operation),
            now=now,
            external_memory_id=provider_result.external_memory_id,
            result_metadata={
                **operation.last_error_metadata,
                "provider_status": provider_result.status.value,
            },
        )
        if operation.operation == MemoryIndexOperationType.DELETE and not _is_repair_compensation(
            operation
        ):
            await self.lifecycle_store.complete_delete_from_index(
                completed,
                now=now,
                provider_status=provider_result.status.value,
            )
        elif operation.operation == MemoryIndexOperationType.DELETE:
            await self.repository.add_event(
                _index_event(
                    completed,
                    item=item,
                    provider_status=provider_result.status.value,
                    now=now,
                )
            )
        else:
            await self.repository.add_event(
                _index_event(
                    completed,
                    item=item,
                    provider_status=provider_result.status.value,
                    now=now,
                    adopted=provider_result.adopted,
                    duplicate_count=len(provider_result.duplicate_external_ids),
                )
            )
        return MemoryIndexWorkerResult(
            operation=completed,
            provider_result=provider_result,
            completed=True,
        )

    async def _fail(
        self,
        operation: MemoryIndexOperation,
        item: MemoryItem | None,
        provider_result: MemoryIndexOperationResult,
    ) -> MemoryIndexWorkerResult:
        now = self.clock()
        error_code = provider_result.error_code or "provider_operation_failed"
        delay = min(
            self.settings.memory_formation_retry_base_seconds
            * (2 ** max(operation.attempt_count - 1, 0)),
            self.settings.memory_formation_retry_max_seconds,
        )
        failed = await self.outbox.fail(
            operation.index_operation_id,
            owner=self.owner,
            lease_token=_required_lease_token(operation),
            now=now,
            error_code=error_code[:128],
            next_attempt_at=now + timedelta(seconds=delay),
        )
        dead_letter = failed.status == MemoryIndexOperationStatus.DEAD_LETTER
        if operation.operation == MemoryIndexOperationType.DELETE and item is not None:
            await self.lifecycle_store.record_delete_index_failure(
                failed,
                error_code=error_code,
                dead_letter=dead_letter,
                now=now,
            )
        elif item is not None and item.lifecycle_status == "active":
            await self.repository.set_index_state(
                memory_id=operation.memory_id,
                tenant_id=operation.tenant_id,
                expected_revision_id=item.current_revision_id,
                status=(
                    MemoryIndexStatus.DEAD_LETTER if dead_letter else MemoryIndexStatus.OUT_OF_SYNC
                ),
                updated_at=now,
            )
            await self.repository.add_event(
                _index_event(
                    failed,
                    item=item,
                    provider_status="dead_letter" if dead_letter else "retry",
                    now=now,
                    error_code=error_code,
                )
            )
        return MemoryIndexWorkerResult(
            operation=failed,
            provider_result=provider_result,
            completed=False,
        )


class MemoryIndexRepairService:
    def __init__(
        self,
        *,
        adapter: MemoryStrategyAdapter,
        repository,
        outbox,
        lifecycle_store,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.repository = repository
        self.outbox = outbox
        self.lifecycle_store = lifecycle_store
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run_tenant(
        self, *, tenant_id: str, rebuild: bool = False, limit: int = 1000
    ) -> MemoryIndexRepairResult:
        started = time.perf_counter()
        result = await self._run_tenant(tenant_id=tenant_id, rebuild=rebuild, limit=limit)
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        occurred_at = self.clock()
        identity = f"{tenant_id}:{occurred_at.isoformat()}:{rebuild}"
        await self.repository.add_event(
            MemoryEvent(
                event_id=f"mevt_{hashlib.sha256(identity.encode()).hexdigest()[:32]}",
                event_type="memory_index_repair",
                tenant_id=tenant_id,
                payload={
                    "status": result.status,
                    "rebuild": rebuild,
                    "latency_ms": latency_ms,
                    "canonical_count": result.canonical_count,
                    "added_count": result.added_count,
                    "updated_count": result.updated_count,
                    "adopted_count": result.adopted_count,
                    "duplicate_deleted_count": result.duplicate_deleted_count,
                    "orphan_deleted_count": result.orphan_deleted_count,
                    "delete_finalized_count": result.delete_finalized_count,
                    "compensation_retry_count": result.compensation_retry_count,
                    "failed_count": result.failed_count,
                    "error_code": result.error_code,
                },
                created_at=occurred_at,
            )
        )
        return result

    async def _run_tenant(
        self, *, tenant_id: str, rebuild: bool = False, limit: int = 1000
    ) -> MemoryIndexRepairResult:
        finalized = await self._finalize_completed_deletes(tenant_id, limit)
        canonical = await self._all_canonical(tenant_id=tenant_id, page_size=limit)
        records, scan_error = await self._all_provider_records(tenant_id=tenant_id, page_size=limit)
        if scan_error is not None:
            return MemoryIndexRepairResult(
                tenant_id=tenant_id,
                status="degraded",
                canonical_count=len(canonical),
                delete_finalized_count=finalized,
                failed_count=1,
                error_code=scan_error,
            )
        duplicate_deleted = 0
        orphan_deleted = 0
        compensation_retries = 0
        failures = 0
        if rebuild:
            for record in records:
                result = await self.adapter.delete_provider_record(
                    memory_id=record.memory_id or "orphan",
                    external_memory_id=record.external_memory_id,
                )
                if result.completed:
                    orphan_deleted += 1
                else:
                    failures += 1
            if failures:
                return MemoryIndexRepairResult(
                    tenant_id=tenant_id,
                    status="error",
                    canonical_count=len(canonical),
                    orphan_deleted_count=orphan_deleted,
                    delete_finalized_count=finalized,
                    failed_count=failures,
                    error_code="rebuild_delete_failed",
                )
            records = []
            canonical = await self._all_canonical(tenant_id=tenant_id, page_size=limit)

        by_memory: dict[str, list[MemoryProviderRecord]] = {}
        for record in records:
            if record.memory_id:
                by_memory.setdefault(record.memory_id, []).append(record)
        added = 0
        updated = 0
        adopted = 0
        retained_external_ids: set[str] = set()
        for snapshot in canonical:
            item = await self.repository.get_by_id(snapshot.memory_id, tenant_id=tenant_id)
            if not _same_indexable_revision(item, snapshot, self.clock()):
                failures += 1
                continue
            matches = by_memory.get(item.memory_id, [])
            keeper = _repair_keeper(matches, item)
            if keeper is not None:
                for duplicate in matches:
                    if duplicate.external_memory_id == keeper.external_memory_id:
                        continue
                    deleted = await self.adapter.delete_provider_record(
                        memory_id=item.memory_id,
                        external_memory_id=duplicate.external_memory_id,
                    )
                    if deleted.completed:
                        duplicate_deleted += 1
                    else:
                        failures += 1
                operation_type = MemoryIndexOperationType.UPDATE
                external_id = keeper.external_memory_id
            else:
                operation_type = MemoryIndexOperationType.ADD
                external_id = None
            operation = _repair_operation(
                item, operation_type=operation_type, external_memory_id=external_id
            )
            result = await self.adapter.execute_index_operation(operation, item=item)
            if not result.completed or result.external_memory_id is None:
                failures += 1
                await self.repository.set_index_state(
                    memory_id=item.memory_id,
                    tenant_id=tenant_id,
                    expected_revision_id=item.current_revision_id,
                    status=MemoryIndexStatus.OUT_OF_SYNC,
                    updated_at=self.clock(),
                )
                continue
            stored = await self.repository.set_index_state(
                memory_id=item.memory_id,
                tenant_id=tenant_id,
                expected_revision_id=item.current_revision_id,
                status=MemoryIndexStatus.READY,
                external_memory_id=result.external_memory_id,
                updated_at=self.clock(),
            )
            if stored is None:
                failures += 1
                compensated = await self._compensate_lost_cas(
                    tenant_id=tenant_id,
                    snapshot=item,
                    external_memory_id=result.external_memory_id,
                )
                compensation_retries += int(not compensated)
                continue
            retained_external_ids.add(result.external_memory_id)
            if operation_type == MemoryIndexOperationType.ADD:
                added += 1
            else:
                updated += 1
                adopted += int(item.metadata.get("mem0_memory_id") != result.external_memory_id)

        for record in records:
            if record.external_memory_id in retained_external_ids:
                continue
            if record.memory_id:
                latest = await self.repository.get_by_id(record.memory_id, tenant_id=tenant_id)
                if _item_indexable(latest, self.clock()):
                    continue
            deleted = await self.adapter.delete_provider_record(
                memory_id=record.memory_id or "orphan",
                external_memory_id=record.external_memory_id,
            )
            if deleted.completed:
                orphan_deleted += 1
            else:
                failures += 1
        return MemoryIndexRepairResult(
            tenant_id=tenant_id,
            status="ok" if failures == 0 else "error",
            canonical_count=len(canonical),
            added_count=added,
            updated_count=updated,
            adopted_count=adopted,
            duplicate_deleted_count=duplicate_deleted,
            orphan_deleted_count=orphan_deleted,
            delete_finalized_count=finalized,
            compensation_retry_count=compensation_retries,
            failed_count=failures,
            error_code=(
                "compensation_delete_retry"
                if compensation_retries
                else ("repair_operation_failed" if failures else None)
            ),
        )

    async def _compensate_lost_cas(
        self,
        *,
        tenant_id: str,
        snapshot: MemoryItem,
        external_memory_id: str,
    ) -> bool:
        operation = _repair_compensation_operation(
            snapshot,
            tenant_id=tenant_id,
            external_memory_id=external_memory_id,
        )
        await self.outbox.add(operation)
        deleted = await self.adapter.delete_provider_record(
            memory_id=snapshot.memory_id,
            external_memory_id=external_memory_id,
        )
        latest = await self.repository.get_by_id(snapshot.memory_id, tenant_id=tenant_id)
        if _item_indexable(latest, self.clock()):
            await self.repository.set_index_state(
                memory_id=latest.memory_id,
                tenant_id=tenant_id,
                expected_revision_id=latest.current_revision_id,
                status=MemoryIndexStatus.OUT_OF_SYNC,
                updated_at=self.clock(),
            )
        return deleted.completed

    async def _all_canonical(self, *, tenant_id: str, page_size: int) -> list[MemoryItem]:
        if page_size < 1:
            raise ValueError("Repair page size must be positive")
        values: list[MemoryItem] = []
        cursor: str | None = None
        while True:
            page = await self.repository.list_indexable(
                tenant_id=tenant_id,
                limit=page_size,
                after_memory_id=cursor,
            )
            if page and (cursor is not None and page[0].memory_id <= cursor):
                raise RuntimeError("Canonical repair pagination did not advance")
            values.extend(page)
            if len(page) < page_size:
                return values
            cursor = page[-1].memory_id

    async def _all_provider_records(
        self, *, tenant_id: str, page_size: int
    ) -> tuple[list[MemoryProviderRecord], str | None]:
        if page_size < 1:
            raise ValueError("Repair page size must be positive")
        values: list[MemoryProviderRecord] = []
        offset = 0
        while True:
            scan = await self.adapter.scan_provider_records(
                tenant_id=tenant_id,
                limit=page_size,
                offset=offset,
            )
            if scan.status != MemoryProviderOperationStatus.SUCCESS:
                return [], scan.error_code or "provider_scan_failed"
            page = [record for record in scan.records if record.tenant_id == tenant_id]
            values.extend(page)
            if len(scan.records) < page_size:
                return values, None
            offset += len(scan.records)

    async def _finalize_completed_deletes(self, tenant_id: str, limit: int) -> int:
        operations = await self.outbox.list_repair_candidates(
            tenant_id=tenant_id,
            statuses=[MemoryIndexOperationStatus.COMPLETED.value],
            limit=limit,
        )
        count = 0
        for operation in operations:
            if operation.operation != MemoryIndexOperationType.DELETE or _is_repair_compensation(
                operation
            ):
                continue
            try:
                _event, replay = await self.lifecycle_store.complete_delete_from_index(
                    operation, now=self.clock()
                )
            except ValueError:
                continue
            count += int(not replay)
        return count


def _repair_operation(
    item: MemoryItem,
    *,
    operation_type: MemoryIndexOperationType,
    external_memory_id: str | None,
) -> MemoryIndexOperation:
    identity = (
        f"repair:{item.tenant_id}:{item.memory_id}:{item.current_revision_id}:{operation_type}"
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return MemoryIndexOperation(
        index_operation_id=f"midxop_{digest[:32]}",
        idempotency_key=f"repair:{digest}",
        operation=operation_type,
        memory_id=item.memory_id,
        revision_id=item.current_revision_id,
        tenant_id=item.tenant_id or "",
        external_memory_id=external_memory_id,
    )


def _repair_compensation_operation(
    item: MemoryItem,
    *,
    tenant_id: str,
    external_memory_id: str,
) -> MemoryIndexOperation:
    identity = (
        f"repair-compensation:{tenant_id}:{item.memory_id}:"
        f"{item.current_revision_id}:{external_memory_id}"
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return MemoryIndexOperation(
        index_operation_id=f"midxop_{digest[:32]}",
        idempotency_key=f"repair-compensation:{digest}",
        operation=MemoryIndexOperationType.DELETE,
        memory_id=item.memory_id,
        revision_id=item.current_revision_id,
        tenant_id=tenant_id,
        external_memory_id=external_memory_id,
        last_error_metadata={"repair_compensation": True},
    )


def _repair_keeper(
    records: list[MemoryProviderRecord], item: MemoryItem
) -> MemoryProviderRecord | None:
    if not records:
        return None
    mapped = item.metadata.get("mem0_memory_id")
    return sorted(
        records,
        key=lambda record: (
            record.external_memory_id != mapped,
            record.revision_id != item.current_revision_id,
            record.external_memory_id,
        ),
    )[0]


def _required_lease_token(operation: MemoryIndexOperation) -> str:
    if not operation.lease_token:
        raise ValueError("Claimed index operation requires a lease token")
    return operation.lease_token


def _is_repair_compensation(operation: MemoryIndexOperation) -> bool:
    return operation.last_error_metadata.get("repair_compensation") is True


def _same_indexable_revision(
    current: MemoryItem | None, snapshot: MemoryItem, now: datetime
) -> bool:
    return (
        _item_indexable(current, now)
        and current.memory_id == snapshot.memory_id
        and current.current_revision_id == snapshot.current_revision_id
    )


def _item_indexable(item: MemoryItem | None, now: datetime) -> bool:
    return item is not None and item.lifecycle_status == "active" and not _expired(item, now)


def _expired(item: MemoryItem, now: datetime) -> bool:
    if item.ttl_expires_at is None:
        return False
    expires_at = item.ttl_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


def _index_event(
    operation: MemoryIndexOperation,
    *,
    item: MemoryItem | None,
    provider_status: str,
    now: datetime,
    adopted: bool = False,
    duplicate_count: int = 0,
    error_code: str | None = None,
) -> MemoryEvent:
    identity = f"{operation.index_operation_id}:{operation.status}:{operation.attempt_count}"
    payload = {
        "operation": operation.operation.value,
        "provider_status": provider_status,
        "external_memory_id": operation.external_memory_id,
        "revision_id": operation.revision_id,
        "adopted": adopted,
        "duplicate_count": duplicate_count,
        "provider_latency_ms": operation.last_error_metadata.get("provider_latency_ms"),
    }
    if error_code:
        payload["error_code"] = error_code[:128]
    return MemoryEvent(
        event_id=f"mevt_{hashlib.sha256(identity.encode()).hexdigest()[:32]}",
        event_type=f"memory_index_{operation.operation.value}",
        memory_id=operation.memory_id,
        user_id=item.user_id if item else None,
        tenant_id=operation.tenant_id,
        agent_id=item.agent_id if item else None,
        formation_job_id=item.formation_job_id if item else None,
        memory_key=item.memory_key if item else None,
        scope=str(item.scope) if item else None,
        payload=payload,
        created_at=now,
    )


__all__ = [
    "MemoryIndexOperationWorker",
    "MemoryIndexRepairResult",
    "MemoryIndexRepairService",
    "MemoryIndexWorkerResult",
]
