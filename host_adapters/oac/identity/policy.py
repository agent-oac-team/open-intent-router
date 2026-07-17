from typing import Literal

from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity

HostOperation = Literal["read_only", "route_stateful", "control_write", "runtime_write_own"]

CREDENTIAL_POLICIES: dict[str, tuple[str, frozenset[HostOperation]]] = {
    "oac_user": (
        "user",
        frozenset({"read_only", "route_stateful", "runtime_write_own"}),
    ),
    "oac_admin": ("user", frozenset({"read_only", "control_write"})),
    "coze_workflow": ("service", frozenset({"read_only"})),
}


def authorize_host_operation(identity: TrustedHostIdentity, operation: HostOperation) -> None:
    policy = CREDENTIAL_POLICIES.get(identity.credential_class)
    if policy is None:
        raise HostAuthorizationError
    principal_type, allowed_operations = policy
    if identity.principal_type != principal_type or operation not in allowed_operations:
        raise HostAuthorizationError
