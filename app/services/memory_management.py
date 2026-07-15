from __future__ import annotations

import hashlib

from app.core.redaction import redact_text
from app.schemas.memory import (
    MemoryDecisionStatus,
    MemoryEvent,
    MemoryFormationCandidate,
    MemoryFormationReasonCode,
    MemoryLifecycleOperation,
    MemoryManagementOperationResponse,
    MemoryOperation,
)
from app.services.memory_candidate_policy import CandidatePolicyResult


class MemoryManagementNotFound(ValueError):
    pass


class MemoryManagementConflict(ValueError):
    pass


class MemoryManagementService:
    def __init__(self, *, memory_service) -> None:
        self.memory_service = memory_service
        self.repository = memory_service.repository
        self.lifecycle = memory_service.lifecycle
        self.index_repository = memory_service.index_outbox

    async def request_delete(
        self,
        *,
        memory_id: str,
        tenant_id: str,
        user_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_revision_id: str | None,
        admin: bool = False,
    ) -> MemoryManagementOperationResponse:
        operation_id = _stable_id("mfop", f"delete\x1f{tenant_id}\x1f{actor}\x1f{idempotency_key}")
        item = await self.repository.get_by_id(memory_id, tenant_id=tenant_id)
        if item is None:
            return await self._completed_delete_replay(
                memory_id=memory_id,
                operation_id=operation_id,
                tenant_id=tenant_id,
                user_id=user_id,
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                expected_revision_id=expected_revision_id,
                admin=admin,
            )
        if not _owned(item, user_id=user_id, admin=admin):
            raise MemoryManagementNotFound("Memory operation target not found")
        _check_revision(item.current_revision_id, expected_revision_id)
        operation = MemoryLifecycleOperation(
            operation_id=operation_id,
            operation=MemoryOperation.DELETE,
            decision_status=MemoryDecisionStatus.ACCEPTED,
            reason_code=MemoryFormationReasonCode.AUTHORIZED_DELETE,
            tenant_id=tenant_id,
            user_id=item.user_id or user_id,
            subject_type=item.subject_type,
            subject_id=item.subject_id,
            agent_id=item.agent_id,
            source="admin_management" if admin else "user_management",
            metadata={"actor": actor, "reason": redact_text(reason, max_length=500)},
            memory_key=item.memory_key or f"legacy-memory:{item.memory_id}",
            candidate_hash=item.candidate_hash
            or f"sha256:{hashlib.sha256(item.memory_id.encode()).hexdigest()}",
            memory_id=item.memory_id,
            revision_id=item.current_revision_id,
            formation_job_id=item.formation_job_id,
            canonical_refs=list(item.canonical_refs),
        )
        try:
            await self._audit(
                operation=operation,
                action="delete",
                actor=actor,
                target_user_id=user_id,
                reason=reason,
                idempotency_key=idempotency_key,
                expected_revision_id=expected_revision_id,
                admin=admin,
            )
        except ValueError as exc:
            raise MemoryManagementConflict("Idempotency key payload changed") from exc
        try:
            result = await self.lifecycle.request_delete(operation)
        except ValueError as exc:
            raise MemoryManagementConflict("Memory operation precondition changed") from exc
        index = result.index_operation
        return MemoryManagementOperationResponse(
            operation_id=operation.operation_id,
            operation="delete",
            status="completed" if index and index.status == "completed" else "pending",
            memory_id=memory_id,
            index_operation_id=index.index_operation_id if index else None,
            provider_status=index.status.value if index else None,
            idempotent_replay=result.idempotent_replay,
        )

    async def resolve_pending(
        self,
        *,
        decision_id: str,
        action: str,
        tenant_id: str,
        user_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_revision_id: str | None,
        admin: bool = False,
    ) -> MemoryManagementOperationResponse:
        if action not in {"confirm", "reject"}:
            raise ValueError("Unsupported pending decision action")
        pending = await self.repository.get_event(
            decision_id,
            tenant_id=tenant_id,
            user_id=None if admin else user_id,
        )
        if (
            pending is None
            or pending.decision_status != "pending"
            or pending.user_id != user_id
            or not pending.event_type.startswith("memory_decision_")
        ):
            raise MemoryManagementNotFound("Memory operation target not found")
        completed = await self._resolution_event(decision_id, tenant_id=tenant_id, user_id=user_id)
        if completed is not None:
            payload = completed.payload
            if (
                payload.get("action") != action
                or payload.get("actor") != actor
                or payload.get("reason") != redact_text(reason, max_length=500)
                or payload.get("admin") != admin
                or payload.get("idempotency_hash") != _hash_ref(idempotency_key)
            ):
                raise MemoryManagementConflict("Idempotency key payload changed")
            return _resolution_response(completed, replay=True)
        existing_claim = await self.repository.get_event(
            _resolution_claim_id(decision_id),
            tenant_id=tenant_id,
            user_id=user_id,
        )
        try:
            operation, candidate = _pending_payload(pending)
        except MemoryManagementConflict:
            if existing_claim is None or action != "confirm":
                raise
            _validate_resolution_claim(
                existing_claim,
                action=action,
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                admin=admin,
            )
            raw_accepted = existing_claim.payload.get("accepted_operation")
            if not isinstance(raw_accepted, dict):
                raise MemoryManagementConflict("Pending decision payload is unavailable") from None
            try:
                accepted = MemoryLifecycleOperation.model_validate(raw_accepted)
            except ValueError as exc:
                raise MemoryManagementConflict("Pending decision payload is unavailable") from exc
            if accepted.operation != MemoryOperation.DELETE:
                raise MemoryManagementConflict("Pending decision payload is unavailable") from None
            result = await self.lifecycle.request_delete(accepted)
            index = result.index_operation
            return await self._complete_resolution(
                pending=pending,
                action=action,
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                admin=admin,
                operation_id=accepted.operation_id,
                index_operation_id=index.index_operation_id if index else None,
                provider_status=index.status.value if index else None,
                memory_id=accepted.memory_id,
            )
        accepted = None
        if action == "confirm":
            if candidate.proposed_operation.value not in {"update", "delete"}:
                raise MemoryManagementConflict("Pending decision is not confirmable")
            if not operation.memory_id:
                raise MemoryManagementConflict("Pending decision target is unavailable")
            item = await self.repository.get_by_id(operation.memory_id, tenant_id=tenant_id)
            if item is None or not _owned(item, user_id=user_id, admin=admin):
                raise MemoryManagementNotFound("Memory operation target not found")
            if expected_revision_id is None:
                raise MemoryManagementConflict("expected_revision_id is required")
            _check_revision(item.current_revision_id, expected_revision_id)
            if operation.revision_id != expected_revision_id:
                raise MemoryManagementConflict("Pending decision precondition changed")
            target_operation = (
                MemoryOperation.UPDATE
                if candidate.proposed_operation.value == "update"
                else MemoryOperation.DELETE
            )
            accepted = operation.model_copy(
                update={
                    "operation_id": _stable_id("mfop", f"resolve\x1f{decision_id}\x1f{action}"),
                    "operation": target_operation,
                    "decision_status": MemoryDecisionStatus.ACCEPTED,
                    "reason_code": (
                        MemoryFormationReasonCode.ACCEPTED_UPDATE
                        if target_operation == MemoryOperation.UPDATE
                        else MemoryFormationReasonCode.AUTHORIZED_DELETE
                    ),
                    "source": "admin_management" if admin else "user_management",
                    "metadata": {
                        "actor": actor,
                        "reason": redact_text(reason, max_length=500),
                        "decision_id": decision_id,
                    },
                }
            )
        claim = await self._claim_resolution(
            pending=pending,
            action=action,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            admin=admin,
            accepted_operation=accepted,
        )
        _validate_resolution_claim(
            claim,
            action=action,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            admin=admin,
        )
        if action == "reject":
            return await self._complete_resolution(
                pending=pending,
                action=action,
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                admin=admin,
                operation_id=claim.event_id,
                index_operation_id=None,
                provider_status=None,
                memory_id=operation.memory_id,
            )
        if accepted is None:
            raise MemoryManagementConflict("Pending decision payload is unavailable")
        try:
            result = await self.lifecycle.apply(
                CandidatePolicyResult(
                    candidate=candidate,
                    operation=accepted,
                    redacted_trace={"decision_id": decision_id, "resolved": True},
                )
            )
        except ValueError as exc:
            await self._complete_resolution(
                pending=pending,
                action="conflict",
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                admin=admin,
                operation_id=accepted.operation_id,
                index_operation_id=None,
                provider_status=None,
                memory_id=accepted.memory_id,
            )
            raise MemoryManagementConflict("Pending decision precondition changed") from exc
        index = result.index_operation
        return await self._complete_resolution(
            pending=pending,
            action=action,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            admin=admin,
            operation_id=accepted.operation_id,
            index_operation_id=index.index_operation_id if index else None,
            provider_status=index.status.value if index else None,
            memory_id=accepted.memory_id,
        )

    async def operation_status(
        self,
        *,
        index_operation_id: str,
        tenant_id: str,
        user_id: str,
        admin: bool = False,
    ) -> MemoryManagementOperationResponse:
        operation = await self.index_repository.get(index_operation_id, tenant_id=tenant_id)
        if operation is None:
            raise MemoryManagementNotFound("Memory operation target not found")
        events = await self.repository.list_events(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_id=operation.memory_id,
            limit=1,
        )
        if not events or events[0].user_id != user_id:
            raise MemoryManagementNotFound("Memory operation target not found")
        return MemoryManagementOperationResponse(
            operation_id=index_operation_id,
            operation=operation.operation.value,
            status=operation.status.value,
            memory_id=operation.memory_id,
            index_operation_id=index_operation_id,
            provider_status=operation.status.value,
        )

    async def _completed_delete_replay(
        self,
        *,
        memory_id,
        operation_id,
        tenant_id,
        user_id,
        actor,
        reason,
        idempotency_key,
        expected_revision_id,
        admin,
    ) -> MemoryManagementOperationResponse:
        audit = await self.repository.get_event(
            _audit_event_id(operation_id), tenant_id=tenant_id, user_id=user_id
        )
        if audit is None or audit.memory_id != memory_id:
            raise MemoryManagementNotFound("Memory operation target not found")
        _validate_management_audit(
            audit,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_revision_id=expected_revision_id,
            admin=admin,
        )
        index_operation_id = audit.payload.get("index_operation_id")
        if not isinstance(index_operation_id, str):
            raise MemoryManagementConflict("Memory operation replay state is unavailable")
        index = await self.index_repository.get(index_operation_id, tenant_id=tenant_id)
        if index is None or index.memory_id != memory_id or index.operation != "delete":
            raise MemoryManagementConflict("Memory operation replay state is unavailable")
        if index.revision_id != audit.payload.get("target_revision_id"):
            raise MemoryManagementConflict("Idempotency key payload changed")
        return MemoryManagementOperationResponse(
            operation_id=operation_id,
            operation="delete",
            status=index.status.value,
            memory_id=memory_id,
            index_operation_id=index.index_operation_id,
            provider_status=index.status.value,
            idempotent_replay=True,
        )

    async def _claim_resolution(
        self,
        *,
        pending,
        action,
        actor,
        reason,
        idempotency_key,
        admin,
        accepted_operation,
    ):
        payload = {
            "decision_id": pending.event_id,
            "action": action,
            "actor": actor,
            "reason_hash": _hash_ref(reason),
            "admin": admin,
            "idempotency_hash": _hash_ref(idempotency_key),
        }
        if accepted_operation is not None:
            payload["accepted_operation"] = accepted_operation.model_dump(mode="json")
        event = MemoryEvent(
            event_id=_resolution_claim_id(pending.event_id),
            event_type="memory_pending_resolution_claimed",
            memory_id=pending.memory_id,
            user_id=pending.user_id,
            tenant_id=pending.tenant_id,
            agent_id=pending.agent_id,
            request_id=pending.request_id,
            session_id=pending.session_id,
            turn_id=pending.turn_id,
            run_id=pending.run_id,
            formation_job_id=pending.formation_job_id,
            memory_key=pending.memory_key,
            decision_id=pending.event_id,
            scope=pending.scope,
            payload=payload,
        )
        try:
            return await self.repository.add_event(event)
        except ValueError:
            existing = await self.repository.get_event(
                event.event_id,
                tenant_id=pending.tenant_id,
                user_id=pending.user_id,
            )
            if existing is None:
                raise
            return existing

    async def _complete_resolution(
        self,
        *,
        pending,
        action,
        actor,
        reason,
        idempotency_key,
        admin,
        operation_id,
        index_operation_id,
        provider_status,
        memory_id,
    ) -> MemoryManagementOperationResponse:
        event = MemoryEvent(
            event_id=_resolution_complete_id(pending.event_id),
            event_type=f"memory_pending_{action}",
            memory_id=memory_id,
            user_id=pending.user_id,
            tenant_id=pending.tenant_id,
            agent_id=pending.agent_id,
            request_id=pending.request_id,
            session_id=pending.session_id,
            turn_id=pending.turn_id,
            run_id=pending.run_id,
            formation_job_id=pending.formation_job_id,
            memory_key=pending.memory_key,
            decision_status="resolved",
            decision_id=pending.event_id,
            scope=pending.scope,
            payload={
                "decision_id": pending.event_id,
                "action": action,
                "actor": actor,
                "reason": redact_text(reason, max_length=500),
                "admin": admin,
                "idempotency_hash": _hash_ref(idempotency_key),
                "operation_id": operation_id,
                "index_operation_id": index_operation_id,
                "provider_status": provider_status,
            },
        )
        stored = await self.repository.add_event(event)
        return _resolution_response(stored, replay=False)

    async def _resolution_event(self, decision_id, *, tenant_id, user_id):
        return await self.repository.get_event(
            _resolution_complete_id(decision_id), tenant_id=tenant_id, user_id=user_id
        )

    async def _audit(
        self,
        *,
        operation,
        action,
        actor,
        target_user_id,
        reason,
        idempotency_key,
        expected_revision_id,
        admin,
    ) -> None:
        await self.repository.add_event(
            MemoryEvent(
                event_id=_stable_id("mevt", f"audit\x1f{operation.operation_id}"),
                event_type="memory_management_audit",
                memory_id=operation.memory_id,
                user_id=target_user_id,
                tenant_id=operation.tenant_id,
                agent_id=operation.agent_id,
                formation_job_id=operation.formation_job_id,
                memory_key=operation.memory_key,
                decision_status="accepted",
                payload={
                    "action": action,
                    "actor": actor,
                    "target_user_id": target_user_id,
                    "reason": redact_text(reason, max_length=500),
                    "admin": admin,
                    "idempotency_hash": _hash_ref(idempotency_key),
                    "operation_id": operation.operation_id,
                    "index_operation_id": _delete_index_operation_id(operation),
                    "expected_revision_id": expected_revision_id,
                    "target_revision_id": operation.revision_id,
                },
            )
        )


def _pending_payload(event) -> tuple[MemoryLifecycleOperation, MemoryFormationCandidate]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    raw_operation = payload.get("operation")
    raw_candidate = payload.get("pending_candidate")
    if not isinstance(raw_operation, dict) or not isinstance(raw_candidate, dict):
        raise MemoryManagementConflict("Pending decision payload is unavailable")
    try:
        return (
            MemoryLifecycleOperation.model_validate(raw_operation),
            MemoryFormationCandidate.model_validate(raw_candidate),
        )
    except ValueError as exc:
        raise MemoryManagementConflict("Pending decision payload is unavailable") from exc


def _validate_resolution_claim(
    claim,
    *,
    action: str,
    actor: str,
    reason: str,
    idempotency_key: str,
    admin: bool,
) -> None:
    expected = {
        "action": action,
        "actor": actor,
        "reason_hash": _hash_ref(reason),
        "admin": admin,
        "idempotency_hash": _hash_ref(idempotency_key),
    }
    payload = claim.payload if isinstance(claim.payload, dict) else {}
    if any(payload.get(key) != value for key, value in expected.items()):
        raise MemoryManagementConflict("Pending decision is already resolving")


def _owned(item, *, user_id: str, admin: bool) -> bool:
    if admin:
        return item.user_id == user_id
    return item.user_id == user_id and item.subject_type == "user" and item.subject_id == user_id


def _check_revision(current: str | None, expected: str | None) -> None:
    if expected is not None and current != expected:
        raise MemoryManagementConflict("Memory operation precondition changed")


def _resolution_response(event, *, replay: bool) -> MemoryManagementOperationResponse:
    payload = event.payload if isinstance(event.payload, dict) else {}
    action = str(payload.get("action") or "resolved")
    return MemoryManagementOperationResponse(
        operation_id=str(payload.get("operation_id") or event.event_id),
        operation=action,
        status="conflict" if action == "conflict" else "completed",
        memory_id=event.memory_id,
        decision_id=str(payload.get("decision_id") or "") or None,
        index_operation_id=payload.get("index_operation_id"),
        provider_status=payload.get("provider_status"),
        idempotent_replay=replay,
    )


def _resolution_claim_id(decision_id: str) -> str:
    return _stable_id("mevt", f"pending-resolution-claim\x1f{decision_id}")


def _audit_event_id(operation_id: str) -> str:
    return _stable_id("mevt", f"audit\x1f{operation_id}")


def _delete_index_operation_id(operation: MemoryLifecycleOperation) -> str:
    identity = f"{operation.operation_id}:delete:{operation.revision_id or 'none'}"
    return _stable_id("midxop", identity)


def _validate_management_audit(
    event,
    *,
    actor: str,
    reason: str,
    idempotency_key: str,
    expected_revision_id: str | None,
    admin: bool,
) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    expected = {
        "action": "delete",
        "actor": actor,
        "reason": redact_text(reason, max_length=500),
        "admin": admin,
        "idempotency_hash": _hash_ref(idempotency_key),
        "expected_revision_id": expected_revision_id,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise MemoryManagementConflict("Idempotency key payload changed")


def _resolution_complete_id(decision_id: str) -> str:
    return _stable_id("mevt", f"pending-resolution-complete\x1f{decision_id}")


def _hash_ref(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:32]}"
