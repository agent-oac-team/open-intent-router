"""OAC service identity verification and trusted OIR identity projection."""

from host_adapters.oac.identity.models import (
    HostAuthenticationError,
    HostAuthorizationError,
    SignedHostRequest,
    TrustedHostIdentity,
)
from host_adapters.oac.identity.policy import HostOperation, authorize_host_operation
from host_adapters.oac.identity.projection import OirIdentityProjection, project_oir_identity
from host_adapters.oac.identity.verifier import HostIdentityVerifier

__all__ = [
    "HostAuthenticationError",
    "HostAuthorizationError",
    "HostIdentityVerifier",
    "HostOperation",
    "OirIdentityProjection",
    "SignedHostRequest",
    "TrustedHostIdentity",
    "authorize_host_operation",
    "project_oir_identity",
]
