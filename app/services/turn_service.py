from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from app.repositories.interfaces import TurnRepository
from app.repositories.turns import TurnOwnershipConflict, TurnTerminalStateError
from app.schemas.turns import (
    CanonicalTurn,
    TurnOutboxEvent,
    TurnResultReferences,
    TurnSemanticResponse,
    TurnStatus,
    TurnUserInput,
)


class TurnIdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class TurnStartResult:
    turn: CanonicalTurn
    created: bool


class TurnService:
    def __init__(self, repository: TurnRepository, *, route_completion_store=None) -> None:
        self.repository = repository
        self.route_completion_store = route_completion_store

    async def start_turn(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        request_id: str,
        source: str,
        user_input: TurnUserInput,
    ) -> TurnStartResult:
        request_owner = await self.repository.find_by_request_id(request_id)
        if request_owner is not None:
            self._validate_replay(
                request_owner,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                source=source,
                user_input=user_input,
            )
            return TurnStartResult(turn=request_owner, created=False)

        now = datetime.now(UTC)
        turn = CanonicalTurn(
            turn_id=f"turn_{uuid4().hex}",
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            source=source,
            user_input=user_input,
            created_at=now,
            updated_at=now,
        )
        try:
            stored, created = await self.repository.create_idempotent(turn)
        except TurnOwnershipConflict as exc:
            raise TurnIdempotencyConflict("request identity conflicts with existing turn") from exc
        self._validate_replay(
            stored,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            source=source,
            user_input=user_input,
        )
        return TurnStartResult(turn=stored, created=created)

    async def get_turn(
        self,
        *,
        turn_id: str,
        tenant_id: str,
        user_id: str,
    ) -> CanonicalTurn | None:
        return await self.repository.get(
            turn_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    async def submission_status(
        self, *, request_id: str, tenant_id: str, user_id: str
    ) -> Literal["not_accepted", "committed", "unknown"]:
        try:
            turn = await self.repository.find_by_request_id(request_id)
        except Exception:
            return "unknown"
        if turn is None:
            return "not_accepted"
        if turn.tenant_id == tenant_id and turn.user_id == user_id:
            return "committed"
        return "unknown"

    async def complete_route_only(
        self,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
        response_kind: str,
        response_text: str,
        error: dict | None = None,
    ) -> CanonicalTurn:
        if response_kind not in {"reply", "clarify", "unsupported", "silent"}:
            raise ValueError("route-only completion requires a direct response kind")
        existing = await self.repository.get_by_request(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        if existing is None:
            raise ValueError("canonical turn not found")
        final_response = TurnSemanticResponse(
            kind=response_kind,
            text=response_text,
            error=error,
        )
        if existing.status.is_terminal:
            if (
                existing.status == TurnStatus.COMPLETED
                and existing.final_response == final_response
            ):
                return existing
            raise TurnTerminalStateError("terminal turn cannot be completed again")
        now = datetime.now(UTC)
        completed = existing.model_copy(
            update={
                "status": TurnStatus.COMPLETED,
                "state_version": existing.state_version + 1,
                "references": TurnResultReferences(
                    route_decision_id=f"route_decision:{request_id}"
                ),
                "final_response": final_response,
                "updated_at": now,
                "completed_at": now,
            }
        )
        if self.route_completion_store is None:
            stored = await self.repository.update_if_version(
                completed,
                expected_version=existing.state_version,
            )
        else:
            stored = await self.route_completion_store.complete(
                completed,
                expected_version=existing.state_version,
                outbox=TurnOutboxEvent(
                    outbox_id=f"outbox_{uuid4().hex}",
                    turn_id=completed.turn_id,
                    event_type="turn.completed",
                    idempotency_key=f"turn.completed:{completed.turn_id}",
                    payload={
                        "turn_id": completed.turn_id,
                        "tenant_id": completed.tenant_id,
                        "user_id": completed.user_id,
                        "request_id": completed.request_id,
                        "state_version": completed.state_version,
                    },
                    available_at=now,
                ),
            )
        if stored is not None:
            return stored
        concurrent = await self.repository.get_by_request(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        if (
            concurrent is not None
            and concurrent.status == TurnStatus.COMPLETED
            and concurrent.final_response == final_response
        ):
            return concurrent
        raise TurnTerminalStateError("turn completion conflicted")

    async def attach_activity(
        self,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
        run_id: str | None = None,
        plan_id: str | None = None,
        blocked: bool = False,
    ) -> CanonicalTurn:
        if not run_id and not plan_id:
            raise ValueError("turn activity requires a Run or Plan reference")
        existing = await self._owned_turn(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        if existing.status.is_terminal:
            raise TurnTerminalStateError("terminal turn cannot attach activity")
        references = existing.references.model_copy(deep=True)
        if run_id and run_id not in references.run_ids:
            references.run_ids.append(run_id)
        if plan_id:
            if references.plan_id and references.plan_id != plan_id:
                raise TurnIdempotencyConflict("turn is already bound to another plan")
            references.plan_id = plan_id
        updated = existing.model_copy(
            update={
                "status": TurnStatus.BLOCKED if blocked else TurnStatus.RUNNING,
                "state_version": existing.state_version + 1,
                "references": references,
                "updated_at": datetime.now(UTC),
            }
        )
        return await self._store_transition(existing, updated)

    async def complete_with_result(
        self,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
        run_id: str,
        result_id: str,
        response_text: str,
        plan_id: str | None = None,
    ) -> CanonicalTurn:
        existing = await self._owned_turn(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        final_response = TurnSemanticResponse(kind="agent_result", text=response_text)
        if existing.status.is_terminal:
            if (
                existing.status == TurnStatus.COMPLETED
                and existing.final_response == final_response
                and result_id in existing.references.result_ids
            ):
                return existing
            raise TurnTerminalStateError("terminal turn cannot accept another result")
        if run_id not in existing.references.run_ids:
            raise TurnIdempotencyConflict("result Run does not match the active turn")
        if plan_id and existing.references.plan_id != plan_id:
            raise TurnIdempotencyConflict("result Plan does not match the active turn")
        references = existing.references.model_copy(deep=True)
        if result_id not in references.result_ids:
            references.result_ids.append(result_id)
        now = datetime.now(UTC)
        completed = existing.model_copy(
            update={
                "status": TurnStatus.COMPLETED,
                "state_version": existing.state_version + 1,
                "references": references,
                "final_response": final_response,
                "updated_at": now,
                "completed_at": now,
            }
        )
        stored = await self.repository.update_if_version(
            completed,
            expected_version=existing.state_version,
        )
        if stored is not None:
            return stored
        concurrent = await self._owned_turn(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        if (
            concurrent.status == TurnStatus.COMPLETED
            and concurrent.final_response == final_response
            and result_id in concurrent.references.result_ids
        ):
            return concurrent
        raise TurnTerminalStateError("turn result completion conflicted")

    async def _owned_turn(self, *, tenant_id: str, user_id: str, request_id: str) -> CanonicalTurn:
        turn = await self.repository.get_by_request(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        if turn is None:
            raise ValueError("canonical turn not found")
        return turn

    async def _store_transition(
        self, existing: CanonicalTurn, updated: CanonicalTurn
    ) -> CanonicalTurn:
        stored = await self.repository.update_if_version(
            updated,
            expected_version=existing.state_version,
        )
        if stored is None:
            raise TurnTerminalStateError("turn state changed concurrently")
        return stored

    @staticmethod
    def _validate_replay(
        existing: CanonicalTurn,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        source: str,
        user_input: TurnUserInput,
    ) -> None:
        if (
            existing.tenant_id != tenant_id
            or existing.user_id != user_id
            or existing.session_id != session_id
            or existing.source != source
            or existing.user_input != user_input
        ):
            raise TurnIdempotencyConflict("request identity or semantic input conflicts")
