from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.core.errors import RegistryVersionConflict
from app.schemas.registry_mutation import RegistryMutationCommand
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity import authorize_host_operation
from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity
from host_adapters.oac.mappers.registry import (
    InvalidRoutePath,
    RegistryPolicyProjectionError,
    RegistryValidationError,
    registry_agent_from_native,
    registry_agent_to_native,
)
from host_adapters.oac.schemas.registry import RegistryAgent, RegistryEnabledRequest
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)

router = APIRouter(prefix="/api/v1/admin/agent-registry", tags=["legacy-registry"])


@router.get("", response_model=list[RegistryAgent])
async def list_registry(
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> list[RegistryAgent]:
    _admin(identity)
    try:
        return [
            registry_agent_from_native(item) for item in await ports.registry.list_definitions()
        ]
    except RegistryPolicyProjectionError as exc:
        raise HTTPException(
            status_code=409, detail="registry_policy_not_legacy_projectable"
        ) from exc


@router.post("", response_model=RegistryAgent)
async def create_registry_agent(
    request: RegistryAgent,
    http_request: Request,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> RegistryAgent:
    _admin(identity)
    if await ports.registry.get_definition(request.agent_id):
        raise HTTPException(status_code=409, detail="agent_already_exists")
    try:
        definition = registry_agent_to_native(request)
        result = await ports.registry.mutate_definition(
            RegistryMutationCommand(
                operation="create",
                agent_id=request.agent_id,
                actor_id=identity.user_id,
                source="oac_host_adapter",
                expected_revision=0,
                definition=definition,
            )
        )
    except (RegistryValidationError, InvalidRoutePath) as exc:
        raise HTTPException(status_code=422, detail="registry_validation_failed") from exc
    except RegistryVersionConflict as exc:
        raise HTTPException(status_code=409, detail="agent_revision_conflict") from exc
    await _refresh_runtime_snapshot(http_request, ports)
    return registry_agent_from_native(result.after)


@router.put("/{agent_id}", response_model=RegistryAgent)
async def update_registry_agent(
    agent_id: str,
    request: RegistryAgent,
    http_request: Request,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> RegistryAgent:
    _admin(identity)
    if request.agent_id != agent_id:
        raise HTTPException(status_code=409, detail="agent_id_conflict")
    current = await ports.registry.get_definition(agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    try:
        definition = registry_agent_to_native(request, existing=current)
        result = await ports.registry.mutate_definition(
            RegistryMutationCommand(
                operation="update",
                agent_id=agent_id,
                actor_id=identity.user_id,
                source="oac_host_adapter",
                expected_revision=current.revision,
                definition=definition,
            )
        )
    except (RegistryValidationError, InvalidRoutePath) as exc:
        raise HTTPException(status_code=422, detail="registry_validation_failed") from exc
    except RegistryVersionConflict as exc:
        raise HTTPException(status_code=409, detail="agent_revision_conflict") from exc
    await _refresh_runtime_snapshot(http_request, ports)
    return registry_agent_from_native(result.after)


@router.patch("/{agent_id}/enabled", response_model=RegistryAgent)
async def set_registry_agent_enabled(
    agent_id: str,
    request: RegistryEnabledRequest,
    http_request: Request,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> RegistryAgent:
    _admin(identity)
    current = await ports.registry.get_definition(agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    try:
        result = await ports.registry.mutate_definition(
            RegistryMutationCommand(
                operation="enable" if request.enabled else "disable",
                agent_id=agent_id,
                actor_id=identity.user_id,
                source="oac_host_adapter",
                expected_revision=current.revision,
            )
        )
    except RegistryVersionConflict as exc:
        raise HTTPException(status_code=409, detail="agent_revision_conflict") from exc
    if result.after is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    await _refresh_runtime_snapshot(http_request, ports)
    return registry_agent_from_native(result.after)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_registry_agent(
    agent_id: str,
    http_request: Request,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> Response:
    _admin(identity)
    current = await ports.registry.get_definition(agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    try:
        await ports.registry.mutate_definition(
            RegistryMutationCommand(
                operation="delete",
                agent_id=agent_id,
                actor_id=identity.user_id,
                source="oac_host_adapter",
                expected_revision=current.revision,
            )
        )
    except RegistryVersionConflict as exc:
        raise HTTPException(status_code=409, detail="agent_revision_conflict") from exc
    await _refresh_runtime_snapshot(http_request, ports)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _admin(identity: TrustedHostIdentity) -> None:
    try:
        authorize_host_operation(identity, "control_write")
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc


async def _refresh_runtime_snapshot(request: Request, ports: OacAdapterApplicationPorts) -> None:
    """Keep the process Snapshot coherent after a committed legacy Registry write."""

    refresh = ports.registry_snapshot_refresh
    if refresh is None:
        return
    if not await refresh.refresh_registry_snapshot(ports.registry):
        raise HTTPException(status_code=503, detail="registry_snapshot_unavailable")
