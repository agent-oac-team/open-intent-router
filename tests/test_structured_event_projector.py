from datetime import UTC, datetime

import pytest

from app.schemas.logs import AgentResult, AgentRun
from app.schemas.plans import Plan, PlanStep
from app.services.structured_event_projector import StructuredEventProjector

_LAST_ACTIVITY = datetime(2026, 7, 13, 12, 30, tzinfo=UTC)


def _plan() -> Plan:
    return Plan(
        plan_id="plan_1",
        user_id="user_1",
        tenant_id="tenant_1",
        session_id="session_1",
        status="pending",
        current_step_id="step_1",
        steps=[
            PlanStep(
                step_id="step_1",
                agent_id="agent_1",
                description="Collect source material",
                status="pending",
            ),
            PlanStep(
                step_id="step_2",
                agent_id="agent_2",
                description="Write the report",
                status="pending",
                depends_on=["step_1"],
            ),
        ],
    )


def _run(status: str = "completed") -> AgentRun:
    return AgentRun(
        run_id="run_1",
        request_id="request_1",
        session_id="session_1",
        agent_id="agent_1",
        user_id="user_1",
        tenant_id="tenant_1",
        plan_id="plan_1",
        step_id="step_1",
        status=status,
        invoker_type="mock",
        created_at=datetime(2026, 7, 13, tzinfo=UTC),
    )


def _result(**updates) -> AgentResult:
    values = {
        "result_id": "result_1",
        "run_id": "run_1",
        "session_id": "session_1",
        "agent_id": "agent_1",
        "user_id": "user_1",
        "tenant_id": "tenant_1",
        "plan_id": "plan_1",
        "step_id": "step_1",
        "status": "completed",
        "output": {"summary": "z" * 5000, "secret": "must-not-be-artifact-metadata"},
        "artifact_refs": [
            {
                "artifact_id": "artifact_1",
                "type": "report",
                "uri": "https://example.test/report?token=secret#section",
                "title": "Quarterly report",
                "metadata": {"secret": "not-projected"},
            }
        ],
    }
    values.update(updates)
    return AgentResult.model_validate(values)


def test_plan_projection_replay_is_stable_and_owner_scoped() -> None:
    projector = StructuredEventProjector()
    first = projector.project_plan(
        _plan(),
        event_type="create",
        event_id="event_1",
        source_version="1",
        last_activity_at=_LAST_ACTIVITY,
    )
    replay = projector.project_plan(
        _plan(),
        event_type="create",
        event_id="event_1",
        source_version="1",
        last_activity_at=_LAST_ACTIVITY,
    )

    assert replay == first
    assert first.tenant_id == "tenant_1"
    assert first.user_id == "user_1"
    assert first.candidates[0].memory_key_hint == (
        "tenant:tenant_1:user:user_1:plan:plan_1:task_status"
    )
    assert first.candidates[0].proposed_operation == "add"
    assert first.candidates[0].semantic.target == "task"
    assert first.candidates[0].semantic.slot == "task_status"
    assert first.candidates[0].semantic.value == "pending"
    assert first.candidates[0].semantic.temporal_scope == "canonical"
    assert first.candidates[0].structured_value["current_step"]["step_id"] == "step_1"
    assert first.candidates[0].structured_value["next_step"] is None
    assert first.candidates[0].structured_value["last_activity_at"] == ("2026-07-13T12:30:00+00:00")
    assert "Collect source material" in first.candidates[0].content


def test_plan_projection_tracks_confirm_progress_cancel_and_terminal_state() -> None:
    projector = StructuredEventProjector()
    running = _plan().model_copy(update={"status": "running"})
    confirmed = projector.project_plan(
        running,
        event_type="confirm",
        event_id="event_2",
        source_version="2",
        last_activity_at=_LAST_ACTIVITY,
    )
    progressed_plan = running.model_copy(
        update={
            "steps": [
                running.steps[0].model_copy(update={"status": "completed"}),
                running.steps[1].model_copy(update={"status": "pending"}),
            ],
            "current_step_id": "step_2",
        }
    )
    progressed = projector.project_plan(
        progressed_plan,
        event_type="update",
        event_id="event_3",
        source_version="3",
        last_activity_at=_LAST_ACTIVITY,
    )
    cancelled_plan = progressed_plan.model_copy(update={"status": "cancelled"})
    cancelled = projector.project_plan(
        cancelled_plan,
        event_type="cancel",
        event_id="event_4",
        source_version="4",
        last_activity_at=_LAST_ACTIVITY,
    )

    assert confirmed.candidates[0].proposed_operation == "update"
    assert progressed.candidates[0].structured_value["current_step"]["step_id"] == "step_2"
    assert cancelled.candidates[0].structured_value["status"] == "cancelled"
    assert cancelled.candidates[0].structured_value["next_step"] is None


def test_plan_projection_rejects_unowned_or_inconsistent_canonical_plan() -> None:
    projector = StructuredEventProjector()
    unowned = _plan().model_copy(update={"tenant_id": ""})

    with pytest.raises(ValueError, match="stored tenant/user ownership"):
        projector.project_plan(
            unowned,
            event_type="create",
            event_id="event_1",
            source_version="1",
            last_activity_at=_LAST_ACTIVITY,
        )
    with pytest.raises(ValueError, match="status=cancelled"):
        projector.project_plan(
            _plan(),
            event_type="cancel",
            event_id="event_2",
            source_version="2",
            last_activity_at=_LAST_ACTIVITY,
        )


def test_model_summary_cannot_override_plan_canonical_authority() -> None:
    projection = StructuredEventProjector().project_plan(
        _plan(),
        event_type="create",
        event_id="event_1",
        source_version="1",
        last_activity_at=_LAST_ACTIVITY,
        model_summary={
            "summary": "Readable task summary",
            "plan_id": "forged-plan",
            "status": "completed",
            "tenant_id": "forged-tenant",
            "user_id": "forged-user",
            "current_step": {"step_id": "forged-step"},
        },
    )

    value = projection.candidates[0].structured_value
    assert value["summary"] == "Readable task summary"
    assert value["plan_id"] == "plan_1"
    assert value["status"] == "pending"
    assert value["tenant_id"] == "tenant_1"
    assert value["user_id"] == "user_1"
    assert value["current_step"]["step_id"] == "step_1"


def test_projection_keys_bound_unicode_ids_and_candidate_ids_are_owner_scoped() -> None:
    projector = StructuredEventProjector()
    unicode_plan = _plan().model_copy(
        update={
            "plan_id": "计" * 128,
            "tenant_id": "租" * 128,
            "user_id": "户" * 128,
        }
    )
    first = projector.project_plan(
        unicode_plan,
        event_type="create",
        event_id="event_unicode",
        source_version="1",
        last_activity_at=_LAST_ACTIVITY,
    )
    other_owner = projector.project_plan(
        _plan().model_copy(update={"tenant_id": "tenant_2"}),
        event_type="create",
        event_id="event_other",
        source_version="1",
        last_activity_at=_LAST_ACTIVITY,
    )
    original_owner = projector.project_plan(
        _plan(),
        event_type="create",
        event_id="event_original",
        source_version="1",
        last_activity_at=_LAST_ACTIVITY,
    )

    assert len(first.candidates[0].memory_key_hint) <= 512
    assert "tenant-sha256-" in first.candidates[0].memory_key_hint
    assert other_owner.candidates[0].candidate_id != original_owner.candidates[0].candidate_id


def test_run_projection_covers_completion_failure_and_canonical_relationships() -> None:
    projector = StructuredEventProjector()
    completed = projector.project_run(
        _run(), event_type="complete", event_id="run_event_1", source_version="2"
    )
    failed = projector.project_run(
        _run("failed"),
        event_type="fail",
        event_id="run_event_2",
        source_version="3",
        model_summary={"summary": "Bounded failure", "plan_id": "forged"},
    )

    assert completed.candidates[0].structured_value["status"] == "completed"
    assert completed.candidates[0].semantic.value == "completed"
    assert completed.candidates[0].semantic.change_intent == "replace"
    assert failed.candidates[0].structured_value["status"] == "failed"
    assert failed.candidates[0].structured_value["plan_id"] == "plan_1"
    assert failed.candidates[0].structured_value["summary"] == "Bounded failure"
    with pytest.raises(ValueError, match="status=completed"):
        projector.project_run(
            _run("running"),
            event_type="complete",
            event_id="run_event_3",
            source_version="4",
        )


def test_result_and_artifact_projection_is_bounded_normalized_and_authoritative() -> None:
    projection = StructuredEventProjector().project_result(
        _result(), run=_run(), event_id="result_event_1", source_version="1"
    )

    assert len(projection.candidates) == 2
    result_candidate, artifact_candidate = projection.candidates
    assert len(result_candidate.content) <= 1000
    assert result_candidate.structured_value["run_id"] == "run_1"
    assert result_candidate.structured_value["tenant_id"] == "tenant_1"
    assert artifact_candidate.scope == "artifact_reference"
    assert artifact_candidate.semantic.target == "artifact"
    assert artifact_candidate.semantic.slot == "reference"
    assert artifact_candidate.semantic.value == {
        "artifact_id": "artifact_1",
        "uri": "https://example.test/report",
    }
    assert artifact_candidate.memory_key_hint == (
        "tenant:tenant_1:user:user_1:artifact:artifact_1:reference"
    )
    assert artifact_candidate.structured_value["uri"] == "https://example.test/report"
    assert "metadata" not in artifact_candidate.structured_value
    assert "not-projected" not in artifact_candidate.model_dump_json()
    assert "must-not-be-artifact-metadata" not in result_candidate.model_dump_json()


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("/download?token=secret#part", "/download"),
        ("file:///tmp/report?sig=secret#part", "file:///tmp/report"),
        ("https://user:password@example.test/report?token=secret", "https://example.test/report"),
    ],
)
def test_artifact_uri_removes_query_fragment_and_userinfo(uri: str, expected: str) -> None:
    result = _result(
        artifact_refs=[
            {
                "artifact_id": "artifact_1",
                "type": "report",
                "uri": uri,
            }
        ]
    )

    projection = StructuredEventProjector().project_result(
        result, run=_run(), event_id="result_event_1", source_version="1"
    )

    assert projection.candidates[1].structured_value["uri"] == expected
    assert "secret" not in projection.candidates[1].model_dump_json()


def test_artifact_projection_rejects_oversized_canonical_id() -> None:
    result = _result(
        artifact_refs=[
            {
                "artifact_id": "文" * 129,
                "type": "report",
                "uri": "https://example.test/report",
            }
        ]
    )

    with pytest.raises(ValueError, match="artifact_id"):
        StructuredEventProjector().project_result(
            result, run=_run(), event_id="result_event_1", source_version="1"
        )


def test_result_projection_rejects_relationship_not_owned_by_canonical_run() -> None:
    projector = StructuredEventProjector()

    with pytest.raises(ValueError, match="plan relationship"):
        projector.project_result(
            _result(plan_id="forged-plan"),
            run=_run(),
            event_id="result_event_1",
            source_version="1",
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"user_id": "other-user"},
        {"tenant_id": "other-tenant"},
        {"user_id": None},
        {"tenant_id": None},
    ],
)
def test_result_projection_rejects_missing_or_cross_owner(updates) -> None:
    with pytest.raises(ValueError, match="canonical Run"):
        StructuredEventProjector().project_result(
            _result(**updates),
            run=_run(),
            event_id="result_event_owner",
            source_version="1",
        )


def test_result_projection_replay_and_version_are_deterministic() -> None:
    projector = StructuredEventProjector()
    first = projector.project_result(
        _result(), run=_run(), event_id="result_event_1", source_version="1"
    )
    replay = projector.project_result(
        _result(), run=_run(), event_id="result_event_1", source_version="1"
    )
    changed = projector.project_result(
        _result(), run=_run(), event_id="result_event_2", source_version="2"
    )

    assert replay == first
    assert changed.idempotency_key != first.idempotency_key
    assert changed.candidates[0].candidate_id != first.candidates[0].candidate_id
