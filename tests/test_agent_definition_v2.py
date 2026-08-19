import pytest
from pydantic import ValidationError

from app.schemas.agents import AgentDefinitionV2, InvocationHandling


def test_v2_invocation_definition_projects_handling_without_topology_details() -> None:
    definition = AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "summary-agent",
            "name": "Summary Agent",
            "description": "Summarizes a document.",
            "handling": {
                "kind": "invocation",
                "adapter_key": "local_function",
                "connector_ref": "tenant-tools",
                "config": {
                    "function": "side_effect",
                    "max_tokens": 1024,
                    "token_budget": 512,
                },
            },
        }
    )

    public = definition.to_public()
    candidate = definition.to_candidate()
    admin = definition.to_admin()

    assert public.handling_kind == "invocation"
    assert "adapter_key" not in public.model_dump()
    assert "connector_ref" not in public.model_dump()
    assert candidate.handling_kind == "invocation"
    assert "adapter_key" not in candidate.model_dump()
    assert "connector_ref" not in candidate.model_dump()
    assert admin.handling == {
        "kind": "invocation",
        "adapter_key": "***REDACTED***",
        "connector_ref": "***REDACTED***",
        "config": {
            "function": "***REDACTED***",
            "max_tokens": 1024,
            "token_budget": 512,
        },
    }


@pytest.mark.parametrize(
    ("handling", "handling_kind", "admin_handling"),
    [
        (
            {
                "kind": "external_execution",
                "executor_ref": "host-executor",
                "params": {"task": "create_ticket", "priority": 10},
            },
            "external_execution",
            {
                "kind": "external_execution",
                "executor_ref": "***REDACTED***",
                "params": {"task": "***REDACTED***", "priority": 10},
            },
        ),
        (
            {
                "kind": "ui_handoff",
                "route": "/agents/summary",
                "params": {"tab": "history", "include_history": True},
            },
            "ui_handoff",
            {
                "kind": "ui_handoff",
                "route": "***REDACTED***",
                "params": {"tab": "***REDACTED***", "include_history": True},
            },
        ),
    ],
)
def test_v2_definition_accepts_each_remaining_closed_handling(
    handling: dict[str, object], handling_kind: str, admin_handling: dict[str, object]
) -> None:
    definition = AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": f"{handling_kind}-agent",
            "name": "Agent",
            "description": "A handled agent.",
            "handling": handling,
        }
    )

    assert definition.handling.kind == handling_kind
    assert definition.to_public().handling_kind == handling_kind
    assert definition.to_candidate().handling_kind == handling_kind
    assert definition.to_admin().handling == admin_handling


@pytest.mark.parametrize(
    "handling",
    [
        {
            "kind": "invocation",
            "adapter_key": "http",
            "config": {"url": "https://remote.example/invoke"},
        },
        {
            "kind": "invocation",
            "adapter_key": "http",
            "config": {"headers": {"Authorization": "Bearer secret"}},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"access_token": "secret"},
        },
        {
            "kind": "ui_handoff",
            "route": "/agents/summary",
            "params": {"client_secret": "secret"},
        },
        {
            "kind": "invocation",
            "adapter_key": "http",
            "config": {"baseUrl": "https://remote.example/invoke"},
        },
        {
            "kind": "invocation",
            "adapter_key": "http",
            "config": {"authorizationHeader": "Bearer secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"x-api-key": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"XApiKey": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"secretValue": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"tokenValue": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"jwtValue": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"secretvalue": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"tokenvalue": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"clienttoken": "opaque-value"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"authcode": "opaque-value"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"lookupkey": "opaque-value"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"privatekeypem": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"credentialsvalue": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"hostnamevalue": "api.example.com"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"privateKeyPem": "secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"host": "api.example.com", "port": 443},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "https://remote.example/execute"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "https:%2F%2Fremote.example%2Fexecute"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "https:\\\\remote.example/execute"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "https:////remote.example/execute"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "https:%5C%5Cremote.example%2Fexecute"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "redis:deployment.internal:6379"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "Bearer super-secret-token"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "api_key=super-secret"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"passphrase": "deployment-passphrase-123456"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"headervalue": "opaque123456"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"cookievalue": "opaque123456"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "127.0.0.1:8080"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "eyJheader.payload.signature"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "sk-proj-0123456789abcdefghijklmnop"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "AKIA0123456789ABCDEF"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "ghp_012345678901234567890123456789012345"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "github_pat_0123456789_abcdefghijklmnop"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "glpat-0123456789abcdefghij"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "xoxb-0123456789-0123456789-abcdefghij"},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            # Assemble the secret-shaped test value at runtime so the source
            # fixture cannot be mistaken for a credential by push protection.
            "params": {"operation": "sk_live_" + ("0" * 24)},
        },
        {
            "kind": "external_execution",
            "executor_ref": "host-executor",
            "params": {"operation": "AIzaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        },
        {
            "kind": "ui_handoff",
            "route": "/agents/summary",
            "params": {"nested": {"clientSecret": "secret"}},
        },
    ],
)
def test_v2_definition_rejects_sensitive_deployment_configuration(
    handling: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AgentDefinitionV2.model_validate(
            {
                "schema_version": "oir-agent-v2",
                "agent_id": "secure-agent",
                "name": "Secure Agent",
                "description": "A secure handled agent.",
                "handling": handling,
            }
        )


@pytest.mark.parametrize(
    "route",
    [
        "https://evil.example/agent",
        "//evil",
        "/a/../b",
        "/a\\b",
        "/%2e%2e/secret",
        "/.%2e/secret",
        "/%252e%252e/secret",
        "/%252525252e%252525252e/secret",
        "/%2f%2fevil",
        "/agents/summary?token=secret",
        "/agents/summary#token=secret",
    ],
)
def test_v2_ui_handoff_rejects_unsafe_routes(route: str) -> None:
    with pytest.raises(ValidationError):
        AgentDefinitionV2.model_validate(
            {
                "schema_version": "oir-agent-v2",
                "agent_id": "handoff-agent",
                "name": "Handoff Agent",
                "description": "An agent with a UI handoff.",
                "handling": {"kind": "ui_handoff", "route": route},
            }
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"type": "http"},
        {"invocation": {"type": "http", "config": {}}},
        {"ui_handoff": {"mode": "route", "route": "/legacy"}},
        {"agent_kind": "workflow"},
        {"metadata": {"handling": {"kind": "ui_handoff", "route": "/override"}}},
        {"host_handling": {"kind": "ui_handoff", "route": "/override"}},
        {
            "handling": {
                "kind": "invocation",
                "adapter_key": "mock",
                "executor_ref": "host-executor",
            }
        },
        {
            "handling": {
                "kind": "external_execution",
                "executor_ref": "host-executor",
                "adapter_key": "mock",
            }
        },
        {
            "handling": {
                "kind": "ui_handoff",
                "route": "/agents/strict",
                "config": {"operation": "override"},
            }
        },
        {"handling": {"kind": "unknown"}},
    ],
)
def test_v2_definition_rejects_legacy_or_mixed_handling_fields(
    updates: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "schema_version": "oir-agent-v2",
        "agent_id": "strict-agent",
        "name": "Strict Agent",
        "description": "An agent with one handling.",
        "handling": {"kind": "invocation", "adapter_key": "mock"},
    }
    payload.update(updates)

    with pytest.raises(ValidationError):
        AgentDefinitionV2.model_validate(payload)


@pytest.mark.parametrize(
    "handling",
    [
        {"kind": "invocation", "adapter_key": "https://adapter.example"},
        {
            "kind": "invocation",
            "adapter_key": "http",
            "connector_ref": "https://connector.example",
        },
        {"kind": "external_execution", "executor_ref": "host executor"},
    ],
)
def test_v2_definition_requires_logical_handling_references(
    handling: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AgentDefinitionV2.model_validate(
            {
                "schema_version": "oir-agent-v2",
                "agent_id": "logical-reference-agent",
                "name": "Logical Reference Agent",
                "description": "An agent that uses logical references.",
                "handling": handling,
            }
        )


def test_v2_definition_retains_common_agent_contract_in_safe_projections() -> None:
    definition = AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "governed-summary-agent",
            "name": "Governed Summary Agent",
            "description": "Summarizes only approved documents.",
            "version": "2026.08",
            "revision": 4,
            "capabilities": ["summarize"],
            "domain": "documents",
            "tags": ["governed"],
            "trigger": {"keywords": ["summary"]},
            "access_policy": {"allow_tenants": ["tenant-a"]},
            "required_inputs": ["text"],
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
            },
            "priority": 10,
            "source": "file",
            "handling": {
                "kind": "invocation",
                "adapter_key": "local_function",
                "connector_ref": "tenant-tools",
                "config": {"function": "side_effect", "max_tokens": 256},
            },
        }
    )

    public = definition.to_public().model_dump()
    candidate = definition.to_candidate().model_dump()
    admin = definition.to_admin().model_dump()

    assert public["revision"] == 4
    assert public["capabilities"] == ["summarize"]
    assert public["access_policy"] == {
        "allow_roles": [],
        "allow_groups": [],
        "allow_tenants": ["tenant-a"],
        "deny_roles": [],
        "deny_groups": [],
        "deny_tenants": [],
        "any_entitlements": [],
        "required_attributes": {},
    }
    assert public["handling_kind"] == "invocation"
    assert "handling" not in public
    assert candidate["capabilities"] == ["summarize"]
    assert candidate["handling_kind"] == "invocation"
    assert "connector_ref" not in candidate
    assert admin["handling"] == {
        "kind": "invocation",
        "adapter_key": "***REDACTED***",
        "connector_ref": "***REDACTED***",
        "config": {"function": "***REDACTED***", "max_tokens": 256},
    }
    assert admin["input_schema"]["required"] == ["text"]


def test_v2_definition_adds_required_inputs_to_its_input_schema() -> None:
    definition = AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "required-input-agent",
            "name": "Required Input Agent",
            "description": "An agent that needs a query.",
            "required_inputs": ["query"],
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "handling": {"kind": "invocation", "adapter_key": "mock"},
        }
    )

    assert definition.input_schema.required == ["query"]


def test_v2_admin_projection_redacts_defensively_constructed_handling() -> None:
    definition = AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "defensive-projection-agent",
            "name": "Defensive Projection Agent",
            "description": "Projects handling safely even if an internal caller bypasses validation.",
            "handling": {"kind": "invocation", "adapter_key": "local_function"},
        }
    )
    definition.handling = InvocationHandling.model_construct(
        kind="invocation",
        adapter_key="local_function",
        config={"operation": "Bearer super-secret-token"},
    )

    handling = definition.to_admin().model_dump()["handling"]

    assert handling == {"kind": "invocation", "redacted": True}


def test_v2_admin_projection_redacts_unknown_bypassed_configuration() -> None:
    definition = AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "defensive-extra-field-agent",
            "name": "Defensive Extra Field Agent",
            "description": "Does not expose configuration fields that bypass the schema.",
            "handling": {"kind": "invocation", "adapter_key": "local_function"},
        }
    )
    definition.handling = InvocationHandling.model_construct(
        kind="invocation",
        adapter_key="local_function",
        config={"headervalue": "opaque123456"},
    )

    handling = definition.to_admin().model_dump()["handling"]

    assert handling == {"kind": "invocation", "redacted": True}


def test_v2_definition_rejects_unapproved_configuration_fields() -> None:
    with pytest.raises(ValidationError):
        AgentDefinitionV2.model_validate(
            {
                "schema_version": "oir-agent-v2",
                "agent_id": "closed-config-agent",
                "name": "Closed Config Agent",
                "description": "Only explicit non-sensitive operation settings are accepted.",
                "handling": {
                    "kind": "invocation",
                    "adapter_key": "mock",
                    "config": {"custom_label": "summarize"},
                },
            }
        )


@pytest.mark.parametrize("schema_version", [None, "oir-agent-v1", "oir-agent-v2-draft"])
def test_v2_definition_requires_exact_schema_version(schema_version: str | None) -> None:
    payload: dict[str, object] = {
        "agent_id": "schema-version-agent",
        "name": "Schema Version Agent",
        "description": "Requires the v2 schema marker.",
        "handling": {"kind": "invocation", "adapter_key": "mock"},
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version

    with pytest.raises(ValidationError):
        AgentDefinitionV2.model_validate(payload)


def test_v2_definition_preserves_revision_changes_in_projections() -> None:
    payload: dict[str, object] = {
        "schema_version": "oir-agent-v2",
        "agent_id": "revisioned-agent",
        "name": "Revisioned Agent",
        "description": "Tracks immutable definition revisions.",
        "handling": {"kind": "invocation", "adapter_key": "mock"},
    }

    first = AgentDefinitionV2.model_validate({**payload, "revision": 3})
    revised = AgentDefinitionV2.model_validate({**payload, "revision": 4})

    assert first.to_public().revision == 3
    assert revised.to_public().revision == 4
    assert revised.to_admin().revision == 4
