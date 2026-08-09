from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.turn_outbox import (
    DatabaseTurnOutboxRepository,
    MemoryTurnOutboxRepository,
    OutboxClaimConflict,
)
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.turns import TurnOutboxEvent, TurnUserInput
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def outbox_repository(request, tmp_path):
    now = datetime.now(UTC)
    if request.param == "memory":
        turns = TurnService(MemoryTurnRepository())
        repository = MemoryTurnOutboxRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'turn-outbox.db'}",
        )
        await create_all_tables(settings)
        factory = create_session_factory(settings)
        turns = TurnService(DatabaseTurnRepository(factory))
        repository = DatabaseTurnOutboxRepository(factory)
    started = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id=f"request-{request.param}",
        source="host",
        user_input=TurnUserInput(text="outbox test"),
    )
    event = TurnOutboxEvent(
        outbox_id=f"outbox-{request.param}",
        turn_id=started.turn.turn_id,
        event_type="turn.completed",
        idempotency_key=f"turn.completed:{started.turn.turn_id}",
        payload={"turn_id": started.turn.turn_id},
        max_attempts=2,
        available_at=now,
    )
    return repository, event, now


async def test_outbox_add_claim_and_complete_are_idempotent(outbox_repository) -> None:
    repository, event, now = outbox_repository
    first, created = await repository.add_idempotent(event)
    replay, replay_created = await repository.add_idempotent(
        event.model_copy(update={"outbox_id": "ignored"})
    )
    assert created is True and replay_created is False
    assert first.outbox_id == replay.outbox_id

    claimed = await repository.claim(owner="worker-1", now=now, lease_seconds=30)
    assert claimed and claimed.status == "claimed" and claimed.lease_token
    with pytest.raises(OutboxClaimConflict):
        await repository.complete(
            claimed.outbox_id,
            owner="worker-other",
            lease_token=claimed.lease_token,
            now=now,
        )
    completed = await repository.complete(
        claimed.outbox_id,
        owner="worker-1",
        lease_token=claimed.lease_token,
        now=now,
    )
    duplicate = await repository.complete(
        claimed.outbox_id,
        owner="worker-1",
        lease_token=claimed.lease_token,
        now=now,
    )
    assert completed.status == duplicate.status == "completed"
    assert await repository.claim(owner="worker-2", now=now, lease_seconds=30) is None


async def test_outbox_retry_dead_letter_and_expired_lease_recovery(outbox_repository) -> None:
    repository, event, now = outbox_repository
    await repository.add_idempotent(event)
    first = await repository.claim(owner="worker-1", now=now, lease_seconds=1)
    assert first and first.lease_token
    recovered = await repository.claim(
        owner="worker-2", now=now + timedelta(seconds=2), lease_seconds=30
    )
    assert recovered and recovered.lease_owner == "worker-2" and recovered.lease_token

    retry = await repository.fail(
        recovered.outbox_id,
        owner="worker-2",
        lease_token=recovered.lease_token,
        now=now + timedelta(seconds=2),
        retry_at=now + timedelta(seconds=3),
        error_code="provider_unavailable",
    )
    assert retry.status == "retry" and retry.attempt_count == 1
    claimed_again = await repository.claim(
        owner="worker-3", now=now + timedelta(seconds=3), lease_seconds=30
    )
    assert claimed_again and claimed_again.lease_token
    dead = await repository.fail(
        claimed_again.outbox_id,
        owner="worker-3",
        lease_token=claimed_again.lease_token,
        now=now + timedelta(seconds=3),
        retry_at=now + timedelta(seconds=4),
        error_code="provider_unavailable",
    )
    assert dead.status == "dead_letter" and dead.attempt_count == 2
    assert (
        await repository.claim(owner="worker-4", now=now + timedelta(seconds=10), lease_seconds=30)
        is None
    )
