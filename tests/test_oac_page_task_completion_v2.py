import asyncio
import hashlib
import hmac

from fastapi import FastAPI

from app.repositories.memory import MemoryPlanRepository
from app.schemas.plans import NextAction, Plan, PlanStep
from app.services.plan_service import PlanService
from host_adapters.oac.api.central import router
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity import HostIdentityVerifier, SignedHostRequest
from host_adapters.oac.identity.canonical import canonicalize_host_request
from host_adapters.oac.repositories.nonces import MemoryNonceStore
from host_apps.oac.dependencies import (
    get_host_identity_verifier,
    get_oac_adapter_application_ports,
)

_NOW = 1784170800
_PATH = "/api/v1/central/plans/plan-v2/steps/step-v2/page-task-completion"


def _signed_headers(
    body: bytes,
    *,
    key_id: str = "v2-user-key",
    secret: str = "v2-user-secret",
    nonce: str,
    timestamp: int = _NOW,
    user_id: str = "trusted-user",
    credential_class: str = "oac_user",
) -> dict[str, str]:
    values = {
        "principal_type": "user",
        "claims_version": "oac-principal-v1",
        "roles": "operator",
        "active_bundle_id": "oac-operations",
        "policy_version": "oac-authz-v1",
    }
    if credential_class == "coze_workflow":
        values = {
            "principal_type": "service",
            "claims_version": "oac-service-principal-v1",
            "roles": "",
            "active_bundle_id": "",
            "policy_version": "oac-readonly-v1",
        }
        user_id = "coze-workflow"
    signed = SignedHostRequest(
        method="POST",
        path=_PATH,
        query="",
        body=body,
        key_id=key_id,
        audience="oac-oir-adapter-local",
        timestamp=str(timestamp),
        nonce=nonce,
        content_sha256=hashlib.sha256(body).hexdigest(),
        user_id=user_id,
        credential_class=credential_class,
        signature="",
        groups="",
        **values,
    )
    signature = hmac.new(
        secret.encode(),
        canonicalize_host_request(signed, tenant_id="oac").encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-OIR-Host-Key-Id": signed.key_id,
        "X-OIR-Host-Audience": signed.audience,
        "X-OIR-Host-Timestamp": signed.timestamp,
        "X-OIR-Host-Nonce": signed.nonce,
        "X-OIR-Host-Content-SHA256": signed.content_sha256,
        "X-OIR-Host-Principal-Type": signed.principal_type,
        "X-OIR-Host-User-Id": signed.user_id,
        "X-OIR-Host-Groups": signed.groups,
        "X-OIR-Host-Credential-Class": signed.credential_class,
        "X-OIR-Host-Claims-Version": signed.claims_version,
        "X-OIR-Host-Roles": signed.roles,
        "X-OIR-Host-Active-Bundle-Id": signed.active_bundle_id,
        "X-OIR-Host-Policy-Version": signed.policy_version,
        "X-OIR-Host-Signature": f"v2={signature}",
    }


def _page_task_body(expected_state_version: int) -> bytes:
    return (
        "{"
        '"request_id":"page-task-v2-1",'
        '"session_id":"session-v2",'
        '"agent_id":"page-agent",'
        f'"expected_state_version":{expected_state_version}'
        "}"
    ).encode()


def _v2_client(non_lifespan_test_client):
    plans = PlanService(MemoryPlanRepository())
    created = asyncio.run(
        plans.save_plan(
            Plan(
                plan_id="plan-v2",
                tenant_id="oac",
                user_id="trusted-user",
                session_id="session-v2",
                status="blocked",
                current_step_id="step-v2",
                next_action=NextAction(
                    type="open_ui",
                    plan_id="plan-v2",
                    step_id="step-v2",
                    agent_id="page-agent",
                    route="/page-task",
                ),
                steps=[
                    PlanStep(
                        step_id="step-v2",
                        agent_id="page-agent",
                        description="finish the page task",
                        status="blocked",
                    ),
                    PlanStep(
                        step_id="step-next",
                        agent_id="next-agent",
                        description="continue the plan",
                    ),
                ],
            ),
            publish=False,
        )
    )
    verifier = HostIdentityVerifier(
        audience="oac-oir-adapter-local",
        tenant_id="oac",
        keys={
            "v2-user-key": "v2-user-secret",
            "v2-coze-key": "v2-coze-secret",
        },
        key_credential_classes={
            "v2-user-key": frozenset({"oac_user"}),
            "v2-coze-key": frozenset({"coze_workflow"}),
        },
        nonce_store=MemoryNonceStore(),
        now=lambda: _NOW,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: (
        OacAdapterApplicationPorts(
            routing=object(),
            registry=object(),
            events=object(),
            plans=plans,
            delegated_runs=object(),
            turns=object(),
        )
    )
    app.dependency_overrides[get_host_identity_verifier] = lambda: verifier
    return non_lifespan_test_client(app), created


def test_page_task_completion_v2_verifies_the_full_host_boundary(
    non_lifespan_test_client,
) -> None:
    """The real V2 dependency gates this runtime write before Plan mutation.

    Durable concurrent / restart / commit-acknowledgement-loss behavior is
    exercised against DatabasePlanRepository in test_plan_confirm_concurrency.
    """

    client, plan = _v2_client(non_lifespan_test_client)
    body = _page_task_body(plan.state_version)
    accepted_headers = _signed_headers(
        body,
        nonce="bm9uY2UtcGFnZS10YXNrLXYyLWFjY2VwdGVkLTAwMQ",
    )

    accepted = client.post(_PATH, content=body, headers=accepted_headers)
    assert accepted.status_code == 200, accepted.json()
    assert accepted.json()["duplicate"] is False
    assert accepted.json()["conflict"] is False
    assert accepted.json()["plan"]["current_step"] == "step-next"

    transport_replay = client.post(_PATH, content=body, headers=accepted_headers)
    assert transport_replay.status_code == 401
    assert transport_replay.json()["detail"] == "host_authentication_failed"

    semantic_replay = client.post(
        _PATH,
        content=body,
        headers=_signed_headers(
            body,
            nonce="bm9uY2UtcGFnZS10YXNrLXYyLXNlbWFudGljLTAwMg",
        ),
    )
    assert semantic_replay.status_code == 200, semantic_replay.json()
    assert semantic_replay.json()["duplicate"] is True
    assert semantic_replay.json()["plan"]["state_version"] == plan.state_version + 1

    bad_key = client.post(
        _PATH,
        content=body,
        headers=_signed_headers(
            body,
            key_id="unknown-v2-key",
            secret="ignored",
            nonce="bm9uY2UtcGFnZS10YXNrLXYyLWJhZC1rZXktMDAz",
        ),
    )
    assert bad_key.status_code == 401

    tampered_body = body.replace(b"page-agent", b"other-agent")
    tampered = client.post(
        _PATH,
        content=tampered_body,
        headers=_signed_headers(
            body,
            nonce="bm9uY2UtcGFnZS10YXNrLXYyLXRhbXBlci0wMDQ",
        ),
    )
    assert tampered.status_code == 401

    expired = client.post(
        _PATH,
        content=body,
        headers=_signed_headers(
            body,
            timestamp=_NOW - 61,
            nonce="bm9uY2UtcGFnZS10YXNrLXYyLWV4cGlyZWQtMDA1",
        ),
    )
    assert expired.status_code == 401

    readonly_profile = client.post(
        _PATH,
        content=body,
        headers=_signed_headers(
            body,
            key_id="v2-coze-key",
            secret="v2-coze-secret",
            credential_class="coze_workflow",
            nonce="bm9uY2UtcGFnZS10YXNrLXYyLWNvemUtMDA2",
        ),
    )
    assert readonly_profile.status_code == 403
    assert readonly_profile.json()["detail"] == "host_operation_forbidden"

    foreign_owner = client.post(
        _PATH,
        content=body,
        headers=_signed_headers(
            body,
            user_id="different-user",
            nonce="bm9uY2UtcGFnZS10YXNrLXYyLWZvcmVpZ24tMDA3",
        ),
    )
    assert foreign_owner.status_code == 404
    assert foreign_owner.json()["detail"]["code"] == "not_found"
