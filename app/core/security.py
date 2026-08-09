import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from fastapi import Depends, Header, Request
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError
from app.schemas.common import UserContext
from app.schemas.security import NativePrincipal


@dataclass(frozen=True)
class MemoryActor:
    user_id: str
    tenant_id: str


@dataclass(frozen=True)
class AdminActor:
    actor_id: str


async def require_native_principal(
    request: Request,
    x_oir_principal_envelope: str | None = Header(default=None, alias="X-OIR-Principal-Envelope"),
    x_oir_principal_signature: str | None = Header(default=None, alias="X-OIR-Principal-Signature"),
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_memory_identity_signature: str | None = Header(
        default=None, alias="X-Memory-Identity-Signature"
    ),
    settings: Settings = Depends(get_settings),
) -> NativePrincipal:
    envelope = (x_oir_principal_envelope or "").strip()
    if not envelope:
        if x_user_id or x_tenant_id or x_memory_identity_signature:
            actor = await _require_legacy_memory_actor(
                request=request,
                x_user_id=x_user_id,
                x_tenant_id=x_tenant_id,
                x_memory_identity_signature=x_memory_identity_signature,
                settings=settings,
            )
            return NativePrincipal(
                claims_version="oir-principal-v1",
                subject=actor.user_id,
                tenant=actor.tenant_id,
            )
        raise AuthenticationError("Authenticated Native Principal is required")

    if settings.native_principal_secret:
        expected = native_principal_signature(
            envelope=envelope,
            secret=settings.native_principal_secret,
        )
        if not x_oir_principal_signature or not hmac.compare_digest(
            x_oir_principal_signature, expected
        ):
            raise AuthenticationError("Invalid Native Principal signature")
    elif settings.app_env != "local" or not _is_loopback_client(request):
        raise AuthenticationError(
            "NATIVE_PRINCIPAL_SECRET is required outside local loopback access"
        )

    try:
        decoded = _decode_base64url(envelope)
        raw_claims = json.loads(decoded)
        principal = NativePrincipal.model_validate(raw_claims)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise AuthenticationError("Invalid Native Principal Envelope") from exc

    if encode_native_principal_envelope(principal) != envelope:
        raise AuthenticationError("Native Principal Envelope must use canonical encoding")
    return principal


async def require_memory_actor(
    principal: NativePrincipal = Depends(require_native_principal),
) -> MemoryActor:
    return MemoryActor(user_id=principal.subject, tenant_id=principal.tenant_id)


async def _require_legacy_memory_actor(
    *,
    request: Request,
    x_user_id: str | None,
    x_tenant_id: str | None,
    x_memory_identity_signature: str | None,
    settings: Settings,
) -> MemoryActor:
    user_id = (x_user_id or "").strip()
    tenant_id = (x_tenant_id or "").strip()
    if not user_id or not tenant_id or len(user_id) > 128 or len(tenant_id) > 128:
        raise AuthenticationError("Authenticated user and tenant identity are required")
    if settings.memory_identity_secret:
        expected = memory_identity_signature(
            user_id=user_id,
            tenant_id=tenant_id,
            secret=settings.memory_identity_secret,
        )
        if not x_memory_identity_signature or not hmac.compare_digest(
            x_memory_identity_signature, expected
        ):
            raise AuthenticationError("Invalid memory identity signature")
    elif settings.app_env != "local" or not _is_loopback_client(request):
        raise AuthenticationError(
            "MEMORY_IDENTITY_SECRET is required outside local loopback access"
        )
    return MemoryActor(user_id=user_id, tenant_id=tenant_id)


async def optional_memory_actor(
    request: Request,
    x_oir_principal_envelope: str | None = Header(default=None, alias="X-OIR-Principal-Envelope"),
    x_oir_principal_signature: str | None = Header(default=None, alias="X-OIR-Principal-Signature"),
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_memory_identity_signature: str | None = Header(
        default=None, alias="X-Memory-Identity-Signature"
    ),
    settings: Settings = Depends(get_settings),
) -> MemoryActor | None:
    if (
        x_oir_principal_envelope
        or x_oir_principal_signature
        or x_user_id
        or x_tenant_id
        or x_memory_identity_signature
    ):
        principal = await require_native_principal(
            request=request,
            x_oir_principal_envelope=x_oir_principal_envelope,
            x_oir_principal_signature=x_oir_principal_signature,
            x_user_id=x_user_id,
            x_tenant_id=x_tenant_id,
            x_memory_identity_signature=x_memory_identity_signature,
            settings=settings,
        )
        return MemoryActor(user_id=principal.subject, tenant_id=principal.tenant_id)
    if settings.app_env == "local" and _is_loopback_client(request):
        return None
    raise AuthenticationError("Authenticated user and tenant identity are required")


async def require_admin_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    settings: Settings = Depends(get_settings),
) -> AdminActor:
    expected = settings.admin_api_token
    if not expected:
        if settings.app_env == "local" and _is_loopback_client(request):
            return AdminActor(actor_id="local_admin")
        raise AuthenticationError("ADMIN_API_TOKEN is required outside local loopback access")

    token = x_admin_token
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if token != expected:
        raise AuthenticationError("Invalid admin token")
    return AdminActor(actor_id=settings.admin_actor_id)


def memory_identity_signature(*, user_id: str, tenant_id: str, secret: str) -> str:
    identity = f"{tenant_id}\x1f{user_id}".encode()
    return hmac.new(secret.encode(), identity, hashlib.sha256).hexdigest()


def encode_native_principal_envelope(principal: NativePrincipal) -> str:
    payload = json.dumps(
        principal.model_dump(by_alias=True, mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def native_principal_signature(*, envelope: str, secret: str) -> str:
    return hmac.new(secret.encode(), envelope.encode(), hashlib.sha256).hexdigest()


def bind_principal_user_context(user: UserContext, principal: NativePrincipal) -> UserContext:
    if user.id != principal.subject:
        raise AuthenticationError("Native Principal subject does not match request body")
    body_tenant = user.tenant_id
    if body_tenant is not None and body_tenant != principal.tenant_id:
        raise AuthenticationError("Native Principal tenant does not match request body")
    return principal.to_user_context()


def bind_trusted_user_context(user: UserContext, actor: MemoryActor) -> UserContext:
    return bind_principal_user_context(
        user,
        NativePrincipal(
            claims_version="oir-principal-v1",
            subject=actor.user_id,
            tenant=actor.tenant_id,
        ),
    )


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
