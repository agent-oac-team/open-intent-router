"""OAC's Host-owned External Executor acceptance boundary."""

import hashlib
import hmac
import json
from collections.abc import Collection

from app.application import ExternalExecutionAcceptanceApplicationPort
from app.schemas.external_execution import (
    ExternalExecutionAcceptanceReservation,
    ExternalExecutorAcceptance,
    ExternalExecutorAcceptanceRequest,
)


class OacExternalExecutor:
    """Accept trusted OAC canonical External Execution bindings.

    The Adapter receives only a logical reference from Core. It deliberately does
    not return a provider endpoint, credential, or a replacement Handling value.
    OAC's eventual external dispatch continues through the existing opaque Ticket.
    """

    def __init__(
        self,
        *,
        supported_executor_refs: Collection[str] = (),
        acceptance_store: ExternalExecutionAcceptanceApplicationPort,
        acceptance_fingerprint_secret: str | None = None,
        healthy: bool = True,
    ) -> None:
        if supported_executor_refs and not acceptance_fingerprint_secret:
            raise ValueError("External Executor acceptance requires a durable fingerprint secret")
        self._healthy = healthy
        self._supported_executor_refs = frozenset(supported_executor_refs)
        self._acceptance_store = acceptance_store
        self._acceptance_fingerprint_secret = acceptance_fingerprint_secret

    def supports(self, executor_ref: str) -> bool:
        return executor_ref in self._supported_executor_refs

    async def accept(
        self,
        request: ExternalExecutorAcceptanceRequest,
    ) -> ExternalExecutorAcceptance:
        if not self.supports(request.executor_ref):
            return ExternalExecutorAcceptance(
                accepted=False,
                reason_code="external_executor_unsupported",
            )
        if request.principal.tenant_id != "oac":
            result = ExternalExecutorAcceptance(
                accepted=False,
                reason_code="external_executor_unauthorized",
            )
        elif not self._healthy:
            result = ExternalExecutorAcceptance(
                accepted=False,
                reason_code="external_executor_unhealthy",
            )
        else:
            secret = self._acceptance_fingerprint_secret
            if secret is None:  # defensive against an invalid mutable test double
                return ExternalExecutorAcceptance(
                    accepted=False,
                    reason_code="external_executor_unhealthy",
                )
            reservation = ExternalExecutionAcceptanceReservation(
                acceptance_id=request.acceptance_id,
                request_fingerprint=_acceptance_request_fingerprint(request, secret=secret),
                executor_ref=request.executor_ref,
            )
            try:
                await self._acceptance_store.record_accepted(reservation)
            except Exception:
                return ExternalExecutorAcceptance(
                    accepted=False,
                    reason_code="external_executor_unhealthy",
                )
            result = ExternalExecutorAcceptance(accepted=True, binding_id="oac_external_executor")
        return result


def _acceptance_request_fingerprint(
    request: ExternalExecutorAcceptanceRequest,
    *,
    secret: str,
) -> str:
    """Bind one durable acceptance ID to the exact safe request it accepted."""

    payload = {
        "executor_ref": request.executor_ref,
        "agent_id": request.agent_id,
        "agent_revision": request.agent_revision,
        "tenant_id": request.principal.tenant_id,
        "user_id": request.principal.user_id,
        "roles": sorted(request.principal.roles),
        "groups": sorted(request.principal.groups),
        "entitlements": sorted(request.principal.entitlements),
        "params": request.params,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(
        secret.encode(),
        f"oir-oac-external-acceptance-v1:{encoded}".encode(),
        hashlib.sha256,
    ).hexdigest()
