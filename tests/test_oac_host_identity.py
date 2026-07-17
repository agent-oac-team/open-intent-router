import hashlib
import hmac
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.security import memory_identity_signature
from host_adapters.oac.identity import (
    HostAuthenticationError,
    HostAuthorizationError,
    HostIdentityVerifier,
    HostOperation,
    SignedHostRequest,
    TrustedHostIdentity,
    authorize_host_operation,
    project_oir_identity,
)
from host_adapters.oac.identity.canonical import canonicalize_host_request
from host_adapters.oac.repositories.nonces import MemoryNonceStore

CONTRACT_PATH = Path("tests/contract/oac_irs/identity/v1/contract.json")


def _vector_request() -> tuple[dict, SignedHostRequest]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    vector = contract["test_vector"]
    source = vector["request"]
    identity = vector["identity"]
    return vector, SignedHostRequest(
        method=source["method"],
        path=source["path"],
        query=source["query"],
        body=source["body"].encode(),
        key_id=identity["key_id"],
        audience=identity["audience"],
        timestamp=str(identity["timestamp"]),
        nonce=identity["nonce"],
        content_sha256=vector["content_sha256"],
        principal_type=identity["principal_type"],
        user_id=identity["user_id"],
        groups=",".join(identity["groups"]),
        credential_class=identity["credential_class"],
        signature=vector["signature"],
    )


async def test_host_identity_verifier_accepts_frozen_vector_and_rejects_replay() -> None:
    vector, request = _vector_request()
    verifier = HostIdentityVerifier(
        audience="oac-oir-adapter-local",
        tenant_id="oac",
        keys={request.key_id: vector["secret"]},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        allowed_groups=frozenset({"operator", "admin"}),
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    identity = await verifier.verify(request)

    assert identity.user_id == "42"
    assert identity.tenant_id == "oac"
    assert identity.groups == ("admin", "operator")
    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


async def test_host_identity_verifier_rejects_tampered_body_with_uniform_error() -> None:
    vector, request = _vector_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: vector["secret"]},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        allowed_groups=frozenset({"operator", "admin"}),
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(
            SignedHostRequest(**{**request.__dict__, "body": b'{"user_id":"forged"}'})
        )


async def test_identity_projection_overwrites_body_identity_and_filters_attributes() -> None:
    vector, request = _vector_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: vector["secret"]},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        allowed_groups=frozenset({"operator", "admin"}),
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )
    identity = await verifier.verify(request)

    projection = project_oir_identity(
        identity,
        untrusted_attributes={
            "tenant_id": "forged-tenant",
            "region": "east",
            "unapproved": "drop-me",
            "user_tags": ["admin"],
        },
        allowed_attribute_keys=frozenset({"region", "tenant_id"}),
        signing_secret="fixture-oir-identity-secret",
    )

    assert projection.user.id == "42"
    assert projection.user.groups == ["admin", "operator"]
    assert projection.user.attributes == {"region": "east", "tenant_id": "oac"}
    assert projection.signature == memory_identity_signature(
        user_id="42",
        tenant_id="oac",
        secret="fixture-oir-identity-secret",
    )


@pytest.mark.parametrize(
    ("credential_class", "principal_type", "allowed"),
    [
        ("oac_user", "user", {"read_only", "route_stateful", "runtime_write_own"}),
        ("oac_admin", "user", {"read_only", "control_write"}),
        ("coze_workflow", "service", {"read_only"}),
    ],
)
def test_credential_classes_enforce_minimum_operation_policy(
    credential_class: str,
    principal_type: str,
    allowed: set[HostOperation],
) -> None:
    identity = TrustedHostIdentity(
        key_id=f"{credential_class}-key",
        audience="oac-oir-adapter-test",
        principal_type=principal_type,
        tenant_id="oac",
        user_id="principal-1",
        groups=(),
        credential_class=credential_class,
    )
    operations: set[HostOperation] = {
        "read_only",
        "route_stateful",
        "control_write",
        "runtime_write_own",
    }

    for operation in operations:
        if operation in allowed:
            authorize_host_operation(identity, operation)
        else:
            with pytest.raises(HostAuthorizationError, match="host_operation_forbidden"):
                authorize_host_operation(identity, operation)


async def test_signing_key_cannot_assert_another_credential_class() -> None:
    vector, request = _vector_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: vector["secret"]},
        key_credential_classes={request.key_id: frozenset({"coze_workflow"})},
        allowed_groups=frozenset({"operator", "admin"}),
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


async def test_expired_host_signature_is_rejected() -> None:
    vector, request = _vector_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: vector["secret"]},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        allowed_groups=frozenset({"operator", "admin"}),
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp) + 61,
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


async def test_signed_user_id_cannot_be_forged() -> None:
    vector, request = _vector_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: vector["secret"]},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        allowed_groups=frozenset({"operator", "admin"}),
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(replace(request, user_id="forged-user"))


async def test_validly_signed_group_outside_allowlist_is_rejected() -> None:
    vector, request = _vector_request()
    changed = replace(request, groups="admin,operator,superuser")
    canonical = canonicalize_host_request(changed, tenant_id="oac")
    signature = hmac.new(vector["secret"].encode(), canonical.encode(), hashlib.sha256).hexdigest()
    changed = replace(changed, signature=f"v1={signature}")
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: vector["secret"]},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        allowed_groups=frozenset({"operator", "admin"}),
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(changed)


def test_coze_workflow_cannot_perform_control_or_runtime_writes() -> None:
    identity = TrustedHostIdentity(
        key_id="coze-key",
        audience="oac-oir-adapter-test",
        principal_type="service",
        tenant_id="oac",
        user_id="coze-workflow",
        groups=(),
        credential_class="coze_workflow",
    )

    for operation in ("control_write", "runtime_write_own", "route_stateful"):
        with pytest.raises(HostAuthorizationError, match="host_operation_forbidden"):
            authorize_host_operation(identity, operation)
