from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.application import DelegatedRunApplicationPort
from app.schemas.delegated_runs import (
    DelegatedRunCompleteCommand,
    DelegatedRunProgressCommand,
    DelegatedRunStartCommand,
)


def test_delegated_run_start_requires_complete_owner_and_deadline() -> None:
    command = DelegatedRunStartCommand(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        turn_id="turn-1",
        agent_id="agent-1",
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        input={"query": "run task"},
    )

    assert command.turn_id == "turn-1"
    assert command.plan_id is None


def test_delegated_progress_rejects_invalid_sequence_or_status() -> None:
    values = {
        "event_id": "event-1",
        "run_id": "run-1",
        "turn_id": "turn-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "expected_state_version": 1,
        "occurred_at": datetime.now(UTC),
        "sequence": 0,
    }

    with pytest.raises(ValidationError):
        DelegatedRunProgressCommand.model_validate(values)
    with pytest.raises(ValidationError):
        DelegatedRunProgressCommand.model_validate({**values, "sequence": 1, "status": "completed"})


def test_delegated_complete_requires_result_identity() -> None:
    with pytest.raises(ValidationError):
        DelegatedRunCompleteCommand.model_validate(
            {
                "event_id": "event-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "tenant_id": "tenant-1",
                "user_id": "user-1",
                "agent_id": "agent-1",
                "expected_state_version": 1,
                "occurred_at": datetime.now(UTC),
                "response_text": "done",
            }
        )


def test_delegated_run_port_is_runtime_checkable() -> None:
    assert not isinstance(object(), DelegatedRunApplicationPort)
