"""Accept a selected External Execution Binding before Run and Ticket side effects."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.ports import (
    DelegatedRunApplicationPort,
    ExternalExecutorApplicationPort,
)
from app.core.errors import ExternalExecutionBindingUnavailableError
from app.schemas.agents import AgentDefinitionV2, ExternalExecutionHandling
from app.schemas.delegated_runs import (
    DelegatedRunExisting,
    DelegatedRunFailCommand,
    DelegatedRunReference,
    DelegatedRunStartCommand,
)
from app.schemas.external_execution import (
    ExternalExecutionPrincipal,
    ExternalExecutionStartResult,
    ExternalExecutorAcceptance,
    ExternalExecutorAcceptanceRequest,
)
from app.schemas.logs import (
    ExternalExecutionBindingSnapshot,
    external_execution_binding_fingerprint,
)
from app.schemas.routing import RouteRequest, RouteResponse
from app.services.execution_ticket_service import ExecutionTicketService
from app.services.registry_snapshot import (
    ExternalExecutionBindingRequirement,
    RegistrySnapshotSelection,
)


@dataclass(frozen=True, slots=True)
class _ResolvedExternalExecutionBinding:
    definition: AgentDefinitionV2
    persistence_snapshot: ExternalExecutionBindingSnapshot


class ExternalExecutionService:
    """Core coordinator for host-accepted External Execution.

    The Host supplies an ``ExternalExecutorApplicationPort`` but never receives a
    mutable Handling field. This service derives the executor reference, Agent
    identity, and persistence facts exclusively from the immutable Route Snapshot
    selection retained by ``RouteResponse``.
    """

    def __init__(
        self,
        *,
        external_executor: ExternalExecutorApplicationPort,
        delegated_runs: DelegatedRunApplicationPort,
        tickets: ExecutionTicketService,
        ticket_ttl_seconds: int,
    ) -> None:
        self._external_executor = external_executor
        self._delegated_runs = delegated_runs
        self._tickets = tickets
        self._ticket_ttl_seconds = ticket_ttl_seconds

    async def start_from_route(
        self,
        request: RouteRequest,
        response: RouteResponse,
        *,
        turn_id: str,
        deadline_at: datetime,
    ) -> ExternalExecutionStartResult:
        trusted_action = response.trusted_external_execution_action_for(request)
        if trusted_action is None:
            raise _binding_unavailable("external_execution_route_invalid")
        if not self._tickets.secret:
            raise _binding_unavailable("execution_ticket_unavailable")
        target = response.decision.target_agent_id
        selection = response.selected_binding(target or "")
        if not isinstance(selection, RegistrySnapshotSelection):
            raise _binding_unavailable("external_execution_binding_invalid")
        if selection.definition.agent_id != target:
            raise _binding_unavailable("external_execution_binding_invalid")

        definition = selection.definition
        if not isinstance(definition, AgentDefinitionV2) or definition.agent_id != target:
            raise _binding_unavailable("external_execution_binding_invalid")
        start_command = DelegatedRunStartCommand(
            tenant_id=request.user.tenant_id or "",
            user_id=request.user.id,
            session_id=request.session_id,
            request_id=response.request_id,
            turn_id=turn_id,
            agent_id=definition.agent_id,
            plan_id=trusted_action.plan_id,
            step_id=trusted_action.step_id,
            deadline_at=deadline_at,
            agent_revision=definition.revision,
            handling_kind="external_execution",
        )
        existing = await self._delegated_runs.find_existing(start_command)
        if existing is not None:
            binding = _binding_from_existing(
                existing,
                definition=definition,
                selection=selection,
            )
            return await self._issue_ticket(
                existing.run,
                binding=binding,
                request_id=response.request_id,
                compensate_on_failure=False,
            )

        binding = await self._accept(
            selection,
            request,
            acceptance_id=_acceptance_id(start_command),
        )
        started = await self._delegated_runs.start(
            start_command.model_copy(update={"binding_snapshot": binding.persistence_snapshot})
        )
        if started.duplicate:
            canonical = await self._delegated_runs.find_existing(start_command)
            if canonical is None:
                raise _binding_unavailable("external_execution_binding_invalid")
            binding = _binding_from_existing(
                canonical,
                definition=definition,
                selection=selection,
            )
        return await self._issue_ticket(
            started.run,
            binding=binding,
            request_id=response.request_id,
            compensate_on_failure=not started.duplicate,
        )

    async def _issue_ticket(
        self,
        run: DelegatedRunReference,
        *,
        binding: _ResolvedExternalExecutionBinding,
        request_id: str,
        compensate_on_failure: bool,
    ) -> ExternalExecutionStartResult:
        try:
            issued = await self._tickets.issue(
                run,
                request_id=request_id,
                purpose="agent_event",
                ttl_seconds=self._ticket_ttl_seconds,
                reuse_active_for_run=True,
            )
        except Exception as ticket_error:
            try:
                recovered = await self._tickets.recover_committed_ticket(
                    run,
                    purpose="agent_event",
                )
            except Exception as recovery_error:
                if compensate_on_failure:
                    raise _binding_unavailable(
                        "execution_ticket_compensation_failed"
                    ) from recovery_error
                raise _binding_unavailable("execution_ticket_unavailable") from recovery_error
            if recovered is not None:
                return _external_execution_start_result(
                    run,
                    binding=binding,
                    execution_ticket=recovered.ticket,
                )
            if compensate_on_failure:
                try:
                    recovered = await self._tickets.recover_after_issue_failure(
                        run,
                        purpose="agent_event",
                    )
                except Exception as compensation_error:
                    raise _binding_unavailable(
                        "execution_ticket_compensation_failed"
                    ) from compensation_error
                if recovered is not None:
                    return _external_execution_start_result(
                        run,
                        binding=binding,
                        execution_ticket=recovered.ticket,
                    )
                try:
                    await self._terminalize_unissued_run(
                        run,
                        reason="execution_ticket_issue_failed",
                    )
                except Exception as compensation_error:
                    raise _binding_unavailable(
                        "execution_ticket_compensation_failed"
                    ) from compensation_error
            raise _binding_unavailable("execution_ticket_unavailable") from ticket_error
        return _external_execution_start_result(
            run,
            binding=binding,
            execution_ticket=issued.ticket,
        )

    async def _accept(
        self,
        selection: RegistrySnapshotSelection,
        request: RouteRequest,
        *,
        acceptance_id: str,
    ) -> _ResolvedExternalExecutionBinding:
        definition = selection.definition
        if not isinstance(definition.handling, ExternalExecutionHandling):
            raise _binding_unavailable("external_execution_handling_invalid")
        if selection.entry.binding_status != "ready":
            raise _binding_unavailable(
                selection.entry.isolation_reason_code or "external_execution_unavailable"
            )
        requirement = selection.binding_requirement
        if not isinstance(requirement, ExternalExecutionBindingRequirement):
            raise _binding_unavailable("external_execution_binding_invalid")
        try:
            supported = bool(self._external_executor.supports(requirement.executor_ref))
        except Exception:
            raise _binding_unavailable("external_executor_unhealthy") from None
        if not supported:
            raise _binding_unavailable("external_executor_unsupported")
        tenant_id = request.user.tenant_id
        if not tenant_id:
            raise _binding_unavailable("external_executor_unauthorized")
        acceptance_request = ExternalExecutorAcceptanceRequest(
            acceptance_id=acceptance_id,
            executor_ref=requirement.executor_ref,
            agent_id=definition.agent_id,
            agent_revision=definition.revision,
            principal=ExternalExecutionPrincipal(
                tenant_id=tenant_id,
                user_id=request.user.id,
                roles=list(request.user.roles),
                groups=list(request.user.groups),
                entitlements=list(request.user.entitlements),
            ),
            params=dict(requirement.params),
        )
        try:
            acceptance = await self._external_executor.accept(acceptance_request)
        except Exception:
            raise _binding_unavailable("external_executor_unhealthy") from None
        if not isinstance(acceptance, ExternalExecutorAcceptance):
            raise _binding_unavailable("external_executor_unhealthy")
        if not acceptance.accepted:
            raise _binding_unavailable(acceptance.reason_code or "external_executor_unhealthy")
        if acceptance.binding_id is None:  # defensive for a nonconforming Host port
            raise _binding_unavailable("external_executor_unhealthy")
        return _ResolvedExternalExecutionBinding(
            definition=definition,
            persistence_snapshot=ExternalExecutionBindingSnapshot(
                executor_ref=requirement.executor_ref,
                executor_binding_id=external_execution_binding_fingerprint(
                    acceptance.binding_id,
                    secret=self._tickets.secret,
                ),
            ),
        )

    async def _terminalize_unissued_run(self, run: DelegatedRunReference, *, reason: str) -> None:
        try:
            await self._delegated_runs.fail(
                DelegatedRunFailCommand(
                    event_id=_compensation_event_id(run.run_id, stage="failed"),
                    run_id=run.run_id,
                    turn_id=run.turn_id,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    agent_id=run.agent_id,
                    plan_id=run.plan_id,
                    step_id=run.step_id,
                    expected_state_version=run.state_version,
                    occurred_at=datetime.now(UTC),
                    error={"code": reason},
                )
            )
        except Exception as failure_error:
            raise RuntimeError("External Execution Run compensation failed") from failure_error


def _binding_from_existing(
    existing: DelegatedRunExisting,
    *,
    definition: AgentDefinitionV2,
    selection: RegistrySnapshotSelection,
) -> _ResolvedExternalExecutionBinding:
    if existing.run.status not in {"pending", "running", "blocked"}:
        raise _binding_unavailable("external_execution_not_active")
    if existing.run.agent_id != definition.agent_id:
        raise _binding_unavailable("external_execution_binding_invalid")
    if existing.agent_revision != definition.revision:
        raise _binding_unavailable("external_execution_binding_invalid")
    if existing.handling_kind != "external_execution":
        raise _binding_unavailable("external_execution_binding_invalid")
    snapshot = existing.binding_snapshot
    if not isinstance(snapshot, ExternalExecutionBindingSnapshot):
        raise _binding_unavailable("external_execution_binding_invalid")
    requirement = selection.binding_requirement
    if not isinstance(requirement, ExternalExecutionBindingRequirement):
        raise _binding_unavailable("external_execution_binding_invalid")
    if snapshot.executor_ref != requirement.executor_ref:
        raise _binding_unavailable("external_execution_binding_invalid")
    return _ResolvedExternalExecutionBinding(
        definition=definition,
        persistence_snapshot=snapshot,
    )


def _acceptance_id(command: DelegatedRunStartCommand) -> str:
    identity = "\x1f".join(
        (
            command.tenant_id,
            command.user_id,
            command.request_id,
            command.turn_id,
            command.agent_id,
            command.plan_id or "",
            command.step_id or "",
        )
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"external_acceptance_{digest}"


def _compensation_event_id(run_id: str, *, stage: str) -> str:
    digest = hashlib.sha256(f"external-ticket:{stage}:{run_id}".encode()).hexdigest()
    return f"event_external_ticket_{stage}_{digest}"


def _external_execution_start_result(
    run: DelegatedRunReference,
    *,
    binding: _ResolvedExternalExecutionBinding,
    execution_ticket: str,
) -> ExternalExecutionStartResult:
    return ExternalExecutionStartResult(
        run=run,
        execution_ticket=execution_ticket,
        binding_snapshot=binding.persistence_snapshot,
        binding_trace_facts={
            "handling_kind": "external_execution",
            "executor_ref": binding.persistence_snapshot.executor_ref,
            "executor_binding_id": binding.persistence_snapshot.executor_binding_id,
        },
    )


def _binding_unavailable(reason_code: str) -> ExternalExecutionBindingUnavailableError:
    return ExternalExecutionBindingUnavailableError(
        "External Execution Binding is unavailable",
        details={"reason_code": reason_code},
    )
