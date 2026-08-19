from dataclasses import dataclass
from importlib import import_module

import pytest
from fastapi.testclient import TestClient

from app.application import RoutingApplicationPort
from app.core.config import Settings
from app.schemas.routing import RouteContext, RouteDecision, RouteResponse
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_apps.oac.config import (
    OacHostProfile,
    OacHostSettings,
    build_oac_host_profile,
    validate_host_credential_profiles,
    validate_registry_single_writer,
)
from host_apps.oac.dependencies import get_oac_adapter_application_ports
from host_apps.oac.main import create_app as create_oac_host_app


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
    monkeypatch.setenv("OAC_HOST_IRS_FALLBACK_BASE_URL", "http://retired.invalid")
    monkeypatch.setenv("OAC_HOST_COZE_WORKFLOW_CREDENTIAL", "test-placeholder")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./data/core-only.db")

    host = OacHostSettings(_env_file=None)
    core = Settings(_env_file=None)
    profile = build_oac_host_profile(core=core, host=host)

    assert profile.host.identity_audience == "oac-oir-adapter-test"
    for retired_name in (
        "fallback_mode",
        "irs_fallback_base_url",
        "irs_fallback_service_token",
        "irs_control_write_enabled",
        "irs_runtime_write_enabled",
        "irs_registry_write_enabled",
    ):
        assert not hasattr(profile.host, retired_name)
    assert profile.host.coze_workflow_credential is not None
    assert profile.core.database_url == "sqlite+aiosqlite:///./data/core-only.db"
    assert not hasattr(profile.core, "coze_workflow_credential")
    assert not hasattr(profile.host, "database_url")


def test_oac_external_executor_capabilities_are_explicit_host_configuration() -> None:
    host = OacHostSettings(
        _env_file=None,
        external_executor_refs="registered_bot, another_bot,registered_bot",
    )

    assert host.supported_external_executor_refs == {"registered_bot", "another_bot"}


def test_external_executor_capability_requires_a_durable_ticket_secret() -> None:
    host = OacHostSettings(_env_file=None, external_executor_refs="registered_bot")

    with pytest.raises(ValueError, match="External Execution requires"):
        build_oac_host_profile(core=Settings(_env_file=None), host=host)

    profile = build_oac_host_profile(
        core=Settings(_env_file=None, execution_ticket_secret="ticket-secret"),
        host=host,
    )
    assert profile.host.supported_external_executor_refs == {"registered_bot"}


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

    assert response.status_code == 404


def test_native_knowledge_search_is_not_exposed_under_internal_prefix() -> None:
    client = TestClient(create_oac_host_app())

    response = client.post(
        "/oir/api/v1/knowledge/search",
        json={"query": "native query", "user": {"id": "user-1"}},
    )

    assert response.status_code == 404


def test_adapter_composition_exposes_only_public_application_ports() -> None:
    ports = get_oac_adapter_application_ports()

    assert isinstance(ports, OacAdapterApplicationPorts)
    assert isinstance(ports.routing, RoutingApplicationPort)
    assert not hasattr(ports, "knowledge")
    assert not hasattr(ports, "knowledge_assets")
    assert not hasattr(ports, "run_repository")


def test_host_runtime_has_no_irs_fallback_dependency_aliases() -> None:
    dependencies = import_module("host_apps.oac.dependencies")

    assert not hasattr(dependencies, "get_irs_fallback_gateway")
    assert not hasattr(dependencies, "get_irs_legacy_client")


def test_recording_application_port_doubles_match_public_protocols() -> None:
    routing = RecordingRoutingPort(
        response=RouteResponse(
            request_id="request-1",
            session_id="session-1",
            decision=RouteDecision(action="unsupported", reason="test"),
            context=RouteContext(),
        )
    )
    assert isinstance(routing, RoutingApplicationPort)


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
        "authorization",
    }
    assert body["governance"]["write_fence"] == "enabled"
    assert set(body["governance"]) == {"write_fence", "write_freeze"}
    assert set(body["versions"]) == {"adapter", "core", "schema", "policy"}
    assert set(body["modes"]) == {"memory", "shadow"}
    authorization = body["authorization"]
    assert authorization == {
        "current_signature_version": "v2",
        "accepted_signature_versions": ["v2"],
        "v1_compatibility_enabled": False,
        "central_route_required_signature_version": "v2",
        "claims_version": "oac-principal-v1",
        "policy_version": "oac-authz-v1",
        "bundle_catalog": "ok",
        "credential_profile_catalog": "ok",
        "signature_usage": authorization["signature_usage"],
    }
    assert all(
        set(item)
        == {
            "signature_version",
            "credential_class",
            "operation",
            "outcome",
            "count",
        }
        for item in authorization["signature_usage"]
    )
    serialized = response.text.lower()
    assert "signing_key" not in serialized
    assert "key_id" not in serialized
    assert "subject" not in serialized
    assert 'signature":' not in serialized
    assert "token" not in serialized
    assert "ticket" not in serialized
    assert "workspace.operations.access" not in serialized


def test_registry_single_writer_gate_rejects_file_and_feishu_restore_paths() -> None:
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


def test_host_profile_rejects_claims_or_bundle_policy_version_drift() -> None:
    with pytest.raises(ValueError, match="authorization policy version"):
        build_oac_host_profile(
            host=OacHostSettings(_env_file=None, authz_policy_version="unknown-policy")
        )
    with pytest.raises(ValueError, match="authorization policy version"):
        build_oac_host_profile(
            host=OacHostSettings(_env_file=None, claims_version="unknown-claims")
        )


def test_host_profile_requires_three_distinct_current_credentials() -> None:
    with pytest.raises(ValueError, match="require a key"):
        validate_host_credential_profiles(
            OacHostProfile(
                core=Settings(),
                host=OacHostSettings(
                    _env_file=None,
                    identity_current_key_id=None,
                    identity_current_key=None,
                ),
            )
        )
    with pytest.raises(ValueError, match="cannot cross credential classes"):
        validate_host_credential_profiles(
            OacHostProfile(
                core=Settings(),
                host=OacHostSettings(
                    _env_file=None,
                    identity_current_key_id="shared-key",
                    oac_admin_key_id="shared-key",
                ),
            )
        )


@dataclass
class RecordingRoutingPort:
    response: RouteResponse

    async def route(self, request):
        return self.response
