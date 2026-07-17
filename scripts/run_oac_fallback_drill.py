#!/usr/bin/env python3
"""Run deterministic Circuit, Fallback, and Write Fence migration drills."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from host_adapters.oac.fallback.circuit import CircuitBreaker, CircuitState
from host_adapters.oac.fallback.gateway import FallbackBlockedError, IRSFallbackGateway
from host_adapters.oac.fallback.policy import CommitStatus, classify_operation, write_fence_blocked
from host_apps.oac.config import OacHostSettings


async def run_drill() -> dict:
    now = datetime.now(UTC)
    circuit = CircuitBreaker(failure_threshold=2, recovery_seconds=10)
    states = [circuit.snapshot.state.value]
    circuit.record_failure(now=now)
    circuit.record_failure(now=now)
    states.append(circuit.snapshot.state.value)
    blocked_before_recovery = not circuit.allow_request(now=now + timedelta(seconds=9))
    probe_allowed = circuit.allow_request(now=now + timedelta(seconds=10))
    states.append(circuit.snapshot.state.value)
    second_probe_blocked = not circuit.allow_request(now=now + timedelta(seconds=10))
    circuit.record_success()
    states.append(circuit.snapshot.state.value)

    fallback_calls: list[str] = []

    async def fail_connection():
        raise ConnectionError("synthetic primary unavailable")

    async def fail_timeout():
        raise TimeoutError("synthetic ambiguous timeout")

    async def legacy_read():
        fallback_calls.append("read")
        return {"source": "irs", "operation": "read"}

    async def legacy_route():
        fallback_calls.append("route")
        return {"source": "irs", "operation": "route"}

    async def legacy_write():
        fallback_calls.append("write")
        return {"source": "irs", "operation": "write"}

    read_gateway = IRSFallbackGateway(
        mode="safe_route",
        policy_version="oac-host-policy-v1",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
    )
    read_result = await read_gateway.execute(
        operation=classify_operation("POST", "/api/v1/knowledge/search"),
        request_id="drill-read",
        primary=fail_connection,
        fallback=legacy_read,
    )
    route_gateway = IRSFallbackGateway(
        mode="safe_route",
        policy_version="oac-host-policy-v1",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
    )
    route_result = await route_gateway.execute(
        operation=classify_operation("POST", "/api/v1/central/route"),
        request_id="drill-route-safe",
        primary=fail_connection,
        fallback=legacy_route,
        commit_probe=lambda: _status(CommitStatus.NOT_ACCEPTED),
    )

    unknown_gateway = IRSFallbackGateway(
        mode="safe_route",
        policy_version="oac-host-policy-v1",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
    )
    unknown_blocked = False
    try:
        await unknown_gateway.execute(
            operation=classify_operation("POST", "/api/v1/central/route"),
            request_id="drill-route-unknown",
            primary=fail_timeout,
            fallback=legacy_route,
            commit_probe=lambda: _status(CommitStatus.UNKNOWN),
        )
    except FallbackBlockedError as exc:
        unknown_blocked = exc.reason == "ambiguous_commit"

    write_gateway = IRSFallbackGateway(
        mode="safe_route",
        policy_version="oac-host-policy-v1",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
    )
    write_failed_closed = False
    try:
        await write_gateway.execute(
            operation=classify_operation("POST", "/api/v1/admin/knowledge/files"),
            request_id="drill-write",
            primary=fail_timeout,
            fallback=legacy_write,
        )
    except TimeoutError:
        write_failed_closed = True

    frozen = OacHostSettings(write_freeze_enabled=True)
    runtime_write_blocked = write_fence_blocked(
        frozen, classify_operation("POST", "/api/v1/central/events/agent")
    )
    control_write_blocked = write_fence_blocked(
        frozen, classify_operation("POST", "/api/v1/admin/knowledge/files")
    )
    checks = {
        "circuit_opened": states[1] == CircuitState.OPEN,
        "circuit_blocked_before_recovery": blocked_before_recovery,
        "circuit_half_open_probe": probe_allowed
        and states[2] == CircuitState.HALF_OPEN
        and second_probe_blocked,
        "circuit_recovered": states[3] == CircuitState.CLOSED,
        "read_only_fallback": read_result == {"source": "irs", "operation": "read"},
        "safe_route_fallback": route_result == {"source": "irs", "operation": "route"},
        "unknown_commit_blocked": unknown_blocked,
        "write_failed_closed": write_failed_closed and "write" not in fallback_calls,
        "runtime_write_fenced": runtime_write_blocked,
        "control_write_fenced": control_write_blocked,
    }
    return {
        "contract": "oir-circuit-fallback-write-fence-drill/v1",
        "passed": all(checks.values()),
        "checks": checks,
        "circuit_states": states,
        "fallback_calls": fallback_calls,
        "unknown_audit_reasons": [record.reason for record in unknown_gateway.metrics.audit],
    }


async def _status(status: CommitStatus) -> CommitStatus:
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run_drill())
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
