import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.config import Settings
from app.repositories.memory_formation import formation_range_idempotency_key
from app.schemas.invocation import AgentInvocation, AgentInvocationResult
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.memory import (
    MemoryEvent,
    MemoryFormationJob,
    MemoryFormationMode,
    MemoryFormationReasonCode,
    MemoryFormationTrigger,
    MemoryFormationTurn,
)

_MAX_CAPSULE_REFS = 50
_MAX_CAPSULE_REF_CHARS = 128


class FormationJobProcessor(Protocol):
    async def process(self, job: MemoryFormationJob, *, execute_lifecycle: bool) -> dict: ...


class FormationProcessorUnavailable(RuntimeError):
    pass


class UnavailableFormationJobProcessor:
    async def process(self, job: MemoryFormationJob, *, execute_lifecycle: bool) -> dict:
        del job, execute_lifecycle
        raise FormationProcessorUnavailable("Formation processor is not configured")


@dataclass(frozen=True)
class TurnCaptureResult:
    status: str
    turn: MemoryFormationTurn | None = None
    job: MemoryFormationJob | None = None
    event: MemoryEvent | None = None


class TurnCapsuleBuilder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(
        self,
        *,
        invocation: AgentInvocation,
        result: AgentInvocationResult,
        result_id: str,
        completed_at: datetime | None = None,
    ) -> MemoryFormationTurn:
        tenant_id = invocation.user.tenant_id
        if not tenant_id:
            raise ValueError("Turn Capsule requires trusted tenant ownership")
        completed = completed_at or datetime.now(UTC)
        request_id = invocation.request_id or invocation.run_id
        plan_id = _optional_string(invocation.context.get("plan_id"))
        return MemoryFormationTurn(
            turn_id=formation_turn_id(
                tenant_id=tenant_id,
                user_id=invocation.user.id,
                session_id=invocation.session_id,
                request_id=request_id,
                run_id=invocation.run_id,
            ),
            request_id=request_id,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            user_id=invocation.user.id,
            tenant_id=tenant_id,
            agent_id=invocation.agent_id,
            user_text=_bounded_text(
                _user_text(invocation.input), self.settings.memory_formation_capsule_user_chars
            ),
            assistant_text=_bounded_text(
                _assistant_summary(
                    result, summary_limit=self.settings.memory_formation_capsule_summary_chars
                ),
                self.settings.memory_formation_capsule_assistant_chars,
            ),
            result_status=result.status,
            result_refs=_bounded_refs([result_id]),
            plan_refs=_bounded_refs([plan_id] if plan_id else []),
            artifact_refs=_bounded_refs(
                [artifact.artifact_id for artifact in result.artifact_refs]
            ),
            used_memory_ids=_bounded_refs(
                [item.memory_id for item in invocation.memory_context.items]
            ),
            completed_at=completed,
        )

    def build_from_records(
        self,
        *,
        run: AgentRun,
        result: AgentResult,
    ) -> MemoryFormationTurn:
        if not run.tenant_id or not run.user_id:
            raise ValueError("Turn Capsule requires trusted canonical ownership")
        _validate_result_run_association(run, result)
        completed = _as_utc(result.created_at or run.updated_at or datetime.now(UTC))
        request_id = run.request_id or run.run_id
        return MemoryFormationTurn(
            turn_id=formation_turn_id(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                session_id=run.session_id,
                request_id=request_id,
                run_id=run.run_id,
            ),
            request_id=request_id,
            session_id=run.session_id,
            run_id=run.run_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            agent_id=run.agent_id,
            user_text=_bounded_text(
                _user_text(run.input),
                self.settings.memory_formation_capsule_user_chars,
            ),
            assistant_text=_bounded_text(
                _record_assistant_summary(
                    result,
                    summary_limit=self.settings.memory_formation_capsule_summary_chars,
                ),
                self.settings.memory_formation_capsule_assistant_chars,
            ),
            result_status=result.status,
            result_refs=_bounded_refs([result.result_id]),
            plan_refs=_bounded_refs([run.plan_id] if run.plan_id else []),
            artifact_refs=_bounded_refs(
                [
                    str(ref.get("artifact_id"))
                    for ref in result.artifact_refs
                    if ref.get("artifact_id")
                ]
            ),
            used_memory_ids=_bounded_refs(run.used_memory_ids),
            completed_at=completed,
        )


class FormationTriggerCoordinator:
    def __init__(self, *, settings: Settings, repository) -> None:
        self.settings = settings
        self.repository = repository

    async def append_and_check(
        self, turn: MemoryFormationTurn
    ) -> tuple[MemoryFormationTurn, MemoryFormationJob | None]:
        deadline = turn.completed_at + timedelta(
            seconds=self.settings.memory_formation_idle_seconds
        )
        pending = turn.model_copy(update={"idle_deadline_at": deadline})
        return await self.repository.append_turn_and_maybe_create_window_job(
            pending,
            window_turns=self.settings.memory_formation_window_turns,
            mode=self.settings.memory_formation_mode,
            model_version=self.settings.memory_formation_model_version,
            prompt_version=self.settings.memory_formation_prompt_version,
            policy_version=self.settings.memory_formation_policy_version,
            max_attempts=self.settings.memory_formation_max_attempts,
        )


class TurnCaptureService:
    def __init__(
        self,
        *,
        settings: Settings,
        builder: TurnCapsuleBuilder,
        coordinator: FormationTriggerCoordinator,
        event_repository,
    ) -> None:
        self.settings = settings
        self.builder = builder
        self.coordinator = coordinator
        self.event_repository = event_repository

    async def capture(
        self,
        *,
        invocation: AgentInvocation,
        result: AgentInvocationResult,
        result_id: str,
        completed_at: datetime | None = None,
    ) -> TurnCaptureResult:
        if self.settings.memory_formation_mode == "off":
            return TurnCaptureResult(status="off")
        if not invocation.user.tenant_id:
            raise ValueError("Turn capture requires trusted tenant ownership")
        if request_prohibits_memory(invocation):
            event = MemoryEvent(
                event_id=_skip_event_id(
                    tenant_id=invocation.user.tenant_id,
                    user_id=invocation.user.id,
                    run_id=invocation.run_id,
                ),
                event_type="formation_skipped",
                user_id=invocation.user.id,
                tenant_id=invocation.user.tenant_id,
                request_id=invocation.request_id,
                session_id=invocation.session_id,
                run_id=invocation.run_id,
                agent_id=invocation.agent_id,
                decision_status="skipped",
                payload={"reason_code": MemoryFormationReasonCode.TEMPORARY_REQUEST.value},
            )
            stored = await self.event_repository.add_event(event)
            return TurnCaptureResult(status="skipped", event=stored)
        turn = self.builder.build(
            invocation=invocation,
            result=result,
            result_id=result_id,
            completed_at=completed_at,
        )
        stored_turn, job = await self.coordinator.append_and_check(turn)
        return TurnCaptureResult(status="captured", turn=stored_turn, job=job)

    async def capture_records(
        self,
        *,
        run: AgentRun,
        result: AgentResult,
    ) -> TurnCaptureResult:
        if self.settings.memory_formation_mode == "off":
            return TurnCaptureResult(status="off")
        if run.formation_suppressed:
            if not result.formation_skip_audit_required:
                return TurnCaptureResult(status="off")
            event = MemoryEvent(
                event_id=_skip_event_id(
                    tenant_id=run.tenant_id or "",
                    user_id=run.user_id or "",
                    run_id=run.run_id,
                ),
                event_type="formation_skipped",
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                request_id=run.request_id,
                session_id=run.session_id,
                run_id=run.run_id,
                agent_id=run.agent_id,
                decision_status="skipped",
                payload={"reason_code": MemoryFormationReasonCode.TEMPORARY_REQUEST.value},
            )
            stored = await self.event_repository.add_event(event)
            return TurnCaptureResult(status="skipped", event=stored)
        turn = self.builder.build_from_records(run=run, result=result)
        stored_turn, job = await self.coordinator.append_and_check(turn)
        return TurnCaptureResult(status="captured", turn=stored_turn, job=job)


class FormationIdleSweeper:
    def __init__(self, *, settings: Settings, repository) -> None:
        self.settings = settings
        self.repository = repository

    async def run_once(self, *, now: datetime | None = None) -> list[MemoryFormationJob]:
        if self.settings.memory_formation_mode == "off":
            return []
        current = now or datetime.now(UTC)
        identities = await self.repository.list_idle_sessions(now=current)
        jobs = []
        for tenant_id, user_id, session_id in identities:
            job = await self.repository.create_job_for_pending(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                trigger=MemoryFormationTrigger.IDLE,
                mode=self.settings.memory_formation_mode,
                model_version=self.settings.memory_formation_model_version,
                prompt_version=self.settings.memory_formation_prompt_version,
                policy_version=self.settings.memory_formation_policy_version,
                max_attempts=self.settings.memory_formation_max_attempts,
                idle_due_at=current,
            )
            if job is not None:
                jobs.append(job)
        return jobs


class FormationJobWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        repository,
        processor: FormationJobProcessor,
        owner: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.processor = processor
        self.owner = owner
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self) -> MemoryFormationJob | None:
        if self.settings.memory_formation_mode == "off":
            return None
        started = self.clock()
        claimed = await self.repository.claim_job(
            owner=self.owner,
            now=started,
            lease_seconds=self.settings.memory_formation_lease_seconds,
        )
        if claimed is None:
            return None
        processing_started = time.perf_counter()
        try:
            summary = await asyncio.wait_for(
                self.processor.process(
                    claimed,
                    execute_lifecycle=claimed.mode == MemoryFormationMode.ENFORCED,
                ),
                timeout=self.settings.memory_formation_model_timeout_seconds,
            )
            latency_ms = max(0, int((time.perf_counter() - processing_started) * 1000))
            queue_latency_ms = max(
                0, int((started - _as_utc(claimed.created_at)).total_seconds() * 1000)
            )
            summary = {
                **summary,
                "queue_latency_ms": summary.get("queue_latency_ms", queue_latency_ms),
                "job_latency_ms": latency_ms,
                "model_latency_ms": summary.get("model_latency_ms", latency_ms),
            }
            return await self.repository.complete_job(
                claimed.job_id,
                owner=self.owner,
                lease_token=claimed.lease_token,
                now=self.clock(),
                trace_summary=summary,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            error_code = "formation_processor_timeout"
        except FormationProcessorUnavailable:
            error_code = "formation_processor_unavailable"
        except Exception as exc:
            error_code = getattr(exc, "error_code", "formation_processor_error")
            if (
                not isinstance(error_code, str)
                or re.fullmatch(r"formation_[a-z0-9_]{1,118}", error_code) is None
            ):
                error_code = "formation_processor_error"
        delay = min(
            self.settings.memory_formation_retry_base_seconds
            * (2 ** max(claimed.attempt_count - 1, 0)),
            self.settings.memory_formation_retry_max_seconds,
        )
        terminal_time = self.clock()
        return await self.repository.fail_job(
            claimed.job_id,
            owner=self.owner,
            lease_token=claimed.lease_token,
            now=terminal_time,
            error_code=error_code,
            next_attempt_at=terminal_time + timedelta(seconds=delay),
        )


class MemoryFormationRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        worker: FormationJobWorker,
        sweeper: FormationIdleSweeper,
        reconciler=None,
        status=None,
    ) -> None:
        self.settings = settings
        self.worker = worker
        self.sweeper = sweeper
        self.reconciler = reconciler
        self.status = status or MemoryFormationRuntimeStatus()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        if self.settings.memory_formation_mode == "off":
            self.status.state = "disabled"
            return
        self.status.state = "starting"
        self._stop.clear()
        if self.reconciler is not None:
            try:
                await self.reconciler.run_once()
            except Exception:
                self.status.last_error_code = "formation_reconciler_error"
        if self.settings.memory_formation_worker_enabled:
            self._tasks.append(
                asyncio.create_task(self._worker_loop(), name="memory-formation-worker")
            )
        if self.settings.memory_formation_sweeper_enabled:
            self._tasks.append(
                asyncio.create_task(self._sweeper_loop(), name="memory-formation-sweeper")
            )
        if self.reconciler is not None:
            self._tasks.append(
                asyncio.create_task(self._reconciler_loop(), name="memory-formation-reconciler")
            )
        self.status.worker_running = self.settings.memory_formation_worker_enabled
        self.status.sweeper_running = self.settings.memory_formation_sweeper_enabled
        self.status.reconciler_running = self.reconciler is not None
        self.status.state = "running" if self._tasks else "idle"

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.status.worker_running = False
        self.status.sweeper_running = False
        self.status.reconciler_running = False
        self.status.state = "stopped"

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status.last_error_code = "formation_worker_error"
                processed = None
            if processed is None:
                await self._wait_interval()

    async def _sweeper_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweeper.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status.last_error_code = "formation_sweeper_error"
            await self._wait_interval()

    async def _reconciler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.reconciler.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status.last_error_code = "formation_reconciler_error"
            await self._wait_interval()

    async def _wait_interval(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=self.settings.memory_formation_sweep_interval_seconds,
            )
        except TimeoutError:
            pass


@dataclass
class MemoryFormationRuntimeStatus:
    state: str = "stopped"
    worker_running: bool = False
    sweeper_running: bool = False
    reconciler_running: bool = False
    last_error_code: str | None = None


def request_prohibits_memory(invocation: AgentInvocation) -> bool:
    for source in (invocation.context, invocation.input):
        if source.get("temporary") is True or source.get("private") is True:
            return True
        policy = source.get("memory_policy")
        if isinstance(policy, str) and policy.lower() in {"off", "temporary", "private"}:
            return True
        if isinstance(policy, dict):
            mode = str(policy.get("mode", "")).lower()
            if mode in {"off", "temporary", "private"} or policy.get("enabled") is False:
                return True
    return False


def structured_event_idempotency_key(
    *,
    tenant_id: str,
    user_id: str,
    source_type: str,
    source_id: str,
    source_version: str,
) -> str:
    value = "\x1f".join((tenant_id, user_id, source_type, source_id, source_version))
    return f"structured-event:{hashlib.sha256(value.encode()).hexdigest()}"


def formation_turn_id(
    *, tenant_id: str, user_id: str, session_id: str, request_id: str, run_id: str
) -> str:
    value = "\x1f".join((tenant_id, user_id, session_id, request_id, run_id))
    return f"mft_{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _skip_event_id(*, tenant_id: str, user_id: str, run_id: str) -> str:
    value = "\x1f".join((tenant_id, user_id, run_id, "formation_skipped"))
    return f"mevt_skip_{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _user_text(values: dict) -> str:
    for key in ("text", "query", "message", "prompt"):
        value = values.get(key)
        if isinstance(value, str) and value:
            return value
    return _json_summary(values)


def _assistant_summary(result: AgentInvocationResult, *, summary_limit: int) -> str:
    parts = [result.message] if result.message else []
    if result.output is not None:
        parts.append(_bounded_text(_json_summary(result.output), summary_limit))
    if result.error is not None:
        parts.append(f"error_code={result.error.code}")
    return "\n".join(part for part in parts if part)


def _record_assistant_summary(result: AgentResult, *, summary_limit: int) -> str:
    parts = [result.message] if result.message else []
    if result.output is not None:
        parts.append(_bounded_text(_json_summary(result.output), summary_limit))
    if result.error is not None:
        code = result.error.get("code") if isinstance(result.error, dict) else None
        parts.append(f"error_code={code or 'invocation_failed'}")
    return "\n".join(part for part in parts if part)


def _validate_result_run_association(run: AgentRun, result: AgentResult) -> None:
    if (
        not result.tenant_id
        or not result.user_id
        or result.run_id != run.run_id
        or result.session_id != run.session_id
        or result.agent_id != run.agent_id
        or result.tenant_id != run.tenant_id
        or result.user_id != run.user_id
        or result.plan_id != run.plan_id
        or result.step_id != run.step_id
    ):
        raise ValueError("Result does not belong to the canonical Run owner")


def _json_summary(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _bounded_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _bounded_refs(values: list[str]) -> list[str]:
    bounded = []
    for value in values:
        if not value:
            continue
        normalized = (
            value
            if len(value) <= _MAX_CAPSULE_REF_CHARS
            else f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"
        )
        if normalized not in bounded:
            bounded.append(normalized)
        if len(bounded) >= _MAX_CAPSULE_REFS:
            break
    return bounded


def _optional_string(value) -> str | None:
    return str(value) if value is not None and str(value) else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "FormationIdleSweeper",
    "FormationJobProcessor",
    "FormationJobWorker",
    "FormationTriggerCoordinator",
    "MemoryFormationRuntime",
    "TurnCapsuleBuilder",
    "TurnCaptureResult",
    "TurnCaptureService",
    "UnavailableFormationJobProcessor",
    "formation_range_idempotency_key",
    "formation_turn_id",
    "request_prohibits_memory",
    "structured_event_idempotency_key",
]
