"""Request providers for the OAC Host Container owned by its lifespan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.runtime.application import ApplicationContainer
from app.runtime.catalog import RuntimeCatalogRuntime
from app.services.execution_ticket_service import ExecutionTicketService
from app.services.snapshot_routing_service import SnapshotRoutingService
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.cutover import CutoverGuard, FileCutoverAuditRepository
from host_adapters.oac.identity import HostIdentityVerifier
from host_adapters.oac.identity.models import (
    HostAuthenticationError,
    SignedHostRequest,
    TrustedHostIdentity,
)
from host_adapters.oac.repositories.nonces import MemoryNonceStore
from host_adapters.oac.routing import OacLegacyRegistryRoutingAdapter
from host_apps.oac.capabilities import OacHostCapabilityProvider
from host_apps.oac.config import OacHostProfile


@dataclass(frozen=True, slots=True)
class OacApplicationContainer:
    """Host capabilities composed only after the full Core Container is ready."""

    ports: OacAdapterApplicationPorts
    identity_verifier: HostIdentityVerifier
    cutover_guard: CutoverGuard
    capability_provider: OacHostCapabilityProvider


def build_oac_application_container(
    *,
    profile: OacHostProfile,
    core_container: ApplicationContainer,
    runtime_catalog_runtime: RuntimeCatalogRuntime,
    registry_snapshot_refresh,
) -> OacApplicationContainer:
    services = core_container.services
    if services is None:
        raise RuntimeError("OAC Host requires a complete Core Application Container")
    external_executor = services.external_executor
    snapshot_routing = SnapshotRoutingService(
        router_factory=services.router_for_snapshot,
        runtime_catalog=core_container.runtime_catalog,
        external_executor=external_executor,
        adapter_health_provider=lambda: runtime_catalog_runtime.health.unhealthy_adapter_keys,
    )
    routing = OacLegacyRegistryRoutingAdapter(
        registry=core_container.registry,
        snapshot_routing=snapshot_routing,
    )
    ports = OacAdapterApplicationPorts(
        routing=routing,
        registry=core_container.registry,
        events=services.event_service,
        plans=services.plan_service,
        delegated_runs=services.delegated_run_service,
        turns=services.turn_service,
        invocation=services.invocation_service,
        registry_snapshot_refresh=registry_snapshot_refresh,
        plan_preflight=routing,
        external_execution=services.external_execution_service,
        execution_traces=services.execution_trace_service,
        memory_management=services.memory_management_service,
        memory_governance=services.memory_governance_service,
    )
    host = profile.host
    identity_verifier = HostIdentityVerifier(
        audience=host.identity_audience,
        tenant_id=host.tenant_id,
        keys=_identity_keys(host),
        key_credential_classes=_identity_key_credential_classes(host),
        nonce_store=MemoryNonceStore(),
    )
    cutover_guard = CutoverGuard(
        watermark=host.cutover_watermark_at,
        repository=FileCutoverAuditRepository(host.cutover_audit_path),
    )
    return OacApplicationContainer(
        ports=ports,
        identity_verifier=identity_verifier,
        cutover_guard=cutover_guard,
        capability_provider=OacHostCapabilityProvider(
            core=profile.core,
            host=host,
            ports=ports,
            memory_policy=services.memory_runtime_policy,
        ),
    )


def get_oac_adapter_application_ports(request: Request) -> OacAdapterApplicationPorts:
    return _host_container(request).ports


def get_execution_ticket_service(request: Request) -> ExecutionTicketService:
    services = _core_container(request).services
    if services is None:
        raise _runtime_unavailable()
    return services.execution_ticket_service


def get_cutover_guard(request: Request) -> CutoverGuard:
    return _host_container(request).cutover_guard


def get_host_identity_verifier(request: Request) -> HostIdentityVerifier:
    container = getattr(request.app.state, "oac_application_container", None)
    if isinstance(container, OacApplicationContainer):
        return container.identity_verifier
    raise _runtime_unavailable()


async def get_trusted_host_identity(
    request: Request,
    verifier: Annotated[HostIdentityVerifier, Depends(get_host_identity_verifier)],
) -> TrustedHostIdentity:
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
        return await verifier.verify(signed)
    except HostAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="host_authentication_failed",
        ) from exc


def _core_container(request: Request) -> ApplicationContainer:
    view = getattr(request.app.state, "application_runtime_view", None)
    if view is None:
        raise _runtime_unavailable()
    try:
        return view.require_container()
    except Exception as exc:
        raise _runtime_unavailable() from exc


def _host_container(request: Request) -> OacApplicationContainer:
    container = getattr(request.app.state, "oac_application_container", None)
    if not isinstance(container, OacApplicationContainer):
        raise _runtime_unavailable()
    return container


def _runtime_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="application_runtime_unavailable",
    )


def _identity_keys(host) -> dict[str, str]:
    keys: dict[str, str] = {}
    if host.identity_current_key_id and host.identity_current_key:
        keys[host.identity_current_key_id] = host.identity_current_key.get_secret_value()
    if host.identity_previous_key_id and host.identity_previous_key:
        keys[host.identity_previous_key_id] = host.identity_previous_key.get_secret_value()
    if host.oac_admin_key_id and host.oac_admin_credential:
        keys[host.oac_admin_key_id] = host.oac_admin_credential.get_secret_value()
    if host.coze_workflow_key_id and host.coze_workflow_credential:
        keys[host.coze_workflow_key_id] = host.coze_workflow_credential.get_secret_value()
    return keys


def _identity_key_credential_classes(host) -> dict[str, frozenset[str]]:
    classes: dict[str, frozenset[str]] = {}
    if host.identity_current_key_id and host.identity_current_key:
        classes[host.identity_current_key_id] = frozenset({"oac_user"})
    if host.identity_previous_key_id and host.identity_previous_key:
        classes[host.identity_previous_key_id] = frozenset({"oac_user"})
    if host.oac_admin_key_id and host.oac_admin_credential:
        classes[host.oac_admin_key_id] = frozenset({"oac_admin"})
    if host.coze_workflow_key_id and host.coze_workflow_credential:
        classes[host.coze_workflow_key_id] = frozenset({"coze_workflow"})
    return classes
