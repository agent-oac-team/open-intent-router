from fastapi import Depends, FastAPI

from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.security import require_admin_token


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.post("/admin-only", dependencies=[Depends(require_admin_token)])
    async def admin_only() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_local_loopback_allows_admin_without_token(non_lifespan_test_client) -> None:
    settings = Settings(app_env="local", admin_api_token=None)
    client = non_lifespan_test_client(_app(settings))

    response = client.post("/admin-only")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_non_local_requires_admin_token_when_missing(non_lifespan_test_client) -> None:
    settings = Settings(app_env="production", admin_api_token=None)
    client = non_lifespan_test_client(_app(settings))

    response = client.post("/admin-only")

    assert response.status_code == 401


def test_configured_admin_token_is_required_even_in_local(non_lifespan_test_client) -> None:
    settings = Settings(app_env="local", admin_api_token="secret")
    client = non_lifespan_test_client(_app(settings))

    missing = client.post("/admin-only")
    valid = client.post("/admin-only", headers={"X-Admin-Token": "secret"})

    assert missing.status_code == 401
    assert valid.status_code == 200
