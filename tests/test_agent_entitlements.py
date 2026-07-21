import json

import pytest
from pydantic import ValidationError

from app.repositories.file_registry import FileRegistrySource
from app.schemas.agents import AccessPolicy, AgentDefinition, InvocationSpec
from app.schemas.common import UserContext


def _agent(policy: AccessPolicy) -> AgentDefinition:
    return AgentDefinition(
        agent_id="entitled-agent",
        name="Entitled Agent",
        description="Tests generic entitlement admission",
        type="mock",
        access_policy=policy,
        invocation=InvocationSpec(type="mock"),
    )


def test_entitlements_are_trimmed_deduplicated_and_stably_sorted() -> None:
    user = UserContext(
        id="u1",
        entitlements=[
            " workspace.sales.access ",
            "workspace.ops.access",
            "workspace.ops.access",
            "",
        ],
    )
    policy = AccessPolicy(any_entitlements=["workspace.sales.access", " workspace.ops.access "])

    assert user.entitlements == ["workspace.ops.access", "workspace.sales.access"]
    assert policy.any_entitlements == ["workspace.ops.access", "workspace.sales.access"]


@pytest.mark.parametrize(
    "value",
    ["运营版", "workspace.ops.access\nadmin", "workspace ops access", "*"],
)
def test_entitlements_reject_unsafe_or_non_ascii_values(value: str) -> None:
    with pytest.raises(ValidationError):
        UserContext(id="u1", entitlements=[value])
    with pytest.raises(ValidationError):
        AccessPolicy(any_entitlements=[value])


def test_any_entitlements_are_or_and_other_dimensions_are_and() -> None:
    policy = AccessPolicy(
        allow_roles=["operator"],
        allow_groups=["trusted"],
        allow_tenants=["tenant-a"],
        any_entitlements=["workspace.ops.access", "workspace.sales.access"],
        required_attributes={"region": "cn"},
    )
    allowed = UserContext(
        id="u1",
        roles=["operator"],
        groups=["trusted"],
        entitlements=["workspace.sales.access"],
        attributes={"tenant_id": "tenant-a", "region": "cn"},
    )

    assert policy.allows(allowed)
    assert not policy.allows(allowed.model_copy(update={"entitlements": []}))
    assert not policy.allows(allowed.model_copy(update={"groups": []}))
    assert not policy.allows(allowed.model_copy(update={"attributes": {"tenant_id": "tenant-a"}}))


def test_deny_policy_still_takes_priority_over_entitlement() -> None:
    policy = AccessPolicy(
        deny_roles=["suspended"],
        any_entitlements=["workspace.ops.access"],
    )
    user = UserContext(id="u1", roles=["suspended"], entitlements=["workspace.ops.access"])
    assert not policy.allows(user)


def test_legacy_policy_without_entitlements_is_backward_compatible() -> None:
    policy = AccessPolicy(
        allow_roles=["operator"],
        allow_groups=["legacy"],
        allow_tenants=["tenant-a"],
        required_attributes={"region": "cn"},
    )
    assert policy.allows(
        UserContext(
            id="u1",
            roles=["operator"],
            groups=["legacy"],
            attributes={"tenant_id": "tenant-a", "region": "cn"},
        )
    )


def test_candidate_omits_policy_while_public_admin_view_retains_it() -> None:
    agent = _agent(AccessPolicy(any_entitlements=["workspace.ops.access"]))
    candidate = agent.to_candidate().model_dump()
    public = agent.to_public().model_dump()

    assert "access_policy" not in candidate
    assert "entitlements" not in candidate
    assert public["access_policy"]["any_entitlements"] == ["workspace.ops.access"]


def test_file_registry_round_trips_any_entitlements(tmp_path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "agents": [
                    _agent(AccessPolicy(any_entitlements=["workspace.ops.access"])).model_dump(
                        mode="json"
                    )
                ]
            }
        ),
        encoding="utf-8",
    )

    loaded = FileRegistrySource(str(path)).load_sync()
    assert loaded[0].access_policy.any_entitlements == ["workspace.ops.access"]
