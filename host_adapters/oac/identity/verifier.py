import base64
import hashlib
import hmac
import re
import time
from collections.abc import Callable

from host_adapters.oac.identity.canonical import canonicalize_host_request
from host_adapters.oac.identity.models import (
    HostAuthenticationError,
    SignedHostRequest,
    TrustedHostIdentity,
)
from host_adapters.oac.identity.observability import (
    HOST_SIGNATURE_METRICS,
    HostSignatureMetrics,
    classify_host_request,
)
from host_adapters.oac.identity.profiles import CREDENTIAL_PROFILES
from host_adapters.oac.repositories.nonces import NonceStore

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE = re.compile(r"^v2=([0-9a-f]{64})$")
SAFE_IDENTITY_VALUE = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")


class HostIdentityVerifier:
    def __init__(
        self,
        *,
        audience: str,
        tenant_id: str,
        keys: dict[str, str],
        key_credential_classes: dict[str, frozenset[str]],
        nonce_store: NonceStore,
        metrics: HostSignatureMetrics | None = None,
        now: Callable[[], int] | None = None,
        max_clock_skew_seconds: int = 60,
        nonce_ttl_seconds: int = 120,
    ) -> None:
        self.audience = audience
        self.tenant_id = tenant_id
        self.keys = keys
        self.key_credential_classes = key_credential_classes
        self.nonce_store = nonce_store
        self.metrics = metrics or HOST_SIGNATURE_METRICS
        self.now = now or (lambda: int(time.time()))
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self.nonce_ttl_seconds = nonce_ttl_seconds

    async def verify(self, request: SignedHostRequest) -> TrustedHostIdentity:
        operation = classify_host_request(request.method, request.path)
        try:
            timestamp = int(request.timestamp)
            groups = tuple(
                sorted({item.strip() for item in request.groups.split(",") if item.strip()})
            )
            roles = tuple(
                sorted({item.strip() for item in request.roles.split(",") if item.strip()})
            )
            secret = self.keys[request.key_id]
            signature_match = SIGNATURE.fullmatch(request.signature)
            if (
                request.audience != self.audience
                or abs(self.now() - timestamp) > self.max_clock_skew_seconds
                or not SHA256_HEX.fullmatch(request.content_sha256)
                or hashlib.sha256(request.body).hexdigest() != request.content_sha256
                or not signature_match
                or not self._valid_identity(request, groups, roles)
                or request.credential_class
                not in self.key_credential_classes.get(request.key_id, frozenset())
                or not _nonce_has_minimum_entropy(request.nonce)
            ):
                raise HostAuthenticationError
            canonical = canonicalize_host_request(request, tenant_id=self.tenant_id)
            expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature_match.group(1), expected):
                raise HostAuthenticationError
            if not await self.nonce_store.consume(
                key_id=request.key_id,
                nonce=request.nonce,
                now=self.now(),
                ttl_seconds=self.nonce_ttl_seconds,
            ):
                raise HostAuthenticationError
        except (HostAuthenticationError, KeyError, TypeError, ValueError) as exc:
            self.metrics.record(
                version=(
                    "v2"
                    if request.signature.startswith("v2=")
                    else "v1"
                    if request.signature.startswith("v1=")
                    else "unknown"
                ),
                credential_class=request.credential_class,
                operation=operation,
                outcome="authentication_failed",
            )
            raise HostAuthenticationError from exc

        return TrustedHostIdentity(
            key_id=request.key_id,
            audience=request.audience,
            principal_type=request.principal_type,
            tenant_id=self.tenant_id,
            user_id=request.user_id,
            groups=groups,
            credential_class=request.credential_class,
            claims_version=request.claims_version,
            roles=roles,
            active_bundle_id=request.active_bundle_id,
            policy_version=request.policy_version,
            signature_version="v2",
            request_operation=operation,
        )

    def _valid_identity(
        self,
        request: SignedHostRequest,
        groups: tuple[str, ...],
        roles: tuple[str, ...],
    ) -> bool:
        profile = CREDENTIAL_PROFILES.get(request.credential_class)
        if profile is None:
            return False
        common = (
            request.principal_type in {"user", "service"}
            and bool(SAFE_IDENTITY_VALUE.fullmatch(request.user_id))
            and bool(SAFE_IDENTITY_VALUE.fullmatch(request.credential_class))
            and all(SAFE_IDENTITY_VALUE.fullmatch(group) for group in groups)
            and all(SAFE_IDENTITY_VALUE.fullmatch(role) for role in roles)
        )
        if not common:
            return False
        return (
            request.principal_type == profile.principal_type
            and request.claims_version == profile.claims_version
            and request.policy_version == profile.policy_version
            and not groups
            and bool(roles) == profile.roles_required
            and bool(request.active_bundle_id) == profile.active_bundle_required
            and (
                not request.active_bundle_id
                or bool(SAFE_IDENTITY_VALUE.fullmatch(request.active_bundle_id))
            )
        )


def _nonce_has_minimum_entropy(nonce: str) -> bool:
    try:
        padding = "=" * (-len(nonce) % 4)
        decoded = base64.b64decode(nonce + padding, altchars=b"-_", validate=True)
    except ValueError:
        return False
    return len(decoded) >= 16
