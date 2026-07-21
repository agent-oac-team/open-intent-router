import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.repositories.knowledge_assets import MemoryCanonicalKnowledgeRepository
from app.schemas.knowledge_assets import KnowledgeAssetGroup
from app.services.knowledge_asset_service import KnowledgeAssetService
from host_adapters.oac.api.knowledge import router
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.fallback.circuit import CircuitBreaker
from host_adapters.oac.fallback.gateway import IRSFallbackGateway
from host_adapters.oac.identity.canonical import canonicalize_host_request
from host_adapters.oac.identity.models import SignedHostRequest, TrustedHostIdentity
from host_apps.oac.dependencies import (
    get_host_identity_verifier,
    get_host_nonce_store,
    get_irs_fallback_gateway,
    get_irs_legacy_client,
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)
from host_apps.oac.main import create_app as create_oac_host_app


async def _service() -> KnowledgeAssetService:
    repository = MemoryCanonicalKnowledgeRepository()
    service = KnowledgeAssetService(repository)
    await service.save_group(
        KnowledgeAssetGroup(
            group_id="content_production",
            tenant_id="oac",
            name="content_production",
            stable_asset_keys=["01", "03", "04"],
        )
    )
    await service.ingest_bytes(
        tenant_id="oac",
        owner_id="admin-1",
        file_name="04 活动表.txt",
        content_type="text/plain",
        data="活动名称：契约测试活动；活动期限：2026 年 12 月 31 日。".encode(),
        name="活动表",
        stable_key="04",
        group_id="content_production",
        metadata={"business_domain": "内容生产", "source_type": "excel"},
    )
    return service


def _client(*, credential_class="coze_workflow") -> TestClient:
    service = asyncio.run(_service())
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        knowledge=SimpleNamespace(),
        knowledge_assets=service,
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=SimpleNamespace(),
    )
    identity = TrustedHostIdentity(
        key_id="key",
        audience="test",
        principal_type="service" if credential_class == "coze_workflow" else "user",
        tenant_id="oac",
        user_id="coze-1" if credential_class == "coze_workflow" else "admin-1",
        groups=("运营版",),
        credential_class=credential_class,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    app.dependency_overrides[get_trusted_host_identity] = lambda: identity
    return TestClient(app)


def _search_body() -> dict:
    return {
        "request_id": "request-search",
        "session_id": "session-1",
        "user_id": "forged-user",
        "user_tags": ["forged-admin"],
        "consumer": "coze_workflow",
        "consumer_id": "workflow-1",
        "purpose": "workflow",
        "query": "活动期限",
        "scope": {"asset_ids": []},
        "return_options": {"include_structured_payload": True},
        "top_k": 3,
    }


def test_coze_transport_search_grouped_read_and_access_endpoints() -> None:
    client = _client()
    search = client.post("/api/v1/knowledge/search", json=_search_body())
    assert search.status_code == 200
    assert search.json()["matched"] is True
    assert search.json()["evidence"][0]["structured_payload"]

    grouped_body = {
        **_search_body(),
        "request_id": "request-grouped",
        "asset_group": "content_production",
        "asset_keys": ["04", "03"],
        "top_k_per_asset": 3,
        "include_empty_assets": True,
    }
    for key in ("scope", "return_options", "top_k"):
        grouped_body.pop(key, None)
    grouped = client.post("/api/v1/knowledge/grouped-search", json=grouped_body)
    assert grouped.status_code == 200
    assert list(grouped.json()["assets"]) == ["04", "03"]
    assert grouped.json()["assets"]["03"]["matched"] is False

    asset_id = search.json()["evidence"][0]["asset_id"]
    read_body = {
        "request_id": "request-read",
        "session_id": "session-1",
        "user_id": "forged-user",
        "consumer": "coze_workflow",
        "purpose": "workflow",
        "target": {"type": "asset", "asset_id": asset_id},
    }
    read = client.post("/api/v1/knowledge/read", json=read_body)
    assert read.status_code == 200
    assert read.json()["chunks"][0]["asset_id"] == asset_id
    assert client.get("/api/v1/knowledge/assets").status_code == 200
    assert client.get(f"/api/v1/knowledge/assets/{asset_id}").status_code == 200
    assert client.get(f"/api/v1/knowledge/assets/{asset_id}/chunks").status_code == 200


def test_coze_cannot_use_knowledge_admin_and_admin_can_upload_text() -> None:
    coze = _client()
    forbidden = coze.get("/api/v1/admin/knowledge/files")
    assert forbidden.status_code == 403

    admin = _client(credential_class="oac_admin")
    uploaded = admin.post(
        "/api/v1/admin/knowledge/files",
        data={
            "source_name": "说明",
            "business_domain": "内容生产",
            "sensitivity": "internal",
            "allowed_user_tags": "运营版",
        },
        files={"file": ("notes.txt", "管理知识", "text/plain")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["asset"]["status"] == "indexed"
    asset_id = uploaded.json()["asset"]["asset_id"]
    assert admin.get("/api/v1/admin/knowledge/files").json()["total"] == 2
    assert admin.get(f"/api/v1/admin/knowledge/files/{asset_id}").status_code == 200
    assert admin.get(f"/api/v1/admin/knowledge/files/{asset_id}/chunks").status_code == 200
    deleted = admin.request(
        "DELETE",
        f"/api/v1/admin/knowledge/files/{asset_id}",
        json={"reason": "test"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_multipart_upload_crosses_real_host_verifier_with_wire_body_binding() -> None:
    boundary = "oir-host-signature-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="source_name"\r\n\r\n'
        "wire-body-fixture\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="business_domain"\r\n\r\n'
        "transport-test\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="wire.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "signed multipart body\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    timestamp = str(int(time.time()))
    signed = SignedHostRequest(
        method="POST",
        path="/api/v1/admin/knowledge/files",
        query="",
        body=body,
        key_id="test-admin-key",
        audience="oac-oir-adapter-local",
        timestamp=timestamp,
        nonce="bm9uY2UtbXVsdGlwYXJ0LTAwMQ",
        content_sha256=hashlib.sha256(body).hexdigest(),
        principal_type="user",
        user_id="admin-wire-1",
        groups="",
        credential_class="oac_admin",
        signature="v2=" + "0" * 64,
        claims_version="oac-admin-principal-v1",
        roles="",
        active_bundle_id="",
        policy_version="oac-control-v1",
    )
    digest = hmac.new(
        b"test-admin-secret",
        canonicalize_host_request(signed, tenant_id="oac").encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-OIR-Host-Key-Id": signed.key_id,
        "X-OIR-Host-Audience": signed.audience,
        "X-OIR-Host-Timestamp": signed.timestamp,
        "X-OIR-Host-Nonce": signed.nonce,
        "X-OIR-Host-Content-SHA256": signed.content_sha256,
        "X-OIR-Host-Principal-Type": signed.principal_type,
        "X-OIR-Host-User-Id": signed.user_id,
        "X-OIR-Host-Groups": "",
        "X-OIR-Host-Credential-Class": signed.credential_class,
        "X-OIR-Host-Claims-Version": signed.claims_version,
        "X-OIR-Host-Roles": "",
        "X-OIR-Host-Active-Bundle-Id": "",
        "X-OIR-Host-Policy-Version": signed.policy_version,
        "X-OIR-Host-Signature": f"v2={digest}",
    }

    service = asyncio.run(_service())
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        knowledge=SimpleNamespace(),
        knowledge_assets=service,
        registry=SimpleNamespace(load=lambda: None),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=SimpleNamespace(),
    )
    get_host_identity_verifier.cache_clear()
    get_host_nonce_store.cache_clear()
    app = create_oac_host_app()
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    try:
        response = TestClient(app).post(
            "/api/v1/admin/knowledge/files", content=body, headers=headers
        )
    finally:
        get_host_identity_verifier.cache_clear()
        get_host_nonce_store.cache_clear()

    assert response.status_code == 200
    assert response.json()["asset"]["owner"] == "admin-wire-1"


def test_coze_search_uses_irs_fallback_only_after_oir_read_failure() -> None:
    client = _client()
    ports = replace(
        client.app.dependency_overrides[get_oac_adapter_application_ports](),
        knowledge_assets=FailingKnowledgeSearch(),
    )
    legacy = StubIRSClient()
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    client.app.dependency_overrides[get_irs_fallback_gateway] = lambda: IRSFallbackGateway(
        mode="read_only",
        policy_version="test-policy",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
    )
    client.app.dependency_overrides[get_irs_legacy_client] = lambda: legacy

    response = client.post("/api/v1/knowledge/search", json=_search_body())

    assert response.status_code == 200
    assert response.json()["request_id"] == "request-search"
    assert legacy.calls == [("POST", "/api/v1/knowledge/search")]


class FailingKnowledgeSearch:
    async def search(self, _request):
        raise ConnectionError("OIR read unavailable")


class StubIRSClient:
    def __init__(self) -> None:
        self.calls = []

    async def request_json(self, *, method, path, json_body=None, query=None):
        del query
        self.calls.append((method, path))
        fixture = Path("tests/contract/oac_irs/irs-baseline/v1/success/knowledge-search.json")
        payload = json.loads(fixture.read_text(encoding="utf-8"))["response"]["body"]
        payload["request_id"] = json_body["request_id"]
        return payload
