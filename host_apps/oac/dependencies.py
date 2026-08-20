from functools import lru_cache

from fastapi import HTTPException, Request, status

from app.application import RegistrySnapshotRefreshApplicationPort
from app.core.config import get_settings
from app.db.session import create_session_factory
from app.dependencies import (
    build_router_service,
    get_delegated_run_service,
    get_event_service,
    get_execution_trace_service,
    get_external_execution_acceptance_store,
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
from app.runtime.catalog import RuntimeCatalogRuntime
from app.services.snapshot_routing_service import SnapshotRoutingService
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.cutover import CutoverGuard, FileCutoverAuditRepository
from host_adapters.oac.external_executor import OacExternalExecutor
from host_adapters.oac.identity import HostIdentityVerifier
from host_adapters.oac.identity.models import (
    HostAuthenticationError,
    SignedHostRequest,
    TrustedHostIdentity,
)
from host_adapters.oac.repositories.nonces import MemoryNonceStore
from host_adapters.oac.routing import OacLegacyRegistryRoutingAdapter
from host_apps.oac.config import get_oac_host_settings


def get_oac_adapter_application_ports(request: Request = None) -> OacAdapterApplicationPorts:
    """Compose request routing with the owning app's current health projection."""

    runtime_catalog = _runtime_catalog_runtime(request)
    routing = get_oac_legacy_registry_routing(runtime_catalog=runtime_catalog)
    return OacAdapterApplicationPorts(
        routing=routing,
        registry=get_registry_service(),
        events=get_event_service(),
        plans=get_plan_service(),
        delegated_runs=get_delegated_run_service(),
        turns=get_turn_service(),
        registry_snapshot_refresh=_registry_snapshot_refresh(request),
        plan_preflight=routing,
        external_execution=get_oac_external_execution_service(),
        execution_traces=get_execution_trace_service(),
        memory_management=get_memory_management_service(),
        memory_governance=get_memory_governance_service(),
    )


@lru_cache
def get_oac_external_executor() -> OacExternalExecutor:
    settings = get_oac_host_settings()
    host_ticket_secret = (
        settings.execution_ticket_secret.get_secret_value()
        if settings.execution_ticket_secret is not None
        else None
    )
    return OacExternalExecutor(
        supported_executor_refs=settings.supported_external_executor_refs,
        acceptance_store=get_external_execution_acceptance_store(),
        acceptance_fingerprint_secret=host_ticket_secret or get_execution_ticket_service().secret,
    )


def get_oac_snapshot_routing(
    *,
    runtime_catalog: RuntimeCatalogRuntime | None = None,
) -> SnapshotRoutingService:
    catalog = runtime_catalog.catalog if runtime_catalog is not None else None
    return SnapshotRoutingService(
        router_factory=lambda snapshot_runtime: build_router_service(
            snapshot_runtime=snapshot_runtime
        ),
        runtime_catalog=catalog,
        external_executor=get_oac_external_executor(),
        adapter_health_provider=(
            (lambda: runtime_catalog.health.unhealthy_adapter_keys)
            if runtime_catalog is not None
            else None
        ),
    )


def get_oac_legacy_registry_routing(
    *,
    runtime_catalog: RuntimeCatalogRuntime | None = None,
) -> OacLegacyRegistryRoutingAdapter:
    return OacLegacyRegistryRoutingAdapter(
        registry=get_registry_service(),
        snapshot_routing=get_oac_snapshot_routing(runtime_catalog=runtime_catalog),
    )


def _runtime_catalog_runtime(request: Request | None) -> RuntimeCatalogRuntime | None:
    if request is None:
        return None
    runtime = getattr(request.app.state, "runtime_catalog_runtime", None)
    return runtime if isinstance(runtime, RuntimeCatalogRuntime) else None


def _registry_snapshot_refresh(
    request: Request | None,
) -> RegistrySnapshotRefreshApplicationPort | None:
    if request is None:
        return None
    refresh = getattr(request.app.state, "runtime_readiness_runtime", None)
    return refresh if isinstance(refresh, RegistrySnapshotRefreshApplicationPort) else None


@lru_cache
def get_oac_external_execution_service():
    from app.services.external_execution_service import ExternalExecutionService

    return ExternalExecutionService(
        external_executor=get_oac_external_executor(),
        delegated_runs=get_delegated_run_service(),
        tickets=get_execution_ticket_service(),
        ticket_ttl_seconds=get_oac_host_settings().execution_ticket_ttl_seconds,
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
