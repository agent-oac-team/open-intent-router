from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from host_adapters.oac.fallback.circuit import CircuitBreaker
from host_adapters.oac.fallback.policy import AdapterOperation, CommitStatus, OperationClass


class FallbackBlockedError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FallbackAuditRecord:
    operation: str
    operation_class: str
    request_id: str
    outcome: str
    reason: str
    mode: str
    policy_version: str
    latency_ms: float = 0
    correlation: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AdapterGovernanceMetrics:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.audit: list[FallbackAuditRecord] = []

    def record(self, record: FallbackAuditRecord) -> None:
        self.audit.append(record)
        self.counters[f"{record.operation}:{record.outcome}"] += 1
        self.counters[f"class:{record.operation_class}:{record.outcome}"] += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(sorted(self.counters.items())),
            "event_count": len(self.audit),
        }


class IRSFallbackGateway:
    def __init__(
        self,
        *,
        mode: str,
        policy_version: str,
        circuit: CircuitBreaker,
        metrics: AdapterGovernanceMetrics | None = None,
    ) -> None:
        self.mode = mode
        self.policy_version = policy_version
        self.circuit = circuit
        self.metrics = metrics or AdapterGovernanceMetrics()

    async def execute(
        self,
        *,
        operation: AdapterOperation,
        request_id: str,
        primary: Callable[[], Awaitable[Any]],
        fallback: Callable[[], Awaitable[Any]],
        commit_probe: Callable[[], Awaitable[CommitStatus]] | None = None,
        correlation: dict[str, str | None] | None = None,
    ) -> Any:
        safe_correlation = _safe_correlation(correlation)
        if operation.operation_class in {
            OperationClass.CONTROL_WRITE,
            OperationClass.RUNTIME_WRITE,
        }:
            return await self._primary(operation, request_id, primary, safe_correlation)
        if self.circuit.allow_request():
            try:
                result = await self._primary(operation, request_id, primary, safe_correlation)
                self.circuit.record_success()
                return result
            except Exception:
                self.circuit.record_failure()
        if operation.operation_class == OperationClass.READ_ONLY:
            if self.mode not in {"read_only", "safe_route"}:
                raise FallbackBlockedError("fallback_disabled")
            return await self._fallback(
                operation=operation,
                request_id=request_id,
                reason="read_only_primary_failure",
                fallback=fallback,
                correlation=safe_correlation,
            )
        if self.mode != "safe_route":
            raise FallbackBlockedError("route_fallback_disabled")
        status = await commit_probe() if commit_probe else CommitStatus.UNKNOWN
        if status != CommitStatus.NOT_ACCEPTED:
            reason = "ambiguous_commit" if status == CommitStatus.UNKNOWN else "already_committed"
            self._audit(
                operation,
                request_id,
                outcome="fallback_blocked",
                reason=reason,
                correlation=safe_correlation,
            )
            raise FallbackBlockedError(reason)
        return await self._fallback(
            operation=operation,
            request_id=request_id,
            reason="route_not_accepted",
            fallback=fallback,
            correlation=safe_correlation,
        )

    async def _primary(
        self,
        operation: AdapterOperation,
        request_id: str,
        primary: Callable[[], Awaitable[Any]],
        correlation: dict[str, str],
    ) -> Any:
        started = datetime.now(UTC)
        try:
            result = await primary()
        except Exception:
            self._audit(
                operation,
                request_id,
                outcome="oir_failure",
                reason="primary_failure",
                latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
                correlation=correlation,
            )
            raise
        self._audit(
            operation,
            request_id,
            outcome="oir_success",
            reason="primary_success",
            latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
            correlation=correlation,
        )
        return result

    async def _fallback(
        self,
        *,
        operation: AdapterOperation,
        request_id: str,
        reason: str,
        fallback: Callable[[], Awaitable[Any]],
        correlation: dict[str, str],
    ) -> Any:
        started = datetime.now(UTC)
        result = await fallback()
        self._audit(
            operation,
            request_id,
            outcome="irs_fallback",
            reason=reason,
            latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
            correlation=correlation,
        )
        return result

    def _audit(
        self,
        operation: AdapterOperation,
        request_id: str,
        *,
        outcome: str,
        reason: str,
        latency_ms: float = 0,
        correlation: dict[str, str] | None = None,
    ) -> None:
        self.metrics.record(
            FallbackAuditRecord(
                operation=operation.name,
                operation_class=operation.operation_class.value,
                request_id=request_id,
                outcome=outcome,
                reason=reason,
                mode=self.mode,
                policy_version=self.policy_version,
                latency_ms=latency_ms,
                correlation=correlation or {},
            )
        )


class IRSLegacyClient:
    def __init__(self, *, base_url: str | None, service_token: str | None = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.service_token = service_token

    async def request_json(
        self,
        *,
        method: str,
        path: str,
        json_body: dict | None = None,
        query: dict | None = None,
    ) -> dict:
        if not self.base_url:
            raise FallbackBlockedError("irs_fallback_not_configured")
        headers = {}
        if self.service_token:
            headers["X-Admin-Sync-Token"] = self.service_token
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                params=query,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("IRS fallback response must be a JSON object")
        return payload


_CORRELATION_KEYS = {
    "request_id",
    "session_id",
    "turn_id",
    "run_id",
    "result_id",
    "event_id",
    "plan_id",
    "trace_id",
    "memory_id",
}


def _safe_correlation(value: dict[str, str | None] | None) -> dict[str, str]:
    return {
        key: str(item)[:160]
        for key, item in (value or {}).items()
        if key in _CORRELATION_KEYS and item
    }
