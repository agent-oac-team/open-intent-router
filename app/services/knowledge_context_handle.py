from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from threading import Condition, Lock, Thread

from app.schemas.agent_context import KnowledgeContext


class KnowledgeContextHandleError(ValueError):
    """A controlled Knowledge Context Handle is invalid or cannot be consumed."""


@dataclass
class _HandleRecord:
    context: KnowledgeContext
    tenant_id: str
    principal_id: str
    agent_id: str
    source_ids: tuple[str, ...]
    source_tags: tuple[str, ...]
    trace_id: str
    expires_at: datetime


class KnowledgeContextHandleService:
    """Issues process-local, opaque, one-time handles for transient Knowledge Context."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Knowledge Context Handle TTL must be positive")
        self.ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._records: dict[str, _HandleRecord] = {}
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._expirations: list[tuple[datetime, str]] = []
        self._closed = False
        self._scheduler = Thread(
            target=self._run_expiry_scheduler,
            name="knowledge-context-handle-expiry",
            daemon=True,
        )
        self._scheduler.start()

    def issue(
        self,
        context: KnowledgeContext,
        *,
        tenant_id: str,
        principal_id: str,
        agent_id: str,
        source_ids: list[str],
        source_tags: list[str],
        trace_id: str,
    ) -> str:
        if not tenant_id or not principal_id or not agent_id or not trace_id:
            raise ValueError(
                "Knowledge Context Handle requires tenant, principal, agent, and trace bindings"
            )
        handle = f"kh_{secrets.token_urlsafe(32)}"
        record = _HandleRecord(
            context=context.model_copy(deep=True),
            tenant_id=tenant_id,
            principal_id=principal_id,
            agent_id=agent_id,
            source_ids=_normalized_scope(source_ids),
            source_tags=_normalized_scope(source_tags),
            trace_id=trace_id,
            expires_at=self._clock() + timedelta(seconds=self.ttl_seconds),
        )
        with self._condition:
            if self._closed:
                raise RuntimeError("Knowledge Context Handle service is closed")
            self._records[handle] = record
            heappush(self._expirations, (record.expires_at, handle))
            self._condition.notify()
        return handle

    def consume(
        self,
        handle: str,
        *,
        tenant_id: str,
        principal_id: str,
        agent_id: str,
        source_ids: list[str],
        source_tags: list[str],
        trace_id: str,
    ) -> KnowledgeContext:
        # Pop first so success and every fail-closed validation path erase the
        # transient body atomically and make the handle one-time.
        with self._condition:
            record = self._records.pop(handle, None)
            self._condition.notify()
        if record is None:
            raise KnowledgeContextHandleError("Knowledge Context Handle is invalid")
        if self._clock() >= record.expires_at:
            raise KnowledgeContextHandleError("Knowledge Context Handle has expired")
        expected = (
            tenant_id,
            principal_id,
            agent_id,
            _normalized_scope(source_ids),
            _normalized_scope(source_tags),
            trace_id,
        )
        actual = (
            record.tenant_id,
            record.principal_id,
            record.agent_id,
            record.source_ids,
            record.source_tags,
            record.trace_id,
        )
        if expected != actual:
            raise KnowledgeContextHandleError("Knowledge Context Handle binding does not match")
        return record.context.model_copy(deep=True)

    @property
    def active_handle_count(self) -> int:
        with self._lock:
            return len(self._records)

    def close(self) -> None:
        """Stop active expiry work and erase every unconsumed transient body."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._records.clear()
            self._expirations.clear()
            self._condition.notify_all()
        if self._scheduler.is_alive():
            self._scheduler.join()

    def _run_expiry_scheduler(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                if not self._expirations:
                    self._condition.wait()
                    continue
                expires_at, handle = self._expirations[0]
                remaining = (expires_at - self._clock()).total_seconds()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                heappop(self._expirations)
                record = self._records.get(handle)
                if record is not None and record.expires_at == expires_at:
                    self._records.pop(handle, None)


def _normalized_scope(values: list[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))
