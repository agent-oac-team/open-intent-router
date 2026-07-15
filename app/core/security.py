import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, Request

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError
from app.schemas.common import UserContext


@dataclass(frozen=True)
class MemoryActor:
    user_id: str
    tenant_id: str


@dataclass(frozen=True)
class AdminActor:
    actor_id: str


async def require_memory_actor(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_memory_identity_signature: str | None = Header(
        default=None, alias="X-Memory-Identity-Signature"
    ),
    settings: Settings = Depends(get_settings),
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
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_memory_identity_signature: str | None = Header(
        default=None, alias="X-Memory-Identity-Signature"
    ),
    settings: Settings = Depends(get_settings),
) -> MemoryActor | None:
    if x_user_id or x_tenant_id:
        return await require_memory_actor(
            request=request,
            x_user_id=x_user_id,
            x_tenant_id=x_tenant_id,
            x_memory_identity_signature=x_memory_identity_signature,
            settings=settings,
        )
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


def bind_trusted_user_context(user: UserContext, actor: MemoryActor) -> UserContext:
    attributes = {
        key: value for key, value in user.attributes.items() if key not in {"tenant", "tenant_id"}
    }
    attributes["tenant_id"] = actor.tenant_id
    return user.model_copy(
        deep=True,
        update={"id": actor.user_id, "attributes": attributes},
    )


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}
