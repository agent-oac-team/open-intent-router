import hashlib
from dataclasses import dataclass
from uuid import uuid4

from app.schemas.delegated_runs import (
    DelegatedRunCommandResult,
    DelegatedRunCompleteCommand,
    DelegatedRunOrphanQuery,
    DelegatedRunOrphanResponse,
    DelegatedRunProgressCommand,
    DelegatedRunReference,
    DelegatedRunStartCommand,
    DelegatedRunTimeoutCommand,
)
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.turns import CanonicalTurn


@dataclass(frozen=True)
class DelegatedRunStartResult:
    run: AgentRun
    turn: CanonicalTurn
    duplicate: bool


class DelegatedRunStartStore:
    async def start(
        self,
        *,
        command: DelegatedRunStartCommand,
        run_id: str,
        delegation_key: str,
    ) -> DelegatedRunStartResult:
        raise NotImplementedError


class DelegatedRunProgressStore:
    async def progress(self, command: DelegatedRunProgressCommand) -> tuple[AgentRun, bool]:
        raise NotImplementedError


@dataclass(frozen=True)
class DelegatedRunCompletionResult:
    run: AgentRun
    result: AgentResult
    turn: CanonicalTurn
    duplicate: bool


class DelegatedRunCompletionStore:
    async def complete(self, command: DelegatedRunCompleteCommand) -> DelegatedRunCompletionResult:
        raise NotImplementedError


class DelegatedRunMaintenanceStore:
    async def list_orphans(self, query: DelegatedRunOrphanQuery) -> list[AgentRun]:
        raise NotImplementedError

    async def timeout(self, command: DelegatedRunTimeoutCommand) -> tuple[AgentRun, bool]:
        raise NotImplementedError


class DelegatedRunService:
    def __init__(
        self,
        start_store: DelegatedRunStartStore,
        progress_store: DelegatedRunProgressStore | None = None,
        completion_store: DelegatedRunCompletionStore | None = None,
        maintenance_store: DelegatedRunMaintenanceStore | None = None,
    ) -> None:
        self.start_store = start_store
        self.progress_store = progress_store
        self.completion_store = completion_store
        self.maintenance_store = maintenance_store

    async def start(self, command: DelegatedRunStartCommand) -> DelegatedRunCommandResult:
        delegation_key = _delegation_key(command)
        stored = await self.start_store.start(
            command=command,
            run_id=f"run_{uuid4().hex}",
            delegation_key=delegation_key,
        )
        run = stored.run
        return DelegatedRunCommandResult(
            run=DelegatedRunReference(
                run_id=run.run_id,
                turn_id=run.turn_id or command.turn_id,
                tenant_id=run.tenant_id or command.tenant_id,
                user_id=run.user_id or command.user_id,
                agent_id=run.agent_id,
                plan_id=run.plan_id,
                step_id=run.step_id,
                status=run.status,
                state_version=run.state_version,
                deadline_at=run.deadline_at or command.deadline_at,
            ),
            duplicate=stored.duplicate,
            turn_id=stored.turn.turn_id,
        )

    async def progress(self, command: DelegatedRunProgressCommand) -> DelegatedRunCommandResult:
        if self.progress_store is None:
            raise RuntimeError("Delegated Run progress store is not configured")
        run, duplicate = await self.progress_store.progress(command)
        return DelegatedRunCommandResult(
            run=_run_reference(run),
            duplicate=duplicate,
            turn_id=command.turn_id,
        )

    async def complete(self, command: DelegatedRunCompleteCommand) -> DelegatedRunCommandResult:
        if self.completion_store is None:
            raise RuntimeError("Delegated Run completion store is not configured")
        completed = await self.completion_store.complete(command)
        return DelegatedRunCommandResult(
            run=_run_reference(completed.run),
            duplicate=completed.duplicate,
            result_id=completed.result.result_id,
            turn_id=completed.turn.turn_id,
        )

    async def list_orphans(self, query: DelegatedRunOrphanQuery) -> DelegatedRunOrphanResponse:
        if self.maintenance_store is None:
            raise RuntimeError("Delegated Run maintenance store is not configured")
        runs = await self.maintenance_store.list_orphans(query)
        return DelegatedRunOrphanResponse(runs=[_run_reference(run) for run in runs])

    async def timeout(self, command: DelegatedRunTimeoutCommand) -> DelegatedRunCommandResult:
        if self.maintenance_store is None:
            raise RuntimeError("Delegated Run maintenance store is not configured")
        run, duplicate = await self.maintenance_store.timeout(command)
        return DelegatedRunCommandResult(
            run=_run_reference(run),
            duplicate=duplicate,
            turn_id=command.turn_id,
        )


def _delegation_key(command: DelegatedRunStartCommand) -> str:
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
    return f"delegation_{hashlib.sha256(identity.encode()).hexdigest()}"


def _run_reference(run: AgentRun, fallback_deadline=None) -> DelegatedRunReference:
    if run.turn_id is None or run.tenant_id is None or run.user_id is None:
        raise ValueError("Delegated Run is missing canonical ownership")
    if run.deadline_at is None and fallback_deadline is None:
        raise ValueError("Delegated Run is missing deadline")
    return DelegatedRunReference(
        run_id=run.run_id,
        turn_id=run.turn_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        agent_id=run.agent_id,
        plan_id=run.plan_id,
        step_id=run.step_id,
        status=run.status,
        state_version=run.state_version,
        deadline_at=run.deadline_at or fallback_deadline,
    )
