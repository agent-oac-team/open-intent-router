from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import memory_identity_signature
from app.dependencies import get_router_service
from app.main import create_app
from app.schemas.routing import RouteContext, RouteDecision, RouteRequest, RouteResponse
from tests.fakes.native_principal import native_principal_headers

_PRINCIPAL_SECRET = "native-principal-test-secret"


def _principal_headers(**updates) -> dict[str, str]:
    return native_principal_headers(
        secret=_PRINCIPAL_SECRET,
        roles=["operator"],
        groups=["support"],
        entitlements=["agent:summarizer"],
        attributes={"region": "east"},
        **updates,
    )


def test_signed_native_principal_is_the_only_authorization_source() -> None:
    service = _CapturingRouterService()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_router_service] = lambda: service

    response = TestClient(app).post(
        "/api/v1/route",
        headers=_principal_headers(),
        json={
            "request_id": "request-1",
            "session_id": "session-1",
            "user": {
                "id": "user-1",
                "roles": ["forged-role"],
                "groups": ["forged-group"],
                "entitlements": ["forged:entitlement"],
                "attributes": {"tenant_id": "tenant-1", "region": "forged"},
            },
            "input": {"text": "summarize this"},
        },
    )

    assert response.status_code == 200
    assert service.last_payload.user.model_dump() == {
        "id": "user-1",
        "roles": ["operator"],
        "groups": ["support"],
        "entitlements": ["agent:summarizer"],
        "attributes": {"region": "east", "tenant_id": "tenant-1"},
    }


def test_legacy_signed_owner_cannot_inherit_body_permissions() -> None:
    service = _CapturingRouterService()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        memory_identity_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_router_service] = lambda: service
    headers = {
        "X-User-ID": "legacy-user",
        "X-Tenant-ID": "legacy-tenant",
        "X-Memory-Identity-Signature": memory_identity_signature(
            user_id="legacy-user",
            tenant_id="legacy-tenant",
            secret=_PRINCIPAL_SECRET,
        ),
    }

    response = TestClient(app).post(
        "/api/v1/route",
        headers=headers,
        json={
            "request_id": "request-legacy",
            "session_id": "session-legacy",
            "user": {
                "id": "legacy-user",
                "roles": ["admin"],
                "groups": ["privileged"],
                "entitlements": ["agent:any"],
                "attributes": {"tenant_id": "legacy-tenant", "region": "forged"},
            },
            "input": {"text": "summarize this"},
        },
    )

    assert response.status_code == 200
    assert service.last_payload.user.model_dump() == {
        "id": "legacy-user",
        "roles": [],
        "groups": [],
        "entitlements": [],
        "attributes": {"tenant_id": "legacy-tenant"},
    }


def test_non_local_native_principal_fails_closed_before_routing() -> None:
    service = _CapturingRouterService()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_router_service] = lambda: service
    client = TestClient(app)
    body = _route_body()
    unsigned = _principal_headers()
    unsigned.pop("X-OIR-Principal-Signature")
    invalid = {**_principal_headers(), "X-OIR-Principal-Signature": "invalid"}

    responses = [
        client.post("/api/v1/route", headers=unsigned, json=body),
        client.post("/api/v1/route", headers=invalid, json=body),
        client.post("/api/v1/route", json=body),
    ]

    assert [response.status_code for response in responses] == [401, 401, 401]
    assert service.calls == 0


def test_local_loopback_accepts_unsigned_canonical_principal() -> None:
    service = _CapturingRouterService()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(app_env="local")
    app.dependency_overrides[get_router_service] = lambda: service
    headers = _principal_headers()
    headers.pop("X-OIR-Principal-Signature")

    response = TestClient(app).post(
        "/api/v1/route",
        headers=headers,
        json=_route_body(),
    )

    assert response.status_code == 200
    assert service.calls == 1


def test_body_owner_conflict_is_rejected_before_routing() -> None:
    service = _CapturingRouterService()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_router_service] = lambda: service
    client = TestClient(app)

    wrong_subject = _route_body()
    wrong_subject["user"]["id"] = "other-user"
    wrong_tenant = _route_body()
    wrong_tenant["user"]["attributes"]["tenant_id"] = "other-tenant"

    responses = [
        client.post("/api/v1/route", headers=_principal_headers(), json=wrong_subject),
        client.post("/api/v1/route", headers=_principal_headers(), json=wrong_tenant),
    ]

    assert [response.status_code for response in responses] == [401, 401]
    assert service.calls == 0


def _route_body() -> dict:
    return {
        "request_id": "request-1",
        "session_id": "session-1",
        "user": {
            "id": "user-1",
            "attributes": {"tenant_id": "tenant-1"},
        },
        "input": {"text": "summarize this"},
    }


class _CapturingRouterService:
    last_payload: RouteRequest

    def __init__(self) -> None:
        self.calls = 0

    async def route(self, payload: RouteRequest) -> RouteResponse:
        self.calls += 1
        self.last_payload = payload
        return RouteResponse(
            request_id=payload.request_id or "request-1",
            session_id=payload.session_id,
            assistant_message="ok",
            decision=RouteDecision(
                status="ok",
                action="reply",
                confidence=1,
                reason="test",
                message="ok",
            ),
            context=RouteContext(relation="new_task"),
        )
