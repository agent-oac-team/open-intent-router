import pytest

from app.main import create_app as create_oir_app
from host_apps.oac.main import create_app as create_oac_host_app

RETIRED_KNOWLEDGE_PATHS = (
    ("POST", "/api/v1/knowledge/search"),
    ("POST", "/api/v1/knowledge/grouped-search"),
    ("POST", "/api/v1/knowledge/read"),
    ("GET", "/api/v1/knowledge/assets"),
    ("GET", "/api/v1/knowledge/assets/asset-1"),
    ("GET", "/api/v1/knowledge/assets/asset-1/chunks"),
    ("GET", "/api/v1/knowledge/chunks/chunk-1"),
    ("POST", "/api/v1/admin/knowledge/files"),
    ("GET", "/api/v1/admin/knowledge/files"),
    ("GET", "/api/v1/admin/knowledge/files/asset-1"),
    ("GET", "/api/v1/admin/knowledge/files/asset-1/chunks"),
    ("DELETE", "/api/v1/admin/knowledge/files/asset-1"),
    ("POST", "/api/v1/admin/knowledge/files/asset-1/retry"),
    ("POST", "/api/v1/admin/knowledge/index"),
    ("PUT", "/api/v1/admin/knowledge/acl"),
    ("DELETE", "/api/v1/admin/knowledge/cache"),
    ("GET", "/api/v1/admin/knowledge/audit"),
)


@pytest.mark.parametrize(("method", "path"), RETIRED_KNOWLEDGE_PATHS)
def test_oir_does_not_expose_knowledge_http(
    method: str, path: str, non_lifespan_test_client
) -> None:
    response = non_lifespan_test_client(create_oir_app()).request(method, path, json={})

    assert response.status_code == 404


@pytest.mark.parametrize(("method", "path"), RETIRED_KNOWLEDGE_PATHS)
def test_oac_host_does_not_proxy_or_fallback_knowledge_http(
    method: str, path: str, non_lifespan_test_client
) -> None:
    response = non_lifespan_test_client(create_oac_host_app()).request(method, path, json={})

    assert response.status_code == 404


def test_openapi_contains_no_knowledge_http_contract() -> None:
    for application in (create_oir_app(), create_oac_host_app()):
        paths = application.openapi()["paths"]
        assert all("/knowledge" not in path for path in paths)
