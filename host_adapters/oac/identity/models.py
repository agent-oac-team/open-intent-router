from dataclasses import dataclass


class HostAuthenticationError(ValueError):
    def __init__(self) -> None:
        super().__init__("host_authentication_failed")


class HostAuthorizationError(PermissionError):
    def __init__(self) -> None:
        super().__init__("host_operation_forbidden")


@dataclass(frozen=True)
class SignedHostRequest:
    method: str
    path: str
    query: str
    body: bytes
    key_id: str
    audience: str
    timestamp: str
    nonce: str
    content_sha256: str
    principal_type: str
    user_id: str
    groups: str
    credential_class: str
    signature: str


@dataclass(frozen=True)
class TrustedHostIdentity:
    key_id: str
    audience: str
    principal_type: str
    tenant_id: str
    user_id: str
    groups: tuple[str, ...]
    credential_class: str
