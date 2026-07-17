from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.errors import RegistryVersionConflict
from app.core.redaction import redact_value
from app.repositories.registry_audit import RegistryAuditStore
from app.schemas.agents import AgentDefinition
from app.schemas.registry_audit import RegistryAuditRecord
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity import authorize_host_operation
from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity
from host_adapters.oac.mappers.registry import (
    registry_agent_from_native,
    registry_agent_to_native,
)
from host_adapters.oac.schemas.registry import RegistryAgent, RegistryEnabledRequest
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_registry_audit_store,
    get_trusted_host_identity,
)

router = APIRouter(prefix="/api/v1/admin/agent-registry", tags=["legacy-registry"])


@router.get("", response_model=list[RegistryAgent])
async def list_registry(
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> list[RegistryAgent]:
    _admin(identity)
    return [registry_agent_from_native(item) for item in await ports.registry.list_definitions()]


@router.post("", response_model=RegistryAgent)
async def create_registry_agent(
    request: RegistryAgent,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    audit: RegistryAuditStore = Depends(get_registry_audit_store),
) -> RegistryAgent:
    _admin(identity)
    if await ports.registry.get_definition(request.agent_id):
        raise HTTPException(status_code=409, detail="agent_already_exists")
    try:
        saved = await ports.registry.upsert_definition(
            registry_agent_to_native(request), expected_revision=0
        )
    except RegistryVersionConflict as exc:
        raise HTTPException(status_code=409, detail="agent_revision_conflict") from exc
    await _audit(audit, identity, operation="create", before=None, after=saved)
    return registry_agent_from_native(saved)


@router.put("/{agent_id}", response_model=RegistryAgent)
async def update_registry_agent(
    agent_id: str,
    request: RegistryAgent,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    audit: RegistryAuditStore = Depends(get_registry_audit_store),
) -> RegistryAgent:
    _admin(identity)
    if request.agent_id != agent_id:
        raise HTTPException(status_code=409, detail="agent_id_conflict")
    current = await ports.registry.get_definition(agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    try:
        saved = await ports.registry.upsert_definition(
            registry_agent_to_native(request), expected_revision=current.revision
        )
    except RegistryVersionConflict as exc:
        raise HTTPException(status_code=409, detail="agent_revision_conflict") from exc
    await _audit(audit, identity, operation="update", before=current, after=saved)
    return registry_agent_from_native(saved)


@router.patch("/{agent_id}/enabled", response_model=RegistryAgent)
async def set_registry_agent_enabled(
    agent_id: str,
    request: RegistryEnabledRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    audit: RegistryAuditStore = Depends(get_registry_audit_store),
) -> RegistryAgent:
    _admin(identity)
    current = await ports.registry.get_definition(agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    try:
        updated = await ports.registry.set_enabled(
            agent_id, request.enabled, expected_revision=current.revision
        )
    except RegistryVersionConflict as exc:
        raise HTTPException(status_code=409, detail="agent_revision_conflict") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    await _audit(
        audit,
        identity,
        operation="enable" if request.enabled else "disable",
        before=current,
        after=updated,
    )
    return registry_agent_from_native(updated)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_registry_agent(
    agent_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    audit: RegistryAuditStore = Depends(get_registry_audit_store),
) -> Response:
    _admin(identity)
    current = await ports.registry.get_definition(agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    try:
        deleted = await ports.registry.delete_definition(
            agent_id, expected_revision=current.revision
        )
    except RegistryVersionConflict as exc:
        raise HTTPException(status_code=409, detail="agent_revision_conflict") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="agent_not_found")
    await _audit(audit, identity, operation="delete", before=current, after=None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _admin(identity: TrustedHostIdentity) -> None:
    try:
        authorize_host_operation(identity, "control_write")
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc


async def _audit(
    store: RegistryAuditStore,
    identity: TrustedHostIdentity,
    *,
    operation: str,
    before: AgentDefinition | None,
    after: AgentDefinition | None,
) -> None:
    revision = after.revision if after else (before.revision + 1 if before else 1)
    await store.append(
        RegistryAuditRecord(
            revision_id=f"registry_revision_{uuid4().hex}",
            agent_id=(after or before).agent_id,
            revision=revision,
            operation=operation,
            operator_id=identity.user_id,
            source="oac_host_adapter",
            before=redact_value(before.model_dump(mode="json")) if before else None,
            after=redact_value(after.model_dump(mode="json")) if after else None,
            created_at=datetime.now(UTC),
        )
    )
