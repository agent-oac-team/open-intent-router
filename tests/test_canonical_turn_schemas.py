from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.turns import (
    CanonicalTurn,
    TurnCapsule,
    TurnSemanticResponse,
    TurnStatus,
    TurnUserInput,
)


def _completed_turn() -> CanonicalTurn:
    now = datetime.now(UTC)
    return CanonicalTurn(
        turn_id="turn-1",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host",
        status=TurnStatus.COMPLETED,
        user_input=TurnUserInput(text="summarize this"),
        final_response=TurnSemanticResponse(kind="reply", text="summary"),
        created_at=now,
        updated_at=now,
        completed_at=now,
    )


def test_completed_canonical_turn_can_project_to_turn_capsule() -> None:
    turn = _completed_turn()

    capsule = TurnCapsule(
        turn_id=turn.turn_id,
        tenant_id=turn.tenant_id,
        user_id=turn.user_id,
        session_id=turn.session_id,
        request_id=turn.request_id,
        source=turn.source,
        state_version=turn.state_version,
        user_input=turn.user_input,
        final_response=turn.final_response,
        references=turn.references,
        completed_at=turn.completed_at,
    )

    assert capsule.turn_id == "turn-1"
    assert capsule.final_response.text == "summary"


def test_completed_turn_requires_final_semantic_response() -> None:
    payload = _completed_turn().model_dump()
    payload["final_response"] = None

    with pytest.raises(ValidationError, match="final semantic response"):
        CanonicalTurn.model_validate(payload)


def test_active_turn_cannot_claim_terminal_timestamp() -> None:
    payload = _completed_turn().model_dump()
    payload["status"] = "running"

    with pytest.raises(ValidationError, match="completed_at"):
        CanonicalTurn.model_validate(payload)


def test_canonical_turn_rejects_host_display_state() -> None:
    payload = _completed_turn().model_dump()
    payload["provider_session_id"] = "provider-session"
    payload["page_state"] = {"route": "/dashboard"}
    payload["messages"] = []

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CanonicalTurn.model_validate(payload)
