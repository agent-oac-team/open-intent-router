from dataclasses import dataclass
from typing import Any

from app.core.security import memory_identity_signature
from app.schemas.common import UserContext
from host_adapters.oac.identity.models import TrustedHostIdentity


@dataclass(frozen=True)
class OirIdentityProjection:
    user: UserContext
    signature: str


def project_oir_identity(
    identity: TrustedHostIdentity,
    *,
    untrusted_attributes: dict[str, Any] | None,
    allowed_attribute_keys: frozenset[str],
    signing_secret: str,
) -> OirIdentityProjection:
    source_attributes = untrusted_attributes or {}
    attributes = {
        key: value
        for key, value in source_attributes.items()
        if key in allowed_attribute_keys and key not in {"tenant", "tenant_id"}
    }
    attributes["tenant_id"] = identity.tenant_id
    user = UserContext(
        id=identity.user_id,
        groups=list(identity.groups),
        attributes=attributes,
    )
    return OirIdentityProjection(
        user=user,
        signature=memory_identity_signature(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            secret=signing_secret,
        ),
    )
