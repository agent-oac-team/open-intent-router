from functools import lru_cache

from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.db.session import create_session_factory
from app.dependencies import (
    build_router_service,
    get_delegated_run_service,
    get_event_service,
    get_execution_trace_service,
    get_memory_governance_service,
    get_memory_management_service,
    get_plan_service,
    get_registry_service,
    get_turn_service,
)
from app.dependencies import (
    get_execution_ticket_service as get_execution_ticket_service,
)
from app.repositories.registry_audit import (
    DatabaseRegistryAuditStore,
    MemoryRegistryAuditStore,
    RegistryAuditStore,
)
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.cutover import CutoverGuard, FileCutoverAuditRepository
from host_adapters.oac.identity import HostIdentityVerifier
from host_adapters.oac.identity.models import (
    HostAuthenticationError,
    SignedHostRequest,
    TrustedHostIdentity,
)
from host_adapters.oac.repositories.nonces import MemoryNonceStore
from host_apps.oac.config import get_oac_host_settings


@lru_cache
def get_oac_adapter_application_ports() -> OacAdapterApplicationPorts:
    return OacAdapterApplicationPorts(
        routing=build_router_service(),
        registry=get_registry_service(),
        events=get_event_service(),
        plans=get_plan_service(),
        delegated_runs=get_delegated_run_service(),
        turns=get_turn_service(),
        execution_traces=get_execution_trace_service(),
        memory_management=get_memory_management_service(),
        memory_governance=get_memory_governance_service(),
    )


@lru_cache
def get_host_nonce_store() -> MemoryNonceStore:
    return MemoryNonceStore()


@lru_cache
def get_host_identity_verifier() -> HostIdentityVerifier:
    settings = get_oac_host_settings()
    keys = {}
    key_credential_classes: dict[str, frozenset[str]] = {}
    if settings.identity_current_key_id and settings.identity_current_key:
        keys[settings.identity_current_key_id] = settings.identity_current_key.get_secret_value()
        key_credential_classes[settings.identity_current_key_id] = frozenset({"oac_user"})
    if settings.identity_previous_key_id and settings.identity_previous_key:
        keys[settings.identity_previous_key_id] = settings.identity_previous_key.get_secret_value()
        key_credential_classes[settings.identity_previous_key_id] = frozenset({"oac_user"})
    if settings.oac_admin_key_id and settings.oac_admin_credential:
        keys[settings.oac_admin_key_id] = settings.oac_admin_credential.get_secret_value()
        key_credential_classes[settings.oac_admin_key_id] = frozenset({"oac_admin"})
    if settings.coze_workflow_key_id and settings.coze_workflow_credential:
        keys[settings.coze_workflow_key_id] = settings.coze_workflow_credential.get_secret_value()
        key_credential_classes[settings.coze_workflow_key_id] = frozenset({"coze_workflow"})
    return HostIdentityVerifier(
        audience=settings.identity_audience,
        tenant_id=settings.tenant_id,
        keys=keys,
        key_credential_classes=key_credential_classes,
        nonce_store=get_host_nonce_store(),
    )


async def get_trusted_host_identity(request: Request) -> TrustedHostIdentity:
    body = request.scope.get("oac_host_wire_body")
    if body is None:
        body = await request.body()
    headers = request.headers
    signed = SignedHostRequest(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        body=body,
        key_id=headers.get("X-OIR-Host-Key-Id", ""),
        audience=headers.get("X-OIR-Host-Audience", ""),
        timestamp=headers.get("X-OIR-Host-Timestamp", ""),
        nonce=headers.get("X-OIR-Host-Nonce", ""),
        content_sha256=headers.get("X-OIR-Host-Content-SHA256", ""),
        principal_type=headers.get("X-OIR-Host-Principal-Type", ""),
        user_id=headers.get("X-OIR-Host-User-Id", ""),
        groups=headers.get("X-OIR-Host-Groups", ""),
        credential_class=headers.get("X-OIR-Host-Credential-Class", ""),
        signature=headers.get("X-OIR-Host-Signature", ""),
        claims_version=headers.get("X-OIR-Host-Claims-Version", ""),
        roles=headers.get("X-OIR-Host-Roles", ""),
        active_bundle_id=headers.get("X-OIR-Host-Active-Bundle-Id", ""),
        policy_version=headers.get("X-OIR-Host-Policy-Version", ""),
    )
    try:
        return await get_host_identity_verifier().verify(signed)
    except HostAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="host_authentication_failed",
        ) from exc


@lru_cache
def get_registry_audit_store() -> RegistryAuditStore:
    core = get_settings()
    if core.storage_backend == "database":
        return DatabaseRegistryAuditStore(create_session_factory(core))
    return MemoryRegistryAuditStore()


@lru_cache
def get_cutover_guard() -> CutoverGuard:
    settings = get_oac_host_settings()
    return CutoverGuard(
        watermark=settings.cutover_watermark_at,
        repository=FileCutoverAuditRepository(settings.cutover_audit_path),
    )
