from importlib import import_module

import pytest
from fastapi.testclient import TestClient

from app.application import KnowledgeApplicationPort, RoutingApplicationPort
from app.core.config import Settings
from app.schemas.knowledge import KnowledgeSearchResponse
from app.schemas.routing import RouteContext, RouteDecision, RouteResponse
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_apps.oac.config import (
    OacHostProfile,
    OacHostSettings,
    build_oac_host_profile,
    validate_registry_single_writer,
)
from host_apps.oac.dependencies import get_oac_adapter_application_ports
from host_apps.oac.main import create_app as create_oac_host_app
from tests.fakes.application_ports import RecordingKnowledgePort, RecordingRoutingPort


@pytest.mark.parametrize(
    "module_name",
    [
        "host_adapters.oac.api",
        "host_adapters.oac.schemas",
        "host_adapters.oac.mappers",
        "host_adapters.oac.identity",
        "host_adapters.oac.shadow",
        "host_adapters.oac.fallback",
        "host_adapters.oac.repositories",
    ],
)
def test_oac_host_adapter_exposes_logically_independent_modules(module_name: str) -> None:
    assert import_module(module_name).__name__ == module_name


def test_oac_host_composition_root_includes_oir_core() -> None:
    app = create_oac_host_app()
    client = TestClient(app)

    assert app.state.host_runtime == "oac"
    assert app.state.identity_audience == "oac-oir-adapter-local"
    assert app.state.native_api_prefix == "/oir/api/v1"
    assert client.get("/health").status_code == 200
    assert client.get("/oir/health").json() == {"status": "ok"}


def test_oac_host_profile_keeps_host_configuration_out_of_core(monkeypatch) -> None:
    monkeypatch.setenv("OAC_HOST_ENVIRONMENT", "test")
    monkeypatch.setenv("OAC_HOST_IRS_FALLBACK_BASE_URL", "http://127.0.0.1:18081")
    monkeypatch.setenv("OAC_HOST_COZE_WORKFLOW_CREDENTIAL", "test-placeholder")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./data/core-only.db")

    host = OacHostSettings(_env_file=None)
    core = Settings(_env_file=None)
    profile = build_oac_host_profile(core=core, host=host)

    assert profile.host.identity_audience == "oac-oir-adapter-test"
    assert profile.host.irs_fallback_base_url == "http://127.0.0.1:18081"
    assert profile.host.coze_workflow_credential is not None
    assert profile.core.database_url == "sqlite+aiosqlite:///./data/core-only.db"
    assert not hasattr(profile.core, "coze_workflow_credential")
    assert not hasattr(profile.host, "database_url")


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "native-shaped", "user": {"id": "user-1"}},
        {"query": "legacy-shaped", "consumer": "coze_workflow", "filters": {}},
    ],
)
def test_legacy_knowledge_search_path_never_guesses_protocol_from_body(payload: dict) -> None:
    client = TestClient(create_oac_host_app())

    response = client.post("/api/v1/knowledge/search", json=payload)

    assert response.status_code == 401
    assert response.json() == {"detail": "host_authentication_failed"}


def test_native_knowledge_search_is_available_only_under_internal_prefix() -> None:
    client = TestClient(create_oac_host_app())

    response = client.post(
        "/oir/api/v1/knowledge/search",
        json={"query": "native query", "user": {"id": "user-1"}},
    )

    assert response.status_code == 200
    assert "context" in response.json()


def test_adapter_composition_exposes_only_public_application_ports() -> None:
    ports = get_oac_adapter_application_ports()

    assert isinstance(ports, OacAdapterApplicationPorts)
    assert isinstance(ports.routing, RoutingApplicationPort)
    assert isinstance(ports.knowledge, KnowledgeApplicationPort)
    assert not hasattr(ports, "run_repository")


def test_recording_application_port_doubles_match_public_protocols() -> None:
    routing = RecordingRoutingPort(
        response=RouteResponse(
            request_id="request-1",
            session_id="session-1",
            decision=RouteDecision(action="unsupported", reason="test"),
            context=RouteContext(),
        )
    )
    knowledge = RecordingKnowledgePort(response=KnowledgeSearchResponse())

    assert isinstance(routing, RoutingApplicationPort)
    assert isinstance(knowledge, KnowledgeApplicationPort)


def test_host_capabilities_are_versioned_and_redacted() -> None:
    response = TestClient(create_oac_host_app()).get("/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "status",
        "host",
        "versions",
        "modes",
        "dependencies",
        "governance",
    }
    assert body["governance"]["write_fence"] == "enabled"
    assert body["governance"]["circuit"] in {"closed", "open", "half_open"}
    assert set(body["versions"]) == {"adapter", "core", "schema", "policy"}
    assert set(body["modes"]) == {"knowledge", "memory", "shadow", "fallback"}
    serialized = response.text.lower()
    assert "credential" not in serialized
    assert "signing_key" not in serialized
    assert "token" not in serialized
    assert "ticket" not in serialized


def test_registry_single_writer_gate_rejects_file_feishu_and_irs_restore_paths() -> None:
    host = OacHostSettings(_env_file=None, enforce_registry_single_writer=True)
    with pytest.raises(ValueError, match="only writable Registry source"):
        validate_registry_single_writer(
            OacHostProfile(core=Settings(registry_backend="file"), host=host)
        )

    valid = build_oac_host_profile(
        core=Settings(registry_backend="database", registry_file_fallback_on_empty=False),
        host=host,
    )
    assert valid.host.feishu_registry_sync_enabled is False

    with pytest.raises(ValueError, match="only writable Registry source"):
        build_oac_host_profile(
            core=Settings(registry_backend="database"),
            host=OacHostSettings(
                _env_file=None,
                enforce_registry_single_writer=True,
                feishu_registry_sync_enabled=True,
            ),
        )
