import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from app.schemas.execution_traces import (
    ExecutionTraceEvent,
    ExecutionTraceEventDraft,
    ExecutionTraceQuery,
    ExecutionTraceSnapshot,
    TraceEvidenceRef,
    trace_id_for_turn,
)
from app.schemas.memory import MemoryManagementOperationResponse, MemoryPendingDecisionEvidence
from app.services.execution_trace_service import ExecutionTraceConflict
from app.services.memory_management import MemoryManagementConflict, MemoryManagementNotFound
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity import authorize_host_operation
from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity
from host_adapters.oac.schemas.runtime_observation import (
    MemoryDecisionActionRequest,
    PageWorkflowEventRequest,
    RuntimeObservationAcceptedResponse,
    UiHandoffEventRequest,
)
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)

router = APIRouter(prefix="/api/v1/runtime-observation", tags=["runtime-observation"])


@router.get(
    "/sessions/{session_id}/turns/{turn_id}",
    response_model=ExecutionTraceSnapshot,
)
async def runtime_observation_snapshot(
    session_id: str,
    turn_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> ExecutionTraceSnapshot:
    _read_trace(identity)
    service = _trace_service(ports)
    snapshot = await service.snapshot(_query(identity, session_id=session_id, turn_id=turn_id))
    if not snapshot.events:
        raise HTTPException(status_code=404, detail="execution_trace_not_found")
    return _public_snapshot(snapshot)


@router.get("/sessions/{session_id}/turns/{turn_id}/events")
async def runtime_observation_stream(
    session_id: str,
    turn_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> StreamingResponse:
    _read_trace(identity)
    service = _trace_service(ports)
    query = _query(identity, session_id=session_id, turn_id=turn_id)
    snapshot = await service.snapshot(query)
    if not snapshot.events:
        raise HTTPException(status_code=404, detail="execution_trace_not_found")
    # The client sends the watermark of the Snapshot it has already rendered.
    # Do not replace it with a newer server-side Snapshot watermark: an event
    # written between those two requests must still be streamed.
    cursor = (
        _parse_last_event_id(last_event_id)
        if last_event_id is not None and last_event_id.strip()
        else snapshot.watermark
    )

    async def events():
        current_metadata = _trace_metadata(snapshot)
        yield _metadata_frame(current_metadata)
        stream_cursor = cursor
        while True:
            new_events = await service.events_after(query, after_offset=stream_cursor)
            for event in new_events:
                stream_cursor = event.event_offset
                payload = json.dumps(
                    jsonable_encoder(_public_trace_event(event)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"id: {event.event_offset}\nevent: execution_trace\ndata: {payload}\n\n"

            current_snapshot = await service.snapshot(query)
            next_metadata = _trace_metadata(current_snapshot)
            if next_metadata != current_metadata:
                current_metadata = next_metadata
                yield _metadata_frame(current_metadata)
            if not new_events:
                await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-OIR-Trace-Completeness": snapshot.completeness,
            "X-OIR-Trace-Recovered": str(snapshot.recovered).lower(),
        },
    )


def _trace_metadata(snapshot: ExecutionTraceSnapshot) -> dict[str, object]:
    return {
        "completeness": snapshot.completeness,
        "incomplete_reason_codes": snapshot.incomplete_reason_codes,
        "recovered": snapshot.recovered,
        "recovered_state": (
            snapshot.recovered_state.model_dump(mode="json")
            if snapshot.recovered_state is not None
            else None
        ),
    }


def _public_snapshot(snapshot: ExecutionTraceSnapshot) -> ExecutionTraceSnapshot:
    return snapshot.model_copy(
        update={"events": [_public_trace_event(event) for event in snapshot.events]}
    )


def _public_trace_event(event: ExecutionTraceEvent) -> ExecutionTraceEvent:
    if event.event_type != "memory_decision":
        return event
    facts = {
        key: value
        for key, value in event.facts.items()
        if key not in {"previous_value", "proposed_value"}
    }
    if facts == event.facts:
        return event
    return event.model_copy(update={"facts": facts})


def _metadata_frame(metadata: dict[str, object]) -> str:
    payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    return f"event: execution_trace_meta\ndata: {payload}\n\n"


@router.post(
    "/sessions/{session_id}/turns/{turn_id}/handoffs",
    status_code=202,
    response_model=RuntimeObservationAcceptedResponse,
)
async def runtime_observation_handoff(
    session_id: str,
    turn_id: str,
    request: UiHandoffEventRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> RuntimeObservationAcceptedResponse:
    _write_trace(identity)
    await _verify_owned_turn(
        ports,
        identity=identity,
        session_id=session_id,
        turn_id=turn_id,
    )
    service = _trace_service(ports)
    stage, status = _handoff_lifecycle(request.status)
    facts = {
        "target_route": request.target_route,
        "from_route": request.from_path,
        "reason": request.reason,
    }
    if request.failure_code is not None:
        facts["failure_code"] = request.failure_code
    try:
        trace_complete = await service.try_record(
            ExecutionTraceEventDraft(
                trace_id=trace_id_for_turn(turn_id),
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=session_id,
                turn_id=turn_id,
                event_type="ui_handoff",
                stage=stage,
                status=status,
                source="oac:ui_handoff",
                source_event_id=f"{request.handoff_id}:{request.status}",
                facts=facts,
                occurred_at=request.occurred_at,
            )
        )
    except ExecutionTraceConflict as exc:
        raise HTTPException(status_code=409, detail="ui_handoff_source_conflict") from exc
    return RuntimeObservationAcceptedResponse(
        accepted=True,
        observation_status="complete" if trace_complete else "incomplete",
        incomplete_reason_codes=[] if trace_complete else ["trace_projection_write_failed"],
    )


@router.post(
    "/sessions/{session_id}/turns/{turn_id}/page-workflows",
    status_code=202,
    response_model=RuntimeObservationAcceptedResponse,
)
async def runtime_observation_page_workflow(
    session_id: str,
    turn_id: str,
    request: PageWorkflowEventRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> RuntimeObservationAcceptedResponse:
    _write_trace(identity)
    await _verify_owned_turn(
        ports,
        identity=identity,
        session_id=session_id,
        turn_id=turn_id,
    )
    event_type, stage, status, facts = _page_workflow_lifecycle(request)
    try:
        trace_complete = await _trace_service(ports).try_record(
            ExecutionTraceEventDraft(
                trace_id=trace_id_for_turn(turn_id),
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=request.run_id,
                event_type=event_type,
                stage=stage,
                status=status,
                source="oac:page_workflow",
                source_event_id=f"{request.run_id}:{request.event_id}",
                facts=facts,
                evidence_refs=[
                    TraceEvidenceRef(
                        reference_type="provider_workflow",
                        reference_id=request.workflow_id,
                        label="page workflow",
                    )
                ],
                occurred_at=request.occurred_at,
            )
        )
    except ExecutionTraceConflict as exc:
        raise HTTPException(status_code=409, detail="page_workflow_source_conflict") from exc
    return RuntimeObservationAcceptedResponse(
        accepted=True,
        observation_status="complete" if trace_complete else "incomplete",
        incomplete_reason_codes=[] if trace_complete else ["trace_projection_write_failed"],
    )


@router.get(
    "/sessions/{session_id}/turns/{turn_id}/memory-decisions/{decision_id}/evidence",
    response_model=MemoryPendingDecisionEvidence,
)
async def runtime_observation_memory_decision_evidence(
    session_id: str,
    turn_id: str,
    decision_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> MemoryPendingDecisionEvidence:
    _read_trace(identity)
    await _verify_owned_turn(
        ports,
        identity=identity,
        session_id=session_id,
        turn_id=turn_id,
    )
    trace = _trace_service(ports)
    snapshot = await trace.snapshot(_query(identity, session_id=session_id, turn_id=turn_id))
    if not _has_pending_memory_decision(snapshot, decision_id):
        raise HTTPException(status_code=404, detail="memory_decision_not_found")
    if ports.memory_management is None:
        raise HTTPException(status_code=503, detail="memory_management_unavailable")
    try:
        return await ports.memory_management.get_pending_decision_evidence(
            decision_id=decision_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
    except MemoryManagementNotFound as exc:
        raise HTTPException(status_code=404, detail="memory_decision_not_found") from exc


@router.post(
    "/sessions/{session_id}/turns/{turn_id}/memory-decisions/{decision_id}/{action}",
    response_model=MemoryManagementOperationResponse,
)
async def runtime_observation_memory_decision(
    session_id: str,
    turn_id: str,
    decision_id: str,
    action: str,
    request: MemoryDecisionActionRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> MemoryManagementOperationResponse:
    _write_trace(identity)
    if action not in {"confirm", "reject"}:
        raise HTTPException(status_code=404, detail="memory_decision_action_not_found")
    await _verify_owned_turn(
        ports,
        identity=identity,
        session_id=session_id,
        turn_id=turn_id,
    )
    trace = _trace_service(ports)
    snapshot = await trace.snapshot(_query(identity, session_id=session_id, turn_id=turn_id))
    if not _has_pending_memory_decision(snapshot, decision_id):
        raise HTTPException(status_code=404, detail="memory_decision_not_found")
    if ports.memory_management is None:
        raise HTTPException(status_code=503, detail="memory_management_unavailable")
    try:
        return await ports.memory_management.resolve_pending(
            decision_id=decision_id,
            action=action,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            actor=identity.user_id,
            reason=request.reason,
            idempotency_key=request.idempotency_key,
            expected_revision_id=request.expected_revision_id,
            trace_session_id=session_id,
            trace_turn_id=turn_id,
        )
    except MemoryManagementNotFound as exc:
        raise HTTPException(status_code=404, detail="memory_decision_not_found") from exc
    except MemoryManagementConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _has_pending_memory_decision(snapshot: ExecutionTraceSnapshot, decision_id: str) -> bool:
    return any(
        event.event_type == "memory_decision"
        and event.facts.get("decision_id") == decision_id
        and event.facts.get("decision_status") == "pending"
        for event in snapshot.events
    )


def _query(
    identity: TrustedHostIdentity,
    *,
    session_id: str,
    turn_id: str,
) -> ExecutionTraceQuery:
    return ExecutionTraceQuery(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        session_id=session_id,
        turn_id=turn_id,
    )


def _trace_service(ports: OacAdapterApplicationPorts):
    if ports.execution_traces is None:
        raise HTTPException(status_code=503, detail="runtime_observation_unavailable")
    return ports.execution_traces


async def _verify_owned_turn(
    ports: OacAdapterApplicationPorts,
    *,
    identity: TrustedHostIdentity,
    session_id: str,
    turn_id: str,
) -> None:
    turn = await ports.turns.get_turn(
        turn_id=turn_id,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    )
    if turn is None or turn.session_id != session_id:
        raise HTTPException(status_code=404, detail="execution_trace_not_found")


def _read_trace(identity: TrustedHostIdentity) -> None:
    try:
        authorize_host_operation(identity, "read_only")
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc


def _write_trace(identity: TrustedHostIdentity) -> None:
    try:
        authorize_host_operation(identity, "runtime_write_own")
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc


def _handoff_lifecycle(status: str) -> tuple[str, str]:
    if status == "requested":
        return "requested", "running"
    if status == "completed":
        return "target_opened", "completed"
    return "target_open_failed", "failed"


def _page_workflow_lifecycle(
    request: PageWorkflowEventRequest,
) -> tuple[str, str, str, dict[str, object]]:
    if request.status == "started":
        return (
            "agent_run",
            "started",
            "running",
            {
                "agent_id": request.agent_id,
                "capability": request.capability,
                "invoker_type": "ui_handoff",
                "delegated": False,
            },
        )
    if request.status == "stage":
        return (
            "agent_event",
            "provider_stage",
            "running",
            {
                "agent_id": request.agent_id,
                "capability": request.capability,
                "provider_stage_name": request.stage_name,
            },
        )
    if request.status == "completed":
        return (
            "agent_result",
            "result_received",
            "completed",
            {
                "agent_id": request.agent_id,
                "capability": request.capability,
                "result_summary": request.result_summary,
                "artifact_count": 0,
            },
        )
    return (
        "agent_result",
        "failed",
        "failed",
        {
            "agent_id": request.agent_id,
            "capability": request.capability,
            "error_code": request.error_code,
            "artifact_count": 0,
        },
    )


def _parse_last_event_id(value: str | None) -> int:
    if value is None or not value.strip():
        return 0
    try:
        offset = int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_last_event_id") from exc
    if offset < 0:
        raise HTTPException(status_code=400, detail="invalid_last_event_id")
    return offset
