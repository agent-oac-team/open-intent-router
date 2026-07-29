from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.memory import (
    MemoryGovernanceItem,
    MemoryGovernanceRepairResponse,
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

    async def repair(
        self,
        *,
        tenant_id: str,
        memory_id: str,
        expected_version: str,
        expected_anomaly: str,
        idempotency_key: str,
    ) -> MemoryGovernanceRepairResponse:
        item = await self.memory_service.repository.get_by_id(memory_id, tenant_id=tenant_id)
        operations = await self.memory_service.index_worker.outbox.list_for_memory(
            memory_id, tenant_id=tenant_id
        )
        deletion = next(
            (
                operation
                for operation in operations
                if operation.operation == MemoryIndexOperationType.DELETE
            ),
            None,
        )
        if item is None:
            if deletion is not None and (
                deletion.last_error_metadata.get("governance_repair_key") == idempotency_key
            ):
                return MemoryGovernanceRepairResponse(
                    memory_id=memory_id,
                    accepted=True,
                    status="repairing",
                    action="canonical_close",
                    reason="修复请求已受理",
                    idempotent_replay=True,
                )
            return _rejected(memory_id, "目标已不在当前异常队列")
        if item.lifecycle_status != "deletion_pending" or deletion is None:
            return _rejected(memory_id, "目标当前状态不可安全修复")
        if deletion.last_error_metadata.get("governance_repair_key") == idempotency_key:
            return MemoryGovernanceRepairResponse(
                memory_id=memory_id,
                accepted=True,
                status="repairing",
                action="cleanup_advance",
                reason="修复请求已受理",
                idempotent_replay=True,
            )
        version = _version(item, deletion)
        if version != expected_version:
            return _rejected(memory_id, "目标版本已变化，请刷新后重试")
        age = max(0.0, (self.clock() - _utc(item.updated_at)).total_seconds())
        classified = _classify(item=item, operation=deletion, age=age)
        if classified is None or classified.anomaly != expected_anomaly:
            return _rejected(memory_id, "目标异常状态已变化，请刷新后重试")
        if classified.status == "repairing":
            if deletion.last_error_metadata.get("governance_repair_key") == idempotency_key:
                return MemoryGovernanceRepairResponse(
                    memory_id=memory_id,
                    accepted=True,
                    status="repairing",
                    action="cleanup_advance",
                    reason="修复请求已受理",
                    idempotent_replay=True,
                )
            return _rejected(memory_id, "目标正在修复中")

        canonical_close = expected_anomaly == "canonical_not_closed"
        try:
            _, replay = await self.memory_service.index_worker.outbox.accept_governance_repair(
                deletion.index_operation_id,
                tenant_id=tenant_id,
                expected_status=deletion.status,
                idempotency_key=idempotency_key,
                now=self.clock(),
                requeue=not canonical_close,
            )
            if canonical_close:
                await self.memory_service.lifecycle_store.complete_delete_from_index(
                    deletion,
                    now=self.clock(),
                    provider_status="completed",
                )
        except ValueError:
            return _rejected(memory_id, "目标状态或版本已变化，请刷新后重试")
        return MemoryGovernanceRepairResponse(
            memory_id=memory_id,
            accepted=True,
            status="repairing",
            action="canonical_close" if canonical_close else "cleanup_advance",
            reason="修复请求已受理",
            idempotent_replay=replay,
        )


def _classify(*, item, operation, age: float) -> MemoryGovernanceItem | None:
    repairing = (
        operation is not None
        and bool(operation.last_error_metadata.get("governance_repair_key"))
        and operation.status
        in {
            MemoryIndexOperationStatus.PENDING,
            MemoryIndexOperationStatus.RETRY,
            MemoryIndexOperationStatus.CLAIMED,
        }
    )
    if repairing:
        anomaly = "provider_residual" if operation.external_memory_id else "deletion_timeout"
        reason = "安全清理已重新提交"
        status = "repairing"
    elif operation is not None and operation.status == MemoryIndexOperationStatus.DEAD_LETTER:
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
        version=_version(item, operation),
        content=content,
        content_state="present" if content else "cleared",
        safe_reason=reason,
        deletion_pending_seconds=age,
        updated_at=item.updated_at,
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _version(item, operation) -> str:
    if operation is None:
        return f"{item.current_revision_id or 'none'}:missing:{item.updated_at.isoformat()}"
    return f"{item.current_revision_id or 'none'}:{operation.status.value}:{operation.updated_at.isoformat()}"


def _rejected(memory_id: str, reason: str) -> MemoryGovernanceRepairResponse:
    return MemoryGovernanceRepairResponse(
        memory_id=memory_id,
        accepted=False,
        status="rejected",
        reason=reason,
    )
