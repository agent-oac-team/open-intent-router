import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.repositories.turns import MemoryTurnRepository
from app.schemas.routing import RouteRequest
from app.schemas.turns import TurnUserInput
from app.services.router_service import RouterService
from app.services.turn_service import TurnService
from host_adapters.oac.fallback.circuit import CircuitBreaker, CircuitState
from host_adapters.oac.fallback.gateway import (
    AdapterGovernanceMetrics,
    FallbackBlockedError,
    IRSFallbackGateway,
)
from host_adapters.oac.fallback.policy import (
    ADAPTER_OPERATIONS,
    CommitStatus,
    OperationClass,
    classify_operation,
    write_fence_blocked,
)
from host_adapters.oac.repositories.shadow import (
    DatabaseShadowRepository,
    create_shadow_tables,
)
from host_adapters.oac.shadow.runner import (
    DecisionShadowGuard,
    ShadowReplayRunner,
    ShadowSideEffectBlocked,
    knowledge_diff,
)
from host_apps.oac.config import OacHostSettings, build_oac_host_profile


def test_all_22_adapter_methods_have_one_static_operation_class() -> None:
    assert len(ADAPTER_OPERATIONS) == 22
    assert {item.operation_class for item in ADAPTER_OPERATIONS} == set(OperationClass)
    assert classify_operation("POST", "/api/v1/central/route").operation_class == "route_stateful"
    assert (
        classify_operation("POST", "/api/v1/admin/knowledge/files").operation_class
        == "control_write"
    )
    assert (
        classify_operation("GET", "/api/v1/knowledge/assets/a-1/chunks").operation_class
        == "read_only"
    )
    with pytest.raises(KeyError):
        classify_operation("POST", "/api/v1/unknown")


def test_write_fence_and_rehearsal_configuration_fail_closed() -> None:
    with pytest.raises(ValueError, match="dual writable primaries"):
        build_oac_host_profile(
            core=Settings(),
            host=OacHostSettings(irs_control_write_enabled=True),
        )
    with pytest.raises(ValueError, match="fallback URL"):
        build_oac_host_profile(
            core=Settings(),
            host=OacHostSettings(fallback_mode="read_only"),
        )
    with pytest.raises(ValueError, match="isolated database"):
        build_oac_host_profile(
            core=Settings(),
            host=OacHostSettings(shadow_mode="state_rehearsal"),
        )
    with pytest.raises(ValueError, match="must differ"):
        build_oac_host_profile(
            core=Settings(database_url="sqlite+aiosqlite:///main.db"),
            host=OacHostSettings(
                shadow_mode="state_rehearsal",
                state_rehearsal_database_url="sqlite+aiosqlite:///main.db",
            ),
        )

    frozen = OacHostSettings(write_freeze_enabled=True)
    assert write_fence_blocked(frozen, classify_operation("POST", "/api/v1/central/events/agent"))
    assert not write_fence_blocked(frozen, classify_operation("POST", "/api/v1/knowledge/search"))


def test_circuit_breaker_opens_half_opens_and_recovers() -> None:
    now = datetime.now(UTC)
    circuit = CircuitBreaker(failure_threshold=2, recovery_seconds=10)

    assert circuit.allow_request(now=now)
    circuit.record_failure(now=now)
    assert circuit.snapshot.state == CircuitState.CLOSED
    assert circuit.allow_request(now=now)
    circuit.record_failure(now=now)
    assert circuit.snapshot.state == CircuitState.OPEN
    assert not circuit.allow_request(now=now + timedelta(seconds=9))
    assert circuit.allow_request(now=now + timedelta(seconds=10))
    assert circuit.snapshot.state == CircuitState.HALF_OPEN
    assert not circuit.allow_request(now=now + timedelta(seconds=10))
    circuit.record_success()
    assert circuit.snapshot.state == CircuitState.CLOSED


async def test_fallback_allows_reads_and_proven_not_accepted_routes_only() -> None:
    metrics = AdapterGovernanceMetrics()
    gateway = IRSFallbackGateway(
        mode="safe_route",
        policy_version="policy-v1",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
        metrics=metrics,
    )

    async def fail():
        raise ConnectionError("primary unavailable")

    async def legacy():
        return {"source": "irs"}

    read_result = await gateway.execute(
        operation=classify_operation("POST", "/api/v1/knowledge/search"),
        request_id="read-1",
        primary=fail,
        fallback=legacy,
    )
    assert read_result == {"source": "irs"}

    route_gateway = IRSFallbackGateway(
        mode="safe_route",
        policy_version="policy-v1",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
        metrics=metrics,
    )
    route_result = await route_gateway.execute(
        operation=classify_operation("POST", "/api/v1/central/route"),
        request_id="route-1",
        primary=fail,
        fallback=legacy,
        commit_probe=lambda: _commit_status(CommitStatus.NOT_ACCEPTED),
    )
    assert route_result == {"source": "irs"}
    assert all("ticket" not in record.__dict__ for record in metrics.audit)
    assert metrics.counters["knowledge.search:irs_fallback"] == 1


async def test_governance_metrics_keep_only_safe_correlation_fields() -> None:
    gateway = IRSFallbackGateway(
        mode="read_only",
        policy_version="policy-v1",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
    )

    async def success():
        return {"ok": True}

    await gateway.execute(
        operation=classify_operation("POST", "/api/v1/knowledge/search"),
        request_id="request-1",
        primary=success,
        fallback=success,
        correlation={
            "turn_id": "turn-1",
            "run_id": "run-1",
            "execution_ticket": "must-not-appear",
            "token": "must-not-appear",
        },
    )

    record = gateway.metrics.audit[0]
    assert record.outcome == "oir_success"
    assert record.correlation == {"turn_id": "turn-1", "run_id": "run-1"}
    assert gateway.metrics.snapshot()["event_count"] == 1


async def test_unknown_route_commit_and_all_writes_never_fallback() -> None:
    fallback_calls = 0

    async def fail():
        raise TimeoutError("ambiguous timeout")

    async def legacy():
        nonlocal fallback_calls
        fallback_calls += 1
        return {}

    gateway = IRSFallbackGateway(
        mode="safe_route",
        policy_version="policy-v1",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
    )
    with pytest.raises(FallbackBlockedError, match="ambiguous_commit"):
        await gateway.execute(
            operation=classify_operation("POST", "/api/v1/central/route"),
            request_id="route-timeout",
            primary=fail,
            fallback=legacy,
            commit_probe=lambda: _commit_status(CommitStatus.UNKNOWN),
        )
    with pytest.raises(TimeoutError):
        await gateway.execute(
            operation=classify_operation("POST", "/api/v1/admin/knowledge/files"),
            request_id="write-1",
            primary=fail,
            fallback=legacy,
        )
    assert fallback_calls == 0
    assert any(record.reason == "ambiguous_commit" for record in gateway.metrics.audit)


async def test_turn_submission_status_proves_owner_commit() -> None:
    service = TurnService(MemoryTurnRepository())
    assert (
        await service.submission_status(request_id="req-1", tenant_id="oac", user_id="u-1")
        == "not_accepted"
    )
    await service.start_turn(
        tenant_id="oac",
        user_id="u-1",
        session_id="s-1",
        request_id="req-1",
        source="test",
        user_input=TurnUserInput(text="hello"),
    )
    assert (
        await service.submission_status(request_id="req-1", tenant_id="oac", user_id="u-1")
        == "committed"
    )
    assert (
        await service.submission_status(request_id="req-1", tenant_id="oac", user_id="u-2")
        == "unknown"
    )


async def test_shadow_replay_persists_coverage_and_blocking_permission_diff(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shadow.db'}")
    await create_shadow_tables(engine)
    repository = DatabaseShadowRepository(async_sessionmaker(engine, expire_on_commit=False))
    runner = ShadowReplayRunner(repository=repository)
    dataset = {
        "dataset_id": "dataset-v1",
        "version": "v1",
        "samples": [
            {
                "sample_id": "route-1",
                "kind": "route",
                "method": "POST",
                "path": "/api/v1/central/route",
                "side_effect_free": True,
                "irs_result": _route_result(),
                "oir_result": _route_result(),
            },
            {
                "sample_id": "knowledge-permission",
                "kind": "knowledge",
                "method": "POST",
                "path": "/api/v1/knowledge/search",
                "side_effect_free": True,
                "irs_result": {
                    "matched": False,
                    "evidence": [],
                    "warnings": [{"code": "permission_filtered"}],
                },
                "oir_result": {
                    "matched": True,
                    "evidence": [{"chunk_id": "secret-chunk"}],
                    "warnings": [],
                },
            },
        ],
    }
    report = await runner.run(
        dataset,
        operation_resolver=lambda sample: classify_operation(sample["method"], sample["path"]),
        oir_executor=lambda sample: _result(sample["oir_result"]),
    )
    snapshot = await repository.snapshot("dataset-v1")
    await engine.dispose()

    assert report["coverage"] == 1.0
    assert report["blocking_diff_count"] == 1
    assert report["passed"] is False
    assert len(snapshot["results"]) == 2
    assert sum(item["blocking"] and not item["approved"] for item in snapshot["diffs"]) == 1


async def test_versioned_shadow_dataset_covers_all_replay_categories(tmp_path) -> None:
    dataset = json.loads(
        Path("tests/contract/oac_irs/shadow/v1/dataset.json").read_text(encoding="utf-8")
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'coverage.db'}")
    await create_shadow_tables(engine)
    repository = DatabaseShadowRepository(async_sessionmaker(engine, expire_on_commit=False))
    report = await ShadowReplayRunner(repository=repository).run(
        dataset,
        operation_resolver=lambda sample: classify_operation(sample["method"], sample["path"]),
        oir_executor=lambda sample: _result(sample["oir_result"]),
    )
    await engine.dispose()

    assert set(report["category_coverage"]) == {"route", "knowledge", "permission", "e2e"}
    assert all(item["coverage"] == 1 for item in report["category_coverage"].values())
    assert report["passed"] is True


def test_shadow_guard_blocks_writes_and_approved_fingerprint_is_exact() -> None:
    guard = DecisionShadowGuard()
    with pytest.raises(ShadowSideEffectBlocked):
        guard.assert_allowed(
            classify_operation("DELETE", "/api/v1/admin/knowledge/files/asset-1"),
            side_effect_free=True,
        )
    with pytest.raises(ShadowSideEffectBlocked):
        guard.assert_allowed(
            classify_operation("POST", "/api/v1/central/route"),
            side_effect_free=False,
        )

    irs = {"matched": False, "evidence": [], "warnings": [{"code": "no_match"}]}
    oir = {"matched": True, "evidence": [{"chunk_id": "chunk-1"}], "warnings": []}
    initial = knowledge_diff(irs, oir)
    approved = knowledge_diff(irs, oir, approved={initial[0].fingerprint})
    assert approved[0].approved is True
    assert any(not item.approved for item in approved[1:])


async def test_decision_shadow_route_has_no_runtime_persistence_side_effects(
    settings, registry_service
) -> None:
    blocked = _BlockedSideEffect()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        turn_service=blocked,
        plan_service=blocked,
        chat_history_service=blocked,
        result_repository=blocked,
        event_service=blocked,
        route_log_repository=blocked,
        agent_context_service=blocked,
        plan_continuation_resolver=blocked,
    )
    request = RouteRequest.model_validate(
        {
            "request_id": "shadow-request-1",
            "session_id": "shadow-session-1",
            "user": {"id": "user-1", "attributes": {"tenant_id": "oac"}},
            "input": {"text": "summarize this input"},
        }
    )

    response = await service.route_decision_shadow(request)

    assert response.request_id == "shadow-request-1"
    assert blocked.calls == []


async def _commit_status(status: CommitStatus) -> CommitStatus:
    return status


async def _result(value: dict) -> dict:
    return value


def _route_result() -> dict:
    return {
        "route": {"action": "reply", "agent_id": None, "message": "ok"},
        "context": {"relation": "new_task"},
        "plan": None,
    }


class _BlockedSideEffect:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        async def blocked(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"Decision Shadow called side-effect dependency: {name}")

        return blocked
