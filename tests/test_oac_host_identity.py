import hashlib
import hmac
from dataclasses import replace

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
from host_adapters.oac.identity.observability import HOST_SIGNATURE_METRICS
from host_adapters.oac.repositories.nonces import MemoryNonceStore


async def test_host_identity_verifier_rejects_retired_v1() -> None:
    HOST_SIGNATURE_METRICS.clear()
    request = replace(_v2_request(), signature="v1=" + "0" * 64)
    verifier = HostIdentityVerifier(
        audience="oac-oir-adapter-local",
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )
    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)
    assert HOST_SIGNATURE_METRICS.snapshot() == [
        {
            "signature_version": "v1",
            "credential_class": "oac_user",
            "operation": "route",
            "outcome": "authentication_failed",
            "count": 1,
        }
    ]


def _v2_request(**updates) -> SignedHostRequest:
    body = (
        '{"request_id":"req-v2","session_id":"session-v2","user_id":"42",'
        '"user_tags":["运营版"],"source":"central_chat","user_query":"排期"}'
    ).encode()
    values = {
        "method": "POST",
        "path": "/api/v1/central/route",
        "query": "",
        "body": body,
        "key_id": "kid-v2",
        "audience": "oac-oir-adapter-local",
        "timestamp": "1784170800",
        "nonce": "bm9uY2UtZml4dHVyZS12Mi0wMDE",
        "content_sha256": "63f46b5783ec77106d58c9b160388edfffb2dcbd15017ee9497f8f220375148e",
        "principal_type": "user",
        "user_id": "42",
        "groups": "",
        "credential_class": "oac_user",
        "signature": "v2=4e8aa5c32ea9b7bdc898ee21239fd6af1f5df8abe1b43afd5926dd1291f9c725",
        "claims_version": "oac-principal-v1",
        "roles": "operator",
        "active_bundle_id": "oac-operations",
        "policy_version": "oac-authz-v1",
    }
    values.update(updates)
    return SignedHostRequest(**values)


def _profile_request(credential_class: str, **updates) -> tuple[str, SignedHostRequest]:
    if credential_class == "oac_admin":
        secret = "admin-secret"
        values = {
            "method": "GET",
            "path": "/api/v1/admin/agent-registry",
            "query": "enabled_only=true",
            "body": b"",
            "key_id": "admin-key",
            "nonce": "bm9uY2UtYWRtaW4tZml4dHVyZS0wMDE",
            "principal_type": "user",
            "user_id": "42",
            "claims_version": "oac-admin-principal-v1",
            "policy_version": "oac-control-v1",
        }
    else:
        secret = "coze-secret"
        values = {
            "method": "POST",
            "path": "/api/v1/knowledge/read",
            "query": "",
            "body": b'{"asset_id":"01"}',
            "key_id": "coze-key",
            "nonce": "bm9uY2UtY296ZS1maXh0dXJlLTAwMQ",
            "principal_type": "service",
            "user_id": "coze-workflow",
            "claims_version": "oac-service-principal-v1",
            "policy_version": "oac-readonly-v1",
        }
    values.update(
        {
            "audience": "oac-oir-adapter-local",
            "timestamp": "1784170800",
            "groups": "",
            "credential_class": credential_class,
            "signature": "v2=" + "0" * 64,
            "roles": "",
            "active_bundle_id": "",
        }
    )
    values.update(updates)
    body = values["body"]
    request = SignedHostRequest(
        **values,
        content_sha256=hashlib.sha256(body).hexdigest(),
    )
    canonical = canonicalize_host_request(request, tenant_id="oac")
    signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return secret, replace(request, signature=f"v2={signature}")


async def test_host_identity_v2_accepts_cross_language_frozen_vector() -> None:
    request = _v2_request()
    assert canonicalize_host_request(request, tenant_id="oac") == "\n".join(
        [
            "OIR-HOST-V2",
            "kid-v2",
            "oac-oir-adapter-local",
            "1784170800",
            "bm9uY2UtZml4dHVyZS12Mi0wMDE",
            "POST",
            "/api/v1/central/route",
            "",
            "63f46b5783ec77106d58c9b160388edfffb2dcbd15017ee9497f8f220375148e",
            "user",
            "oac",
            "42",
            "operator",
            "",
            "oac-operations",
            "oac-principal-v1",
            "oac-authz-v1",
            "oac_user",
        ]
    )
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    identity = await verifier.verify(request)
    assert identity.claims_version == "oac-principal-v1"
    assert identity.roles == ("operator",)
    assert identity.active_bundle_id == "oac-operations"
    assert identity.policy_version == "oac-authz-v1"
    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


@pytest.mark.parametrize(
    ("credential_class", "expected_signature"),
    [
        ("oac_admin", "v2=28dfd64140acc1c156d4e3ebfaec82eecbcf8dc977b7aa7708dd4f7b04633345"),
        ("coze_workflow", "v2=4d3895a60d0fac3fd0fdc68edd2e9d9b7851d9f4fb48d23b0eb4b83d6f4ae8a0"),
    ],
)
async def test_admin_and_coze_v2_cross_language_frozen_vectors(
    credential_class: str, expected_signature: str
) -> None:
    secret, request = _profile_request(credential_class)
    assert request.signature == expected_signature
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: secret},
        key_credential_classes={request.key_id: frozenset({credential_class})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )
    identity = await verifier.verify(request)
    assert identity.credential_class == credential_class
    assert identity.groups == ()
    assert identity.roles == ()
    assert identity.active_bundle_id == ""
    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


@pytest.mark.parametrize("credential_class", ["oac_admin", "coze_workflow"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("roles", "admin"),
        ("groups", "admin"),
        ("active_bundle_id", "oac-operations"),
        ("principal_type", "service"),
        ("claims_version", "unknown-v1"),
        ("policy_version", "unknown-v1"),
    ],
)
async def test_admin_and_coze_profiles_reject_extra_or_mismatched_claims(
    credential_class: str, field: str, value: str
) -> None:
    if credential_class == "coze_workflow" and field == "principal_type":
        value = "user"
    secret, request = _profile_request(credential_class, **{field: value})
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: secret},
        key_credential_classes={request.key_id: frozenset({credential_class})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )
    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("claims_version", "unknown-v1"),
        ("policy_version", "unknown-policy"),
        ("active_bundle_id", ""),
        ("roles", ""),
        ("signature", "v3=" + "0" * 64),
    ],
)
async def test_host_identity_v2_unknown_or_incomplete_claims_fail_closed(
    field: str, value: str
) -> None:
    request = _v2_request(**{field: value})
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )
    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


async def test_host_identity_verifier_rejects_tampered_body_with_uniform_error() -> None:
    request = _v2_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(
            SignedHostRequest(**{**request.__dict__, "body": b'{"user_id":"forged"}'})
        )


async def test_identity_projection_overwrites_body_identity_and_filters_attributes() -> None:
    request = _v2_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
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
    assert projection.user.groups == []
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
    request = _v2_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"coze_workflow"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


async def test_expired_host_signature_is_rejected() -> None:
    request = _v2_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp) + 61,
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(request)


async def test_signed_user_id_cannot_be_forged() -> None:
    request = _v2_request()
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )

    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(replace(request, user_id="forged-user"))


async def test_validly_signed_user_groups_are_rejected() -> None:
    request = _v2_request()
    changed = replace(request, groups="admin,operator,superuser")
    canonical = canonicalize_host_request(changed, tenant_id="oac")
    signature = hmac.new(b"fixture-v2-secret", canonical.encode(), hashlib.sha256).hexdigest()
    changed = replace(changed, signature=f"v2={signature}")
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: "fixture-v2-secret"},
        key_credential_classes={request.key_id: frozenset({"oac_user"})},
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


async def test_signature_metrics_record_one_terminal_outcome_per_request() -> None:
    HOST_SIGNATURE_METRICS.clear()
    secret, request = _profile_request("oac_admin")
    verifier = HostIdentityVerifier(
        audience=request.audience,
        tenant_id="oac",
        keys={request.key_id: secret},
        key_credential_classes={request.key_id: frozenset({"oac_admin"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(request.timestamp),
    )
    identity = await verifier.verify(request)
    assert HOST_SIGNATURE_METRICS.snapshot() == []
    authorize_host_operation(identity, "control_write")
    assert HOST_SIGNATURE_METRICS.snapshot() == [
        {
            "signature_version": "v2",
            "credential_class": "oac_admin",
            "operation": "registry",
            "outcome": "verified",
            "count": 1,
        }
    ]

    HOST_SIGNATURE_METRICS.clear()
    with pytest.raises(HostAuthenticationError, match="host_authentication_failed"):
        await verifier.verify(replace(request, signature="v2=" + "0" * 64))
    assert HOST_SIGNATURE_METRICS.snapshot()[0]["outcome"] == "authentication_failed"

    HOST_SIGNATURE_METRICS.clear()
    coze_secret, coze_request = _profile_request("coze_workflow")
    coze_identity = await HostIdentityVerifier(
        audience=coze_request.audience,
        tenant_id="oac",
        keys={coze_request.key_id: coze_secret},
        key_credential_classes={coze_request.key_id: frozenset({"coze_workflow"})},
        nonce_store=MemoryNonceStore(),
        now=lambda: int(coze_request.timestamp),
    ).verify(coze_request)
    with pytest.raises(HostAuthorizationError, match="host_operation_forbidden"):
        authorize_host_operation(coze_identity, "control_write")
    assert HOST_SIGNATURE_METRICS.snapshot()[0]["outcome"] == "authorization_failed"
