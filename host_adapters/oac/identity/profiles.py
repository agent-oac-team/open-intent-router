from dataclasses import dataclass

from host_adapters.oac.identity.policy_types import HostOperation


@dataclass(frozen=True)
class CredentialProfile:
    credential_class: str
    principal_type: str
    claims_version: str
    policy_version: str
    roles_required: bool
    active_bundle_required: bool
    allowed_operations: frozenset[HostOperation]


CREDENTIAL_PROFILES: dict[str, CredentialProfile] = {
    "oac_user": CredentialProfile(
        credential_class="oac_user",
        principal_type="user",
        claims_version="oac-principal-v1",
        policy_version="oac-authz-v1",
        roles_required=True,
        active_bundle_required=True,
        allowed_operations=frozenset({"read_only", "route_stateful", "runtime_write_own"}),
    ),
    "oac_admin": CredentialProfile(
        credential_class="oac_admin",
        principal_type="user",
        claims_version="oac-admin-principal-v1",
        policy_version="oac-control-v1",
        roles_required=False,
        active_bundle_required=False,
        allowed_operations=frozenset({"read_only", "control_write"}),
    ),
    "coze_workflow": CredentialProfile(
        credential_class="coze_workflow",
        principal_type="service",
        claims_version="oac-service-principal-v1",
        policy_version="oac-readonly-v1",
        roles_required=False,
        active_bundle_required=False,
        allowed_operations=frozenset({"read_only"}),
    ),
}


def credential_profile_catalog_healthy() -> bool:
    expected = {"oac_user", "oac_admin", "coze_workflow"}
    return set(CREDENTIAL_PROFILES) == expected and all(
        profile.credential_class == credential_class
        and profile.claims_version
        and profile.policy_version
        and profile.allowed_operations
        for credential_class, profile in CREDENTIAL_PROFILES.items()
    )
