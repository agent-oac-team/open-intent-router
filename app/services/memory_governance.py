from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.memory import (
    MemoryGovernanceItem,
    MemoryGovernanceResponse,
    MemoryIndexOperationStatus,
    MemoryIndexOperationType,
)

DELETION_TIMEOUT_SECONDS = 300


class MemoryGovernanceService:
    def __init__(self, *, memory_service, clock=None) -> None:
        self.memory_service = memory_service
        self.clock = clock or (lambda: datetime.now(UTC))

    async def query(
        self,
        *,
        tenant_id: str,
        memory_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> MemoryGovernanceResponse:
        items = await self.memory_service.repository.list_active(
            tenant_id=tenant_id,
            lifecycle_statuses=["deletion_pending"],
            limit=10000,
        )
        anomalies: list[MemoryGovernanceItem] = []
        now = self.clock()
        for item in items:
            if item.tenant_id != tenant_id:
                continue
            if memory_id is not None and item.memory_id != memory_id:
                continue
            operations = await self.memory_service.index_worker.outbox.list_for_memory(
                item.memory_id, tenant_id=tenant_id
            )
            deletion = next(
                (
                    operation
                    for operation in operations
                    if operation.operation == MemoryIndexOperationType.DELETE
                ),
                None,
            )
            age = max(0.0, (now - _utc(item.updated_at)).total_seconds())
            classified = _classify(item=item, operation=deletion, age=age)
            if classified is not None:
                anomalies.append(classified)

        anomalies.sort(key=lambda value: (value.updated_at, value.memory_id), reverse=True)
        total = len(anomalies)
        if memory_id is not None:
            selected = anomalies[:1]
            return MemoryGovernanceResponse(
                items=selected,
                page=1,
                page_size=1,
                total=len(selected),
                healthy=not selected,
            )
        start = (page - 1) * page_size
        return MemoryGovernanceResponse(
            items=anomalies[start : start + page_size],
            page=page,
            page_size=page_size,
            total=total,
            healthy=total == 0,
        )


def _classify(*, item, operation, age: float) -> MemoryGovernanceItem | None:
    if operation is not None and operation.status == MemoryIndexOperationStatus.DEAD_LETTER:
        anomaly = "provider_residual" if operation.external_memory_id else "deletion_dead_letter"
        reason = "外部存储残留已确认" if operation.external_memory_id else "删除清理已停止重试"
        status = "blocked"
    elif operation is not None and operation.status == MemoryIndexOperationStatus.COMPLETED:
        anomaly = "canonical_not_closed"
        reason = "外部删除已完成，但权威状态尚未收口"
        status = "needs_attention"
    elif age >= DELETION_TIMEOUT_SECONDS:
        anomaly = "deletion_timeout"
        reason = "删除清理等待已超过 300 秒"
        status = "needs_attention"
    else:
        return None
    content = item.content.strip() or None
    return MemoryGovernanceItem(
        memory_id=item.memory_id,
        anomaly=anomaly,
        status=status,
        content=content,
        content_state="present" if content else "cleared",
        safe_reason=reason,
        deletion_pending_seconds=age,
        updated_at=item.updated_at,
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
