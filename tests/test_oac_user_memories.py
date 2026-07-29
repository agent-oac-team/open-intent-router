import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory_index_operations import MemoryIndexOutboxRepository
from app.schemas.memory import MemoryItem, UserMemoryDeleteResponse
from app.services.memory_management import (
    MemoryManagementConflict,
    MemoryManagementNotFound,
    MemoryManagementService,
)
from host_adapters.oac.api.user_memories import router
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity.models import TrustedHostIdentity
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)


def _service(repository: MemoryItemRepository) -> MemoryManagementService:
    return MemoryManagementService(
        memory_service=SimpleNamespace(
            repository=repository,
            lifecycle=SimpleNamespace(),
            index_outbox=MemoryIndexOutboxRepository(),
        )
    )


def _item(
    memory_id: str,
    *,
    user_id: str = "7",
    scope: str = "user_preference",
    lifecycle_status: str = "active",
    index_status: str | None = "ready",
    minutes: int = 0,
) -> MemoryItem:
    updated_at = datetime(2026, 7, 29, 10, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return MemoryItem(
        memory_id=memory_id,
        scope=scope,
        subject_id=user_id,
        user_id=user_id,
        tenant_id="oac",
        content=f"正文 {memory_id}",
        current_revision_id=f"revision-{memory_id}",
        lifecycle_status=lifecycle_status,
        index_status=index_status,
        created_at=updated_at,
        updated_at=updated_at,
    )


def test_authenticated_user_memory_api_returns_only_current_principal_product_fields() -> None:
    repository = MemoryItemRepository()

    async def seed() -> None:
        for index in range(23):
            await repository.add(
                _item(
                    f"mine-{index:02}",
                    scope="stable_fact" if index % 2 else "user_preference",
                    index_status=("ready", "pending", "out_of_sync")[index % 3],
                    minutes=index,
                )
            )
        await repository.add(_item("other-user", user_id="8", minutes=100))
        await repository.add(_item("task", scope="task_memory", minutes=101))
        await repository.add(_item("summary", scope="session_summary", minutes=102))
        await repository.add(_item("artifact", scope="artifact_reference", minutes=103))
        await repository.add(_item("deleted", lifecycle_status="deletion_pending", minutes=104))

    asyncio.run(seed())
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        knowledge=SimpleNamespace(),
        knowledge_assets=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=SimpleNamespace(),
        memory_management=_service(repository),
    )
    identity = TrustedHostIdentity(
        key_id="user-key",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id="7",
        groups=(),
        credential_class="oac_user",
        claims_version="oac-principal-v1",
        roles=("admin",),
        active_bundle_id="oac-admin",
        policy_version="oac-authz-v1",
        signature_version="v2",
        request_operation="user-memory-read",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    app.dependency_overrides[get_trusted_host_identity] = lambda: identity
    client = TestClient(app)

    response = client.get("/api/v1/user-memories?page=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["page_size"] == 20
    assert payload["page"] == 1
    assert payload["total"] == 23
    assert payload["total_pages"] == 2
    assert len(payload["items"]) == 20
    assert payload["items"][0]["content"] == "正文 mine-22"
    assert {item["availability"] for item in payload["items"]} == {
        "available",
        "preparing",
        "unavailable",
    }
    assert set(payload["items"][0]) == {
        "content",
        "memory_type",
        "availability",
        "updated_at",
        "target_token",
        "concurrency_token",
    }
    assert payload["items"][0]["target_token"] != "mine-22"
    assert payload["items"][0]["concurrency_token"] != "revision-mine-22"
    serialized = response.text
    for forbidden in (
        "memory_id",
        "provider",
        "confidence",
        "revision_id",
        "index_status",
        "dead_letter",
        "other-user",
        "task",
        "summary",
        "artifact",
        "deleted",
    ):
        assert forbidden not in serialized

    filtered = client.get("/api/v1/user-memories?memory_type=stable_fact&page=2")
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 11
    assert filtered.json()["items"] == []
    assert client.get("/api/v1/user-memories?memory_type=task_memory").status_code == 422


def test_authenticated_user_delete_api_accepts_opaque_target_and_product_response() -> None:
    repository = MemoryItemRepository()
    asyncio.run(repository.add(_item("mine-delete")))
    service = _service(repository)

    class DeletePort:
        async def list_user_memories(self, **kwargs):
            return await service.list_user_memories(**kwargs)

        async def delete_user_memory(self, **kwargs):
            assert kwargs["tenant_id"] == "oac"
            assert kwargs["user_id"] == "7"
            return UserMemoryDeleteResponse(
                idempotent_replay=kwargs["idempotency_key"] == "remove-mine-delete-replay"
            )

    delete_port = DeletePort()
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        knowledge=SimpleNamespace(),
        knowledge_assets=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=SimpleNamespace(),
        memory_management=delete_port,
    )
    identity = TrustedHostIdentity(
        key_id="user-key",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id="7",
        groups=(),
        credential_class="oac_user",
        claims_version="oac-principal-v1",
        roles=(),
        active_bundle_id="oac-operations",
        policy_version="oac-authz-v1",
        signature_version="v2",
        request_operation="user-memory-delete",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    app.dependency_overrides[get_trusted_host_identity] = lambda: identity
    client = TestClient(app)
    listed = client.get("/api/v1/user-memories").json()["items"][0]
    request = {
        "idempotency_key": "remove-mine-delete-v1",
        "concurrency_token": listed["concurrency_token"],
    }

    deleted = client.request(
        "DELETE",
        f"/api/v1/user-memories/{listed['target_token']}",
        json=request,
    )
    replay_request = {**request, "idempotency_key": "remove-mine-delete-replay"}
    replay = client.request(
        "DELETE",
        f"/api/v1/user-memories/{listed['target_token']}",
        json=replay_request,
    )

    assert deleted.status_code == 200
    assert deleted.json() == {"accepted": True, "idempotent_replay": False}
    assert replay.status_code == 200
    assert replay.json() == {"accepted": True, "idempotent_replay": True}


def test_user_memory_delete_hides_cross_subject_and_controls_version_conflict() -> None:
    repository = MemoryItemRepository()

    async def seed() -> None:
        await repository.add(_item("mine"))
        await repository.add(_item("other", user_id="8"))

    asyncio.run(seed())
    service = _service(repository)
    mine = asyncio.run(
        service.list_user_memories(tenant_id="oac", user_id="7", memory_type=None, page=1)
    ).items[0]
    other = asyncio.run(
        service.list_user_memories(tenant_id="oac", user_id="8", memory_type=None, page=1)
    ).items[0]

    async def verify() -> None:
        try:
            await service.delete_user_memory(
                target_token=other.target_token,
                concurrency_token=other.concurrency_token or "",
                tenant_id="oac",
                user_id="7",
                idempotency_key="cross-subject",
            )
        except MemoryManagementNotFound:
            pass
        else:
            raise AssertionError("cross-subject target must be indistinguishable from missing")
        try:
            await service.delete_user_memory(
                target_token=mine.target_token,
                concurrency_token="changed-version",
                tenant_id="oac",
                user_id="7",
                idempotency_key="stale-version",
            )
        except MemoryManagementConflict:
            pass
        else:
            raise AssertionError("stale version must conflict")

    asyncio.run(verify())
