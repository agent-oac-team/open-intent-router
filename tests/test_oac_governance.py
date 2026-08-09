import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.schemas.routing import RouteRequest
from app.services.router_service import RouterService
from host_adapters.oac.fallback.policy import (
    ADAPTER_OPERATIONS,
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
)
from host_apps.oac.config import (
    OacHostSettings,
    build_oac_host_profile,
    memory_execution_plane_for_shadow,
)


def test_all_adapter_methods_have_one_static_operation_class() -> None:
    assert len(ADAPTER_OPERATIONS) == 9
    assert {item.operation_class for item in ADAPTER_OPERATIONS} == set(OperationClass)
    assert classify_operation("POST", "/api/v1/central/route").operation_class == "route_stateful"
    assert (
        classify_operation("POST", "/api/v1/admin/agent-registry").operation_class
        == "control_write"
    )
    assert classify_operation("GET", "/api/v1/admin/agent-registry").operation_class == "read_only"
    with pytest.raises(KeyError):
        classify_operation("POST", "/api/v1/knowledge/search")
    with pytest.raises(KeyError):
        classify_operation("POST", "/api/v1/unknown")


def test_write_fence_and_rehearsal_configuration_fail_closed() -> None:
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
    assert not write_fence_blocked(
        frozen, classify_operation("GET", "/api/v1/admin/agent-registry")
    )


@pytest.mark.parametrize(
    ("shadow", "execution_plane"),
    [
        ("off", "live"),
        ("decision", "decision_shadow"),
        ("state_rehearsal", "state_rehearsal"),
    ],
)
def test_oac_shadow_mode_maps_to_core_execution_plane(shadow, execution_plane) -> None:
    assert memory_execution_plane_for_shadow(shadow) == execution_plane


async def test_shadow_replay_persists_route_coverage(tmp_path) -> None:
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
    assert report["blocking_diff_count"] == 0
    assert report["passed"] is True
    assert len(snapshot["results"]) == 1
    assert snapshot["diffs"] == []


def test_shadow_guard_blocks_writes_and_approved_fingerprint_is_exact() -> None:
    guard = DecisionShadowGuard()
    with pytest.raises(ShadowSideEffectBlocked):
        guard.assert_allowed(
            classify_operation("POST", "/api/v1/admin/agent-registry"),
            side_effect_free=True,
        )
    with pytest.raises(ShadowSideEffectBlocked):
        guard.assert_allowed(
            classify_operation("POST", "/api/v1/central/route"),
            side_effect_free=False,
        )


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
