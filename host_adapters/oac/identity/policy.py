from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity
from host_adapters.oac.identity.observability import HOST_SIGNATURE_METRICS
from host_adapters.oac.identity.policy_types import HostOperation
from host_adapters.oac.identity.profiles import CREDENTIAL_PROFILES


def authorize_host_operation(identity: TrustedHostIdentity, operation: HostOperation) -> None:
    profile = CREDENTIAL_PROFILES.get(identity.credential_class)
    if profile is None:
        _record(identity, "authorization_failed")
        raise HostAuthorizationError
    if (
        identity.principal_type != profile.principal_type
        or operation not in profile.allowed_operations
    ):
        _record(identity, "authorization_failed")
        raise HostAuthorizationError
    _record(identity, "verified")


def _record(identity: TrustedHostIdentity, outcome: str) -> None:
    if not identity.signature_version:
        return
    HOST_SIGNATURE_METRICS.record(
        version=identity.signature_version,
        credential_class=identity.credential_class,
        operation=identity.request_operation,
        outcome=outcome,
    )
