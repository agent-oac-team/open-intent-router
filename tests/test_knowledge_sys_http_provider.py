import asyncio
import json
from dataclasses import dataclass

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.adapters.knowledge_sys import KnowledgeSysHttpProvider
from app.core.config import Settings, get_settings
from app.dependencies import build_knowledge_provider
from app.main import create_app
from app.schemas.common import UserContext
from app.schemas.knowledge_provider import (
    KnowledgeProviderRequest,
    KnowledgeRetrievalBudget,
)


@pytest.fixture
def rsa_signing_material() -> tuple[str, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        private_key.public_key(),
    )


def _request(**updates) -> KnowledgeProviderRequest:
    values = {
        "query": "退款规则",
        "principal": UserContext(
            id="user-1",
            roles=["advisor"],
            groups=["north"],
            attributes={
                "tenant_id": "tenant-1",
                "principal_type": "user",
                "knowledge_access_tags": ["internal"],
            },
        ),
        "purpose": "agent_execution",
        "consumer": "agent:refund-assistant",
        "source_ids": ["policy/refund"],
        "source_tags": ["approved"],
        "budget": KnowledgeRetrievalBudget(max_items=3),
        "trace_context": {"request_id": "trace-1", "session_id": "session-1"},
    }
    values.update(updates)
    return KnowledgeProviderRequest.model_validate(values)


def _provider(
    signing_material,
    handler,
    *,
    clock=lambda: 100.0,
) -> KnowledgeSysHttpProvider:
    private_key, _ = signing_material
    return KnowledgeSysHttpProvider(
        base_url="https://knowledge.example",
        signing_private_key=private_key,
        signing_key_id="oir-key-1",
        transport=httpx.MockTransport(handler),
        clock=clock,
        wall_clock=lambda: 1_800_000_000.0,
    )


@pytest.mark.asyncio
async def test_http_provider_signs_identity_and_maps_search_result(rsa_signing_material) -> None:
    _, public_key = rsa_signing_material
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        captured["token"] = token
        captured["claims"] = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience="knowledge_sys",
            issuer="oir",
            options={"verify_iat": False},
        )
        captured["header"] = jwt.get_unverified_header(token)
        return httpx.Response(
            200,
            json={
                "request_id": "trace-1",
                "matched": True,
                "confidence": 0.91,
                "trace_id": "knowledge-trace-1",
                "warnings": [],
                "evidence": [
                    {
                        "evidence_id": "evidence-1",
                        "item_id": "item-1",
                        "citation": {"source_id": "policy/refund"},
                        "chunk_id": "chunk-1",
                        "asset_id": "asset-1",
                        "content": "退款须在七日内申请。",
                        "title": "退款规则",
                        "source_type": "document",
                        "source_name": "refund.md",
                        "source_ref": {
                            "source_type": "document",
                            "uri": "knowledge://refund",
                        },
                        "confidence": 0.91,
                        "freshness": "current",
                        "sensitivity": "internal",
                    }
                ],
            },
        )

    result = await _provider(rsa_signing_material, handler).retrieve(_request())

    assert captured["url"] == "https://knowledge.example/api/v1/knowledge/search"
    assert captured["header"] == {"alg": "RS256", "kid": "oir-key-1", "typ": "JWT"}
    claims = captured["claims"]
    assert claims["iss"] == "oir"
    assert claims["sub"] == "user-1"
    assert claims["aud"] == "knowledge_sys"
    assert claims["tenant_id"] == "tenant-1"
    assert claims["principal_type"] == "user"
    assert claims["scopes"] == ["knowledge:read"]
    assert claims["access_tags"] == ["internal"]
    assert claims["exp"] - claims["iat"] == 60
    body = captured["body"]
    assert body["request_id"] == "trace-1"
    assert body["session_id"] == "session-1"
    assert body["user_id"] == "user-1"
    assert body["tenant_id"] == "tenant-1"
    assert body["principal_type"] == "user"
    assert body["consumer"] == "agent"
    assert body["consumer_id"] == "agent:refund-assistant"
    assert body["purpose"] == "execute"
    assert body["filters"]["source_ids"] == ["policy/refund"]
    assert body["filters"]["source_tags"] == ["approved"]
    assert body["top_k"] == 3
    assert result.status == "ok"
    assert result.trace_id == "knowledge-trace-1"
    assert result.items[0].model_dump(mode="json") == {
        "item_id": "item-1",
        "source_id": "policy/refund",
        "content": "退款须在七日内申请。",
        "score": 0.91,
        "title": "退款规则",
        "uri": "knowledge://refund",
        "updated_at": None,
        "citation": {
            "source_id": "policy/refund",
            "chunk_id": None,
            "title": None,
            "uri": None,
            "metadata": {},
        },
        "metadata": {"asset_id": "asset-1", "chunk_id": "chunk-1"},
    }
    assert result.citations[0].source_id == "policy/refund"


@pytest.mark.asyncio
async def test_http_provider_timeout_has_one_attempt_and_no_sensitive_error(
    rsa_signing_material,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("secret body and bearer token", request=request)

    result = await _provider(rsa_signing_material, handler).retrieve(_request())

    assert calls == 1
    assert result.status == "timeout"
    assert result.error_code == "knowledge_provider_timeout"
    assert "secret" not in result.model_dump_json()
    assert "Bearer" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_http_provider_maps_empty_and_auth_failure_without_opening_circuit(
    rsa_signing_material,
) -> None:
    responses = [
        httpx.Response(
            200,
            json={
                "request_id": "trace-1",
                "matched": False,
                "confidence": 0,
                "trace_id": "knowledge-trace-empty",
                "warnings": [
                    {"code": "no_match", "message": "none", "detail": {}},
                ],
                "evidence": [],
            },
        ),
        httpx.Response(401, json={"detail": "invalid_token"}),
    ] * 3
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    provider = _provider(rsa_signing_material, handler)
    results = [await provider.retrieve(_request()) for _ in range(6)]

    assert [item.status for item in results] == ["empty", "denied"] * 3
    assert results[1].error_code == "knowledge_auth_denied"
    assert calls == 6


@pytest.mark.asyncio
async def test_http_provider_opens_circuit_after_five_counted_failures(
    rsa_signing_material,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "provider unavailable"})

    provider = _provider(rsa_signing_material, handler)
    failures = [await provider.retrieve(_request()) for _ in range(5)]
    rejected = await provider.retrieve(_request())

    assert calls == 5
    assert all(item.status == "error" for item in failures)
    assert all(item.error_code == "knowledge_provider_unavailable" for item in failures)
    assert rejected.status == "error"
    assert rejected.error_code == "knowledge_provider_circuit_open"


@dataclass
class _MutableClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value


@pytest.mark.asyncio
async def test_http_provider_allows_one_half_open_probe_and_recovers(
    rsa_signing_material,
) -> None:
    clock = _MutableClock()
    calls = 0
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 5:
            return httpx.Response(503, json={"detail": "unavailable"})
        probe_started.set()
        await release_probe.wait()
        return httpx.Response(
            200,
            json={
                "request_id": "trace-1",
                "matched": False,
                "confidence": 0,
                "trace_id": "trace-recovered",
                "warnings": [],
                "evidence": [],
            },
        )

    provider = _provider(rsa_signing_material, handler, clock=clock)
    for _ in range(5):
        await provider.retrieve(_request())
    clock.value += 30

    probe_task = asyncio.create_task(provider.retrieve(_request()))
    await probe_started.wait()
    concurrent = await provider.retrieve(_request())
    release_probe.set()
    recovered = await probe_task
    after_recovery = await provider.retrieve(_request())

    assert calls == 7
    assert concurrent.error_code == "knowledge_provider_circuit_open"
    assert recovered.status == "empty"
    assert after_recovery.status == "empty"


@pytest.mark.asyncio
async def test_http_provider_rejects_missing_tenant_without_sending_request(
    rsa_signing_material,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    provider = _provider(rsa_signing_material, handler)
    result = await provider.retrieve(_request(principal=UserContext(id="user-1", attributes={})))

    assert calls == 0
    assert result.status == "denied"
    assert result.error_code == "knowledge_identity_missing"


def test_oir_jwks_exposes_only_public_rsa_material(rsa_signing_material) -> None:
    private_key, _ = rsa_signing_material
    settings = Settings(
        _env_file=None,
        storage_backend="memory",
        knowledge_provider_base_url="https://knowledge.example",
        knowledge_provider_jwt_private_key=private_key,
        knowledge_provider_jwt_key_id="oir-key-1",
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    response = TestClient(app).get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert response.json() == {
        "keys": [
            {
                "kid": "oir-key-1",
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "n": response.json()["keys"][0]["n"],
                "e": response.json()["keys"][0]["e"],
            }
        ]
    }
    assert response.json()["keys"][0]["n"]
    assert response.json()["keys"][0]["e"]
    assert not ({"d", "p", "q", "dp", "dq", "qi"} & response.json()["keys"][0].keys())
    assert "PRIVATE" not in response.text


def test_provider_configuration_supports_private_key_file_and_optional_provider(
    rsa_signing_material,
    tmp_path,
) -> None:
    private_key, _ = rsa_signing_material
    key_path = tmp_path / "oir-provider-private.pem"
    key_path.write_text(private_key)
    disabled = Settings(_env_file=None, storage_backend="memory")
    enabled = Settings(
        _env_file=None,
        storage_backend="memory",
        knowledge_provider_base_url="https://knowledge.example",
        knowledge_provider_jwt_private_key_file=str(key_path),
        knowledge_provider_jwt_key_id="oir-key-2",
        knowledge_provider_jwt_ttl_seconds=300,
    )

    assert build_knowledge_provider(disabled) is None
    assert isinstance(build_knowledge_provider(enabled), KnowledgeSysHttpProvider)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("knowledge_provider_jwt_issuer", "irs", "must be oir"),
        (
            "knowledge_provider_jwt_audience",
            "intent_recon_sys",
            "must be knowledge_sys",
        ),
    ],
)
def test_provider_configuration_rejects_noncanonical_service_identity(
    rsa_signing_material,
    field,
    value,
    message,
) -> None:
    private_key, _ = rsa_signing_material
    with pytest.raises(ValueError, match=message):
        Settings(
            _env_file=None,
            storage_backend="memory",
            knowledge_provider_base_url="https://knowledge.example",
            knowledge_provider_jwt_private_key=private_key,
            knowledge_provider_jwt_key_id="oir-key-2",
            **{field: value},
        )


def test_provider_configuration_rejects_token_ttl_over_five_minutes() -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            storage_backend="memory",
            knowledge_provider_jwt_ttl_seconds=301,
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8083",
        "http://127.0.0.1:8083",
        "http://[::1]:8083",
    ],
)
def test_provider_accepts_loopback_http(rsa_signing_material, base_url) -> None:
    private_key, _ = rsa_signing_material

    provider = KnowledgeSysHttpProvider(
        base_url=base_url,
        signing_private_key=private_key,
        signing_key_id="oir-key-1",
    )

    assert isinstance(provider, KnowledgeSysHttpProvider)


def test_provider_rejects_remote_plain_http(rsa_signing_material) -> None:
    private_key, _ = rsa_signing_material

    with pytest.raises(ValueError, match="HTTPS or loopback HTTP"):
        KnowledgeSysHttpProvider(
            base_url="http://knowledge.internal:8083",
            signing_private_key=private_key,
            signing_key_id="oir-key-1",
        )
