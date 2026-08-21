from app.core.config import Settings, get_settings
from app.main import create_app


def test_local_ui_origin_can_preflight_runtime_config(non_lifespan_test_client) -> None:
    settings = Settings(
        storage_backend="memory",
        registry_backend="database",
        cors_allow_origins="http://127.0.0.1:5174",
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    client = non_lifespan_test_client(app)

    response = client.options(
        "/api/v1/runtime/config",
        headers={
            "Origin": "http://127.0.0.1:5174",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5174"
