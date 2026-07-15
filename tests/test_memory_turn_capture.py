from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.schemas.agent_context import MemoryContext, MemoryContextItem
from app.schemas.common import ArtifactRef, ErrorDetail, UserContext
from app.schemas.invocation import AgentInvocation, AgentInvocationResult
from app.services.memory_formation import (
    FormationTriggerCoordinator,
    TurnCapsuleBuilder,
    TurnCaptureService,
)


def _settings(**updates) -> Settings:
    values = {
        "storage_backend": "memory",
        "memory_formation_mode": "observe",
        "memory_formation_capsule_user_chars": 20,
        "memory_formation_capsule_assistant_chars": 40,
        "memory_formation_capsule_summary_chars": 16,
    }
    return Settings(**{**values, **updates})


def _invocation(**updates) -> AgentInvocation:
    values = {
        "run_id": "run_1",
        "request_id": "request_1",
        "session_id": "session_1",
        "agent_id": "agent_1",
        "user": UserContext(id="u1", attributes={"tenant_id": "t1"}),
        "input": {"text": "请帮我长期记住偏好"},
        "context": {"plan_id": "plan_1"},
    }
    return AgentInvocation(**{**values, **updates})


def _result(**updates) -> AgentInvocationResult:
    values = {
        "run_id": "run_1",
        "agent_id": "agent_1",
        "status": "completed",
        "message": "已完成处理",
        "output": {"summary": "结果摘要"},
    }
    return AgentInvocationResult(**{**values, **updates})


def test_turn_capsule_builder_bounds_text_and_keeps_only_canonical_refs() -> None:
    settings = _settings()
    memory_items = [
        MemoryContextItem(
            memory_id=f"mem_{index}",
            scope="stable_fact",
            content=f"private-memory-content-{index}",
        )
        for index in range(60)
    ]
    artifacts = [
        ArtifactRef(
            artifact_id=f"artifact_{index}",
            uri=f"https://private.example/{index}",
            title=f"private-title-{index}",
        )
        for index in range(60)
    ]
    invocation = _invocation(
        input={"text": "用中文回答" * 20},
        memory_context=MemoryContext(status="ok", items=memory_items, summary="private-summary"),
    )
    result = _result(
        message="完成" * 20,
        output={"large": "x" * 500},
        artifact_refs=artifacts,
    )

    capsule = TurnCapsuleBuilder(settings).build(
        invocation=invocation,
        result=result,
        result_id="result_1",
        completed_at=datetime(2026, 7, 13, tzinfo=UTC),
    )

    assert len(capsule.user_text) == settings.memory_formation_capsule_user_chars
    assert len(capsule.assistant_text) <= settings.memory_formation_capsule_assistant_chars
    assert capsule.result_refs == ["result_1"]
    assert capsule.plan_refs == ["plan_1"]
    assert capsule.artifact_refs == [f"artifact_{index}" for index in range(50)]
    assert capsule.used_memory_ids == [f"mem_{index}" for index in range(50)]
    serialized = capsule.model_dump_json()
    assert "private-memory-content" not in serialized
    assert "private-summary" not in serialized
    assert "private.example" not in serialized
    assert "private-title" not in serialized


def test_turn_capsule_hashes_oversized_refs_to_keep_total_payload_bounded() -> None:
    oversized = "ref_" + ("x" * 3_000_000)
    capsule = TurnCapsuleBuilder(_settings()).build(
        invocation=_invocation(
            context={"plan_id": oversized},
            memory_context=MemoryContext(
                status="ok",
                items=[
                    MemoryContextItem(
                        memory_id=oversized,
                        scope="stable_fact",
                        content="bounded",
                    )
                ],
            ),
        ),
        result=_result(
            artifact_refs=[ArtifactRef(artifact_id=oversized, uri="https://example.test")]
        ),
        result_id=oversized,
    )

    refs = [
        *capsule.result_refs,
        *capsule.plan_refs,
        *capsule.artifact_refs,
        *capsule.used_memory_ids,
    ]
    assert all(ref.startswith("sha256:") and len(ref) == 71 for ref in refs)
    serialized = capsule.model_dump_json()
    assert len(serialized) < 2_000
    assert oversized not in serialized


@pytest.mark.parametrize("status", ["failed", "invalid_output"])
def test_turn_capsule_builder_records_failure_status_without_error_message(status) -> None:
    capsule = TurnCapsuleBuilder(_settings()).build(
        invocation=_invocation(),
        result=_result(
            status=status,
            message="调用失败",
            output=None,
            error=ErrorDetail(code=status, message="secret-error-details"),
        ),
        result_id="result_1",
    )

    assert capsule.result_status == status
    assert f"error_code={status}" in capsule.assistant_text
    assert "secret-error-details" not in capsule.assistant_text


async def test_temporary_request_skips_buffer_and_records_only_redacted_event() -> None:
    settings = _settings()
    turns = MemoryFormationTurnJobRepository()
    events = MemoryItemRepository()
    service = TurnCaptureService(
        settings=settings,
        builder=TurnCapsuleBuilder(settings),
        coordinator=FormationTriggerCoordinator(settings=settings, repository=turns),
        event_repository=events,
    )
    invocation = _invocation(
        input={"text": "password=super-secret-value"},
        context={"memory_policy": {"mode": "private"}},
    )

    outcome = await service.capture(
        invocation=invocation,
        result=_result(),
        result_id="result_1",
    )

    assert outcome.status == "skipped"
    assert outcome.turn is None and outcome.job is None
    assert (
        await turns.list_pending_turns(tenant_id="t1", user_id="u1", session_id="session_1") == []
    )
    stored_events = await events.list_events(tenant_id="t1", user_id="u1")
    assert len(stored_events) == 1
    serialized = stored_events[0].model_dump_json()
    assert stored_events[0].payload["reason_code"] == "temporary_request"
    assert "super-secret-value" not in serialized


async def test_formation_mode_off_has_no_automatic_capture_or_trace_side_effect() -> None:
    settings = _settings(memory_formation_mode="off")
    turns = MemoryFormationTurnJobRepository()
    events = MemoryItemRepository()
    service = TurnCaptureService(
        settings=settings,
        builder=TurnCapsuleBuilder(settings),
        coordinator=FormationTriggerCoordinator(settings=settings, repository=turns),
        event_repository=events,
    )

    outcome = await service.capture(
        invocation=_invocation(context={"private": True}),
        result=_result(),
        result_id="result_1",
    )

    assert outcome.status == "off"
    assert turns.turns == {}
    assert events.events == []


def test_turn_capsule_requires_trusted_tenant_owner() -> None:
    with pytest.raises(ValueError, match="tenant ownership"):
        TurnCapsuleBuilder(_settings()).build(
            invocation=_invocation(user=UserContext(id="u1")),
            result=_result(),
            result_id="result_1",
        )


def test_turn_id_is_scoped_by_trusted_owner_and_session() -> None:
    builder = TurnCapsuleBuilder(_settings())
    first = builder.build(
        invocation=_invocation(),
        result=_result(),
        result_id="result_1",
    )
    second = builder.build(
        invocation=_invocation(
            user=UserContext(id="u2", attributes={"tenant_id": "t2"}),
            session_id="session_2",
        ),
        result=_result(),
        result_id="result_2",
    )

    assert first.request_id == second.request_id == "request_1"
    assert first.run_id == second.run_id == "run_1"
    assert first.turn_id != second.turn_id


@pytest.mark.parametrize(
    ("field", "invocation"),
    [
        ("request_id", _invocation(request_id="x" * 129)),
        ("session_id", _invocation(session_id="x" * 129)),
        ("run_id", _invocation(run_id="x" * 129)),
        ("agent_id", _invocation(agent_id="x" * 129)),
        ("user_id", _invocation(user=UserContext(id="x" * 129, attributes={"tenant_id": "t1"}))),
        (
            "tenant_id",
            _invocation(user=UserContext(id="u1", attributes={"tenant_id": "x" * 129})),
        ),
    ],
)
def test_turn_capsule_rejects_oversized_identity_fields(field, invocation) -> None:
    with pytest.raises(ValueError, match=field):
        TurnCapsuleBuilder(_settings()).build(
            invocation=invocation,
            result=_result(),
            result_id="result_1",
        )


def test_turn_capsule_accepts_128_character_identity_boundary() -> None:
    boundary = "x" * 128
    capsule = TurnCapsuleBuilder(_settings()).build(
        invocation=_invocation(
            request_id=boundary,
            session_id=boundary,
            run_id=boundary,
            agent_id=boundary,
            user=UserContext(id=boundary, attributes={"tenant_id": boundary}),
        ),
        result=_result(),
        result_id=boundary,
    )

    assert capsule.request_id == boundary
    assert capsule.session_id == boundary
    assert capsule.run_id == boundary
    assert capsule.user_id == boundary
    assert capsule.tenant_id == boundary
    assert capsule.agent_id == boundary
    assert capsule.result_refs == [boundary]
