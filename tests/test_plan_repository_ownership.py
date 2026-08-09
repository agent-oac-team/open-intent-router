import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import memory_identity_signature
from app.db.session import create_all_tables, create_session_factory
from app.dependencies import get_plan_executor, get_plan_service
from app.main import create_app
from app.repositories.database import DatabasePlanRepository
from app.repositories.memory import MemoryPlanRepository
from app.schemas.events import AgentEvent
from app.schemas.plans import Plan, PlanExecutionResponse
from app.services.plan_service import PlanService


def _plan(**updates) -> Plan:
    values = {
        "plan_id": "plan_1",
        "user_id": "u1",
        "tenant_id": "t1",
        "session_id": "session_1",
        "status": "running",
        "steps": [{"step_id": "step_1", "agent_id": "agent_1", "description": "run"}],
    }
    return Plan.model_validate({**values, **updates})


async def test_plan_http_contract_rejects_missing_or_cross_user_identity() -> None:
    repository = MemoryPlanRepository()
    service = PlanService(repository)
    await service.save_plan(_plan())
    app = create_app()
    settings = Settings(
        app_env="production",
        memory_identity_secret="plan-test-secret",
    )
    executor = _OwnedPlanExecutor(service)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_plan_service] = lambda: service
    app.dependency_overrides[get_plan_executor] = lambda: executor

    def headers(user_id: str) -> dict[str, str]:
        return {
            "X-User-ID": user_id,
            "X-Tenant-ID": "t1",
            "X-Memory-Identity-Signature": memory_identity_signature(
                user_id=user_id,
                tenant_id="t1",
                secret="plan-test-secret",
            ),
        }

    owned_body = {
        "user": {
            "id": "u1",
            "roles": ["operator"],
            "attributes": {"tenant_id": "t1"},
        },
        "input": {},
        "context": {},
    }

    with TestClient(app) as client:
        assert client.get("/api/v1/plans/plan_1").status_code == 401
        assert (
            client.get(
                "/api/v1/plans/plan_1",
                headers={"X-User-ID": "u1", "X-Tenant-ID": "t1"},
            ).status_code
            == 401
        )
        owned = client.get("/api/v1/plans/plan_1", headers=headers("u1"))
        denied = client.get("/api/v1/plans/plan_1", headers=headers("u2"))
        denied_action = client.post(
            "/api/v1/plans/plan_1/actions",
            headers=headers("u2"),
            json={
                "action": "cancel",
                "user": {"id": "u2", "attributes": {"tenant_id": "t1"}},
            },
        )
        executed = client.post(
            "/api/v1/plans/plan_1/execute",
            headers=headers("u1"),
            json=owned_body,
        )
        denied_execute = client.post(
            "/api/v1/plans/plan_1/execute",
            headers=headers("u2"),
            json={**owned_body, "user": {"id": "u2", "attributes": {"tenant_id": "t1"}}},
        )
        resumed = client.post(
            "/api/v1/plans/plan_1/resume",
            headers=headers("u1"),
            json=owned_body,
        )
        confirm_execute = client.post(
            "/api/v1/plans/plan_1/confirm-and-execute",
            headers=headers("u1"),
            json=owned_body,
        )
        forged_owner = client.post(
            "/api/v1/plans/plan_1/execute",
            headers=headers("u1"),
            json={**owned_body, "user": {"id": "u2", "attributes": {"tenant_id": "t1"}}},
        )

    assert owned.status_code == 200
    assert (owned.json()["tenant_id"], owned.json()["user_id"]) == ("t1", "u1")
    assert denied.status_code == 404
    assert denied_action.status_code == 404
    assert denied_execute.status_code == 404
    assert executed.status_code == resumed.status_code == confirm_execute.status_code == 200
    assert forged_owner.status_code == 401
    assert executor.users and all(
        user.id == "u1" and user.tenant_id == "t1" for user in executor.users
    )


class _OwnedPlanExecutor:
    def __init__(self, service: PlanService) -> None:
        self.service = service
        self.users = []

    async def preflight(self, plan_id: str, *, user):
        plan = await self.service.get_plan(
            plan_id,
            tenant_id=user.tenant_id or "",
            user_id=user.id,
        )
        if plan is None:
            raise ValueError("Plan not found")
        return {}

    async def execute(self, plan_id: str, *, user, **_) -> PlanExecutionResponse:
        plan = await self.service.get_plan(
            plan_id,
            tenant_id=user.tenant_id or "",
            user_id=user.id,
        )
        if plan is None:
            raise ValueError("Plan not found")
        self.users.append(user)
        return PlanExecutionResponse(plan=plan)


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_plan_repository_requires_owner_for_reads_and_preserves_owner(
    backend, tmp_path
) -> None:
    if backend == "memory":
        repository = MemoryPlanRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'plans-owned.db'}",
        )
        await create_all_tables(settings)
        repository = DatabasePlanRepository(create_session_factory(settings))
    original = _plan()
    await repository.save(original)
    original.user_id = "mutated-original"

    assert await repository.get("plan_1", tenant_id="t1", user_id="u1")
    assert await repository.get("plan_1", tenant_id="t1", user_id="u2") is None
    assert await repository.get("plan_1", tenant_id="t2", user_id="u1") is None
    assert await repository.get_active_by_session("session_1", tenant_id="t1", user_id="u1")
    assert await repository.get_active_by_session("session_1", tenant_id="t1", user_id="u2") is None
    loaded = await repository.get("plan_1", tenant_id="t1", user_id="u1")
    assert loaded
    loaded.user_id = "mutated-read"
    assert await repository.get("plan_1", tenant_id="t1", user_id="u1")
    with pytest.raises(ValueError, match="ownership cannot be changed"):
        await repository.save(_plan(user_id="u2"))


async def test_database_terminal_plan_round_trip_preserves_empty_current_step_and_event_cursor(
    tmp_path,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'terminal-plan.db'}",
    )
    await create_all_tables(settings)
    repository = DatabasePlanRepository(create_session_factory(settings))
    service = PlanService(repository)
    stored = await service.save_plan(
        _plan(
            status="completed",
            current_step_id=None,
            last_event_id="forged",
            state_version=999,
            steps=[
                {
                    "step_id": "step_1",
                    "agent_id": "agent_1",
                    "description": "run",
                    "status": "completed",
                }
            ],
        )
    )
    loaded = await service.get_plan("plan_1", tenant_id="t1", user_id="u1")

    assert stored.last_event_id is None and stored.state_version == 1
    assert loaded is not None
    assert loaded.current_step_id is None
    assert loaded.last_event_id is None
    assert loaded.state_version == 1


async def test_database_plan_step_claim_is_atomic(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'plan-claim.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    service = PlanService(DatabasePlanRepository(session_factory))
    await service.save_plan(_plan())

    claims = await asyncio.gather(
        service.claim_step("plan_1", "step_1", tenant_id="t1", user_id="u1"),
        service.claim_step("plan_1", "step_1", tenant_id="t1", user_id="u1"),
    )

    assert sum(claim is not None for claim in claims) == 1
    loaded = await service.get_plan("plan_1", tenant_id="t1", user_id="u1")
    assert loaded and loaded.steps[0].status == "running"
    await session_factory.kw["bind"].dispose()


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_plan_claim_completion_respects_concurrent_block_and_can_be_reclaimed(
    backend, tmp_path
) -> None:
    if backend == "memory":
        repository = MemoryPlanRepository()
        session_factory = None
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'claim-version.db'}",
        )
        await create_all_tables(settings)
        session_factory = create_session_factory(settings)
        repository = DatabasePlanRepository(session_factory)
    service = PlanService(repository)
    await service.save_plan(_plan())
    claimed = await service.claim_step("plan_1", "step_1", tenant_id="t1", user_id="u1")
    assert claimed is not None
    claimed_plan, claim_id = claimed

    blocked = await service.apply_agent_event(
        AgentEvent(
            event_id="clarify_after_claim",
            session_id="session_1",
            agent_id="agent_1",
            plan_id="plan_1",
            step_id="step_1",
            event_type="agent_clarify",
        ),
        tenant_id="t1",
        user_id="u1",
    )
    assert blocked is not None and blocked.status == "blocked"
    finished = await service.finish_claimed_step(
        "plan_1",
        "step_1",
        tenant_id="t1",
        user_id="u1",
        claim_id=claim_id,
        status="completed",
    )
    assert finished is not None and finished.status == "blocked"
    assert finished.state_version > claimed_plan.state_version

    resumed = await service.claim_step("plan_1", "step_1", tenant_id="t1", user_id="u1")
    assert resumed is not None
    assert resumed[0].status == "running" and resumed[0].steps[0].status == "running"
    if session_factory is not None:
        await session_factory.kw["bind"].dispose()


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_plan_claim_completion_consumes_result_after_concurrent_progress(
    backend, tmp_path
) -> None:
    if backend == "memory":
        repository = MemoryPlanRepository()
        session_factory = None
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'claim-progress.db'}",
        )
        await create_all_tables(settings)
        session_factory = create_session_factory(settings)
        repository = DatabasePlanRepository(session_factory)
    service = PlanService(repository)
    await service.save_plan(_plan())
    claimed = await service.claim_step("plan_1", "step_1", tenant_id="t1", user_id="u1")
    assert claimed is not None
    _, claim_id = claimed
    progressed = await service.apply_agent_event(
        AgentEvent(
            event_id="progress_after_claim",
            session_id="session_1",
            agent_id="agent_1",
            plan_id="plan_1",
            step_id="step_1",
            event_type="agent_progress",
        ),
        tenant_id="t1",
        user_id="u1",
    )
    assert progressed is not None and progressed.status == "running"

    finished = await service.finish_claimed_step(
        "plan_1",
        "step_1",
        tenant_id="t1",
        user_id="u1",
        claim_id=claim_id,
        status="completed",
    )
    assert finished is not None and finished.status == "completed"
    assert finished.current_step_id is None
    if session_factory is not None:
        await session_factory.kw["bind"].dispose()


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_expired_claim_reuses_execution_idempotency_key(backend, tmp_path) -> None:
    if backend == "memory":
        repository = MemoryPlanRepository()
        session_factory = None
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'claim-idempotency.db'}",
        )
        await create_all_tables(settings)
        session_factory = create_session_factory(settings)
        repository = DatabasePlanRepository(session_factory)
    service = PlanService(repository)
    await service.save_plan(_plan())
    first = await service.claim_step(
        "plan_1",
        "step_1",
        tenant_id="t1",
        user_id="u1",
        lease_seconds=0.01,
    )
    assert first is not None
    first_key = await service.get_execution_claim_key("plan_1", claim_id=first[1])
    await asyncio.sleep(0.02)
    second = await service.claim_step(
        "plan_1",
        "step_1",
        tenant_id="t1",
        user_id="u1",
        lease_seconds=1,
    )
    assert second is not None and second[1] != first[1]
    second_key = await service.get_execution_claim_key("plan_1", claim_id=second[1])
    assert first_key and second_key == first_key
    renewed = await repository.renew_step_claim(
        "plan_1",
        tenant_id="t1",
        user_id="u1",
        claim_id=second[1],
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=2),
        now=datetime.now(UTC),
    )
    assert renewed
    if session_factory is not None:
        await session_factory.kw["bind"].dispose()
