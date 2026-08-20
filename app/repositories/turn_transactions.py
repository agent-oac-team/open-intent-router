from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    AgentEventModel,
    AgentResultModel,
    AgentRunModel,
    CanonicalTurnModel,
    PlanModel,
    PlanStepModel,
    TurnOutboxModel,
)
from app.repositories.json_utils import dumps
from app.repositories.plan_steps import plan_step_model
from app.schemas.events import AgentEvent
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.plans import Plan
from app.schemas.turns import CanonicalTurn, TurnOutboxEvent


class TurnTransactionConflict(ValueError):
    pass


@dataclass(frozen=True)
class TurnCompletionBundle:
    run: AgentRun
    result: AgentResult
    turn: CanonicalTurn
    outbox: TurnOutboxEvent
    plan: Plan | None = None
    event: AgentEvent | None = None


class DatabaseTurnTransactionCoordinator:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def complete(self, bundle: TurnCompletionBundle) -> None:
        _validate_bundle(bundle)
        async with self.session_factory() as session, session.begin():
            turn_row = await session.scalar(
                select(CanonicalTurnModel)
                .where(CanonicalTurnModel.turn_id == bundle.turn.turn_id)
                .with_for_update()
            )
            run_row = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == bundle.run.run_id)
                .with_for_update()
            )
            if turn_row is None or run_row is None:
                raise TurnTransactionConflict("Turn or Run does not exist")
            if (
                turn_row.tenant_id != bundle.turn.tenant_id
                or turn_row.user_id != bundle.turn.user_id
                or turn_row.state_version + 1 != bundle.turn.state_version
                or run_row.tenant_id != bundle.turn.tenant_id
                or run_row.user_id != bundle.turn.user_id
            ):
                raise TurnTransactionConflict("Turn or Run ownership/version conflict")

            _apply_run(run_row, bundle.run)
            session.add(AgentResultModel(**_result_values(bundle.result)))
            if bundle.event is not None:
                session.add(AgentEventModel(**_event_values(bundle.event)))
            if bundle.plan is not None:
                await _apply_plan(session, bundle.plan)
            _apply_turn(turn_row, bundle.turn)
            session.add(TurnOutboxModel(**_outbox_values(bundle.outbox)))


def _validate_bundle(bundle: TurnCompletionBundle) -> None:
    if bundle.result.run_id != bundle.run.run_id:
        raise TurnTransactionConflict("Result does not belong to Run")
    if bundle.outbox.turn_id != bundle.turn.turn_id:
        raise TurnTransactionConflict("Outbox does not belong to Turn")
    if bundle.run.run_id not in bundle.turn.references.run_ids:
        raise TurnTransactionConflict("Turn does not reference Run")
    if bundle.result.result_id not in bundle.turn.references.result_ids:
        raise TurnTransactionConflict("Turn does not reference Result")
    if bundle.plan is not None:
        if bundle.turn.references.plan_id != bundle.plan.plan_id:
            raise TurnTransactionConflict("Turn does not reference Plan")
        if (
            bundle.run.plan_id != bundle.plan.plan_id
            or bundle.result.plan_id != bundle.plan.plan_id
        ):
            raise TurnTransactionConflict("Run/Result Plan association conflicts")
    if bundle.event is not None and (
        bundle.event.run_id != bundle.run.run_id
        or bundle.event.turn_id != bundle.turn.turn_id
        or bundle.event.tenant_id != bundle.turn.tenant_id
        or bundle.event.user_id != bundle.turn.user_id
    ):
        raise TurnTransactionConflict("Event association conflicts")


def _apply_run(row: AgentRunModel, run: AgentRun) -> None:
    row.status = run.status
    row.turn_id = run.turn_id
    row.delegated = run.delegated
    row.delegation_key = run.delegation_key
    row.state_version = run.state_version
    row.event_sequence = run.event_sequence
    row.deadline_at = run.deadline_at
    row.heartbeat_at = run.heartbeat_at
    row.claim_owner = run.claim_owner
    row.claim_token = run.claim_token
    row.claim_expires_at = run.claim_expires_at
    row.terminal_event_id = run.terminal_event_id
    row.output_text = dumps(run.output) if run.output is not None else None
    row.error_text = dumps(run.error) if run.error is not None else None
    row.latency_ms = run.latency_ms
    row.updated_at = run.updated_at


def _result_values(result: AgentResult) -> dict:
    return {
        "result_id": result.result_id,
        "run_id": result.run_id,
        "session_id": result.session_id,
        "agent_id": result.agent_id,
        "user_id": result.user_id,
        "tenant_id": result.tenant_id,
        "turn_id": result.turn_id,
        "plan_id": result.plan_id,
        "step_id": result.step_id,
        "status": result.status,
        "run_state_version": result.run_state_version,
        "message": result.message,
        "formation_suppressed": result.formation_suppressed,
        "formation_published": False,
        "turn_captured": False,
        "output_text": dumps(result.output) if result.output is not None else None,
        "artifact_refs_text": dumps(result.artifact_refs),
        "error_text": dumps(result.error) if result.error is not None else None,
        "created_at": result.created_at,
    }


def _event_values(event: AgentEvent) -> dict:
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "request_id": event.request_id,
        "session_id": event.session_id,
        "agent_id": event.agent_id,
        "user_id": event.user_id,
        "tenant_id": event.tenant_id,
        "turn_id": event.turn_id,
        "agent_session_id": event.agent_session_id,
        "event_type": event.event_type,
        "status": event.status,
        "plan_id": event.plan_id,
        "step_id": event.step_id,
        "sequence": event.sequence,
        "run_state_version": event.run_state_version,
        "payload_text": dumps(event.payload),
        "created_at": event.created_at,
    }


async def _apply_plan(session: AsyncSession, plan: Plan) -> None:
    row = await session.scalar(
        select(PlanModel).where(PlanModel.plan_id == plan.plan_id).with_for_update()
    )
    if row is None or row.tenant_id != plan.tenant_id or row.user_id != plan.user_id:
        raise TurnTransactionConflict("Plan ownership conflict")
    if plan.state_version != row.state_version + 1:
        raise TurnTransactionConflict("Plan version conflict")
    row.status = plan.status
    row.current_step_id = plan.current_step_id
    row.state_version = plan.state_version
    row.original_query = dumps(
        {
            "execution_policy": plan.execution_policy,
            "next_action": plan.next_action.model_dump(mode="json") if plan.next_action else None,
            "last_event_id": plan.last_event_id,
            "state_version": plan.state_version,
            "formation_event_type": plan.formation_event_type,
        }
    )
    row.updated_at = plan.updated_at
    await session.execute(delete(PlanStepModel).where(PlanStepModel.plan_id == plan.plan_id))
    for step in plan.steps:
        session.add(plan_step_model(plan_id=plan.plan_id, step=step))


def _apply_turn(row: CanonicalTurnModel, turn: CanonicalTurn) -> None:
    row.status = turn.status.value
    row.state_version = turn.state_version
    row.references_text = dumps(turn.references.model_dump(mode="json"))
    row.final_response_text = (
        dumps(turn.final_response.model_dump(mode="json")) if turn.final_response else None
    )
    row.updated_at = turn.updated_at
    row.completed_at = turn.completed_at


def _outbox_values(event: TurnOutboxEvent) -> dict:
    return {
        "outbox_id": event.outbox_id,
        "turn_id": event.turn_id,
        "event_type": event.event_type,
        "idempotency_key": event.idempotency_key,
        "payload_text": dumps(event.payload),
        "status": event.status,
        "attempt_count": event.attempt_count,
        "max_attempts": event.max_attempts,
        "available_at": event.available_at,
    }
