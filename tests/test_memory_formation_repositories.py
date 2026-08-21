import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.repositories.memory_formation import (
    DatabaseMemoryFormationTurnJobRepository,
    MemoryFormationTurnJobRepository,
    formation_range_idempotency_key,
)
from app.schemas.memory import (
    MemoryFormationJobStatus,
    MemoryFormationTrigger,
    MemoryFormationTurn,
)


def _turn(index: int, *, session_id: str = "s1") -> MemoryFormationTurn:
    completed_at = datetime(2026, 7, 13, 0, 0, index, tzinfo=UTC)
    return MemoryFormationTurn(
        turn_id=f"turn_{index}",
        request_id=f"request_{index}",
        session_id=session_id,
        user_id="u1",
        tenant_id="t1",
        result_status="completed",
        idle_deadline_at=completed_at + timedelta(seconds=30),
        completed_at=completed_at,
    )


async def test_in_memory_repository_freezes_ranges_and_advances_only_success() -> None:
    repository = MemoryFormationTurnJobRepository()
    for index in range(1, 4):
        await repository.append_turn(_turn(index))

    job = await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.TURN_WINDOW,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
        max_turns=2,
    )
    assert job
    assert job.source_refs == ["turn_1", "turn_2"]
    assert [
        turn.turn_id
        for turn in await repository.list_pending_turns(
            tenant_id="t1", user_id="u1", session_id="s1"
        )
    ] == ["turn_3"]

    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claimed = await repository.claim_job(owner="worker-a", now=now, lease_seconds=60)
    assert claimed and claimed.lease_owner == "worker-a"
    failed = await repository.fail_job(
        claimed.job_id,
        owner="worker-a",
        lease_token=claimed.lease_token,
        now=now + timedelta(seconds=1),
        error_code="provider_timeout",
        next_attempt_at=now + timedelta(seconds=5),
    )
    assert failed.status == MemoryFormationJobStatus.RETRY
    assert (
        await repository.successful_watermark(tenant_id="t1", user_id="u1", session_id="s1") is None
    )

    reclaimed = await repository.claim_job(
        owner="worker-b", now=now + timedelta(seconds=5), lease_seconds=60
    )
    assert reclaimed and reclaimed.job_id == job.job_id
    await repository.complete_job(
        reclaimed.job_id,
        owner="worker-b",
        lease_token=reclaimed.lease_token,
        now=now + timedelta(seconds=6),
        trace_summary={"add": 1},
    )
    assert (
        await repository.successful_watermark(tenant_id="t1", user_id="u1", session_id="s1")
        == "turn_2"
    )


async def test_window_idle_race_and_expired_lease_have_single_claim() -> None:
    repository = MemoryFormationTurnJobRepository()
    await repository.append_turn(_turn(1))
    parameters = {
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "mode": "observe",
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
        "policy_version": "policy-v1",
    }
    window, idle = await asyncio.gather(
        repository.create_job_for_pending(trigger=MemoryFormationTrigger.TURN_WINDOW, **parameters),
        repository.create_job_for_pending(trigger=MemoryFormationTrigger.IDLE, **parameters),
    )
    assert len([job for job in (window, idle) if job is not None]) == 1
    assert len(repository.jobs) == 1

    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    first = await repository.claim_job(owner="worker-a", now=now, lease_seconds=10)
    duplicate = await repository.claim_job(owner="worker-b", now=now, lease_seconds=10)
    recovered = await repository.claim_job(
        owner="worker-b", now=now + timedelta(seconds=10), lease_seconds=10
    )
    assert first
    assert duplicate is None
    assert recovered and recovered.job_id == first.job_id
    assert recovered.attempt_count == 2


async def test_retry_reaches_dead_letter_without_releasing_turns() -> None:
    repository = MemoryFormationTurnJobRepository()
    await repository.append_turn(_turn(1))
    job = await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
        max_attempts=2,
    )
    assert job
    assert job.max_attempts == 2
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    for attempt in range(2):
        claimed = await repository.claim_job(owner=f"worker-{attempt}", now=now, lease_seconds=10)
        assert claimed
        failed = await repository.fail_job(
            job.job_id,
            owner=f"worker-{attempt}",
            lease_token=claimed.lease_token,
            now=now,
            error_code="invalid_output",
            next_attempt_at=now,
        )
    assert failed.status == MemoryFormationJobStatus.DEAD_LETTER
    assert repository.turns["turn_1"].status == "claimed"


async def test_stale_worker_cannot_complete_reclaimed_job() -> None:
    repository = MemoryFormationTurnJobRepository()
    await repository.append_turn(_turn(1))
    job = await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    first = await repository.claim_job(owner="worker-a", now=now, lease_seconds=10)
    second = await repository.claim_job(
        owner="worker-b", now=now + timedelta(seconds=10), lease_seconds=10
    )
    assert first and second

    with pytest.raises(ValueError, match="ownership changed"):
        await repository.complete_job(
            job.job_id,
            owner="worker-a",
            lease_token=first.lease_token,
            now=now + timedelta(seconds=11),
        )
    await repository.complete_job(
        job.job_id,
        owner="worker-b",
        lease_token=second.lease_token,
        now=now + timedelta(seconds=11),
    )


async def test_watermark_advances_only_across_contiguous_completed_ranges() -> None:
    repository = MemoryFormationTurnJobRepository()
    await repository.append_turn(_turn(1))
    await repository.append_turn(_turn(2))
    parameters = {
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "trigger": MemoryFormationTrigger.TURN_WINDOW,
        "mode": "observe",
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
        "policy_version": "policy-v1",
        "max_turns": 1,
    }
    first_job = await repository.create_job_for_pending(**parameters)
    second_job = await repository.create_job_for_pending(**parameters)
    assert first_job and second_job
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    first_claim = await repository.claim_job(owner="worker-a", now=now, lease_seconds=30)
    second_claim = await repository.claim_job(owner="worker-b", now=now, lease_seconds=30)
    assert first_claim and second_claim
    await repository.complete_job(
        second_claim.job_id,
        owner="worker-b",
        lease_token=second_claim.lease_token,
        now=now + timedelta(seconds=1),
    )
    assert (
        await repository.successful_watermark(tenant_id="t1", user_id="u1", session_id="s1") is None
    )
    await repository.complete_job(
        first_claim.job_id,
        owner="worker-a",
        lease_token=first_claim.lease_token,
        now=now + timedelta(seconds=2),
    )
    assert (
        await repository.successful_watermark(tenant_id="t1", user_id="u1", session_id="s1")
        == "turn_2"
    )


async def test_expired_final_attempt_becomes_dead_letter() -> None:
    repository = MemoryFormationTurnJobRepository()
    await repository.append_turn(_turn(1))
    job = await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
        max_attempts=1,
    )
    assert job
    assert job.max_attempts == 1
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    assert await repository.claim_job(owner="worker-a", now=now, lease_seconds=10)
    assert (
        await repository.claim_job(
            owner="worker-b", now=now + timedelta(seconds=10), lease_seconds=10
        )
        is None
    )
    assert repository.jobs[job.job_id].status == MemoryFormationJobStatus.DEAD_LETTER


def test_formation_range_idempotency_key_is_stable_and_identity_scoped() -> None:
    values = {
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "first_turn_id": "turn_1",
        "last_turn_id": "turn_5",
        "policy_version": "policy-v1",
    }
    assert formation_range_idempotency_key(**values) == formation_range_idempotency_key(**values)
    assert formation_range_idempotency_key(**values) != formation_range_idempotency_key(
        **{**values, "tenant_id": "t2"}
    )


async def test_retry_configuration_does_not_change_range_identity() -> None:
    first_repository = MemoryFormationTurnJobRepository()
    second_repository = MemoryFormationTurnJobRepository()
    await first_repository.append_turn(_turn(1))
    await second_repository.append_turn(_turn(1))
    parameters = {
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "trigger": MemoryFormationTrigger.IDLE,
        "mode": "observe",
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
        "policy_version": "policy-v1",
    }
    first = await first_repository.create_job_for_pending(max_attempts=2, **parameters)
    second = await second_repository.create_job_for_pending(max_attempts=9, **parameters)

    assert first and second
    assert first.idempotency_key == second.idempotency_key
    assert first.max_attempts == 2
    assert second.max_attempts == 9


async def test_in_memory_formation_repository_does_not_expose_mutable_state() -> None:
    repository = MemoryFormationTurnJobRepository()
    original = _turn(1).model_copy(update={"user_text": "original"})
    returned_turn = await repository.append_turn(original)
    original.tenant_id = "attacker-tenant"
    returned_turn.user_text = "attacker"
    pending = await repository.list_pending_turns(tenant_id="t1", user_id="u1", session_id="s1")
    assert len(pending) == 1
    assert pending[0].user_text == "original"
    job = await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job
    job.user_id = "attacker"
    job.source_refs.clear()
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claimed = await repository.claim_job(owner="worker", now=now, lease_seconds=30)
    assert claimed and claimed.user_id == "u1"
    lease_token = claimed.lease_token
    claimed.lease_token = "attacker-token"
    completed = await repository.complete_job(
        claimed.job_id,
        owner="worker",
        lease_token=lease_token,
        now=now + timedelta(seconds=1),
        trace_summary={"nested": {"count": 1}},
    )
    completed.trace_summary["nested"]["count"] = 99
    assert repository.jobs[claimed.job_id].trace_summary == {"nested": {"count": 1}}
    assert repository.turns["turn_1"].status == "formed"


async def test_database_custom_retry_limit_round_trips_and_dead_letters(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-retries.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseMemoryFormationTurnJobRepository(
        await managed_database.session_factory(settings)
    )
    await repository.append_turn(_turn(1))
    job = await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
        max_attempts=2,
    )
    assert job and job.max_attempts == 2

    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    for attempt in range(2):
        claimed = await repository.claim_job(owner=f"worker-{attempt}", now=now, lease_seconds=10)
        assert claimed and claimed.max_attempts == 2
        failed = await repository.fail_job(
            job.job_id,
            owner=f"worker-{attempt}",
            lease_token=claimed.lease_token,
            now=now,
            error_code="invalid_output",
            next_attempt_at=now,
        )
    assert failed.status == MemoryFormationJobStatus.DEAD_LETTER


async def test_database_repository_recovers_across_restart_and_isolates_tenants(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-repository.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    repository = DatabaseMemoryFormationTurnJobRepository(session_factory)
    await repository.append_turn(_turn(1))
    await repository.append_turn(_turn(2))
    await repository.append_turn(
        _turn(1, session_id="other").model_copy(
            update={"turn_id": "other_turn", "request_id": "other_request", "tenant_id": "t2"}
        )
    )
    job = await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.TURN_WINDOW,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
        max_turns=1,
    )
    assert job and job.source_refs == ["turn_1"]

    restarted = DatabaseMemoryFormationTurnJobRepository(session_factory)
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claimed = await restarted.claim_job(owner="worker-a", now=now, lease_seconds=30)
    assert claimed
    await restarted.complete_job(
        claimed.job_id,
        owner="worker-a",
        lease_token=claimed.lease_token,
        now=now + timedelta(seconds=1),
    )
    assert (
        await restarted.successful_watermark(tenant_id="t1", user_id="u1", session_id="s1")
        == "turn_1"
    )
    assert [
        turn.turn_id
        for turn in await restarted.list_pending_turns(
            tenant_id="t1", user_id="u1", session_id="s1"
        )
    ] == ["turn_2"]
    assert [
        turn.turn_id
        for turn in await restarted.list_pending_turns(
            tenant_id="t2", user_id="u1", session_id="other"
        )
    ] == ["other_turn"]


async def test_database_repository_rejects_stale_worker_after_lease_recovery(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-lease.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseMemoryFormationTurnJobRepository(
        await managed_database.session_factory(settings)
    )
    await repository.append_turn(_turn(1))
    job = await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    first = await repository.claim_job(owner="worker-a", now=now, lease_seconds=10)
    second = await repository.claim_job(
        owner="worker-b", now=now + timedelta(seconds=10), lease_seconds=10
    )
    assert first and second
    with pytest.raises(ValueError, match="transition lost"):
        await repository.complete_job(
            job.job_id,
            owner="worker-a",
            lease_token=first.lease_token,
            now=now + timedelta(seconds=11),
        )


async def test_database_repository_claim_is_atomic_on_sqlite(tmp_path, managed_database) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-claim-race.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    repository = DatabaseMemoryFormationTurnJobRepository(session_factory)
    await repository.append_turn(_turn(1))
    assert await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claims = await asyncio.gather(
        repository.claim_job(owner="worker-a", now=now, lease_seconds=30),
        repository.claim_job(owner="worker-b", now=now, lease_seconds=30),
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].attempt_count == 1


async def test_database_repository_terminal_transition_is_atomic_on_sqlite(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-terminal-race.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseMemoryFormationTurnJobRepository(
        await managed_database.session_factory(settings)
    )
    await repository.append_turn(_turn(1))
    assert await repository.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claim = await repository.claim_job(owner="worker-a", now=now, lease_seconds=30)
    assert claim
    results = await asyncio.gather(
        repository.complete_job(
            claim.job_id,
            owner="worker-a",
            lease_token=claim.lease_token,
            now=now + timedelta(seconds=1),
        ),
        repository.fail_job(
            claim.job_id,
            owner="worker-a",
            lease_token=claim.lease_token,
            now=now + timedelta(seconds=1),
            error_code="provider_error",
            next_attempt_at=now + timedelta(seconds=5),
        ),
        return_exceptions=True,
    )
    assert len([result for result in results if not isinstance(result, Exception)]) == 1
    assert len([result for result in results if isinstance(result, ValueError)]) == 1


async def test_database_turn_collision_does_not_disclose_other_tenant_capsule(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-turn-collision.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseMemoryFormationTurnJobRepository(
        await managed_database.session_factory(settings)
    )
    original = _turn(1).model_copy(update={"user_text": "tenant-a-private"})
    await repository.append_turn(original)
    collision = original.model_copy(
        update={
            "request_id": "tenant-b-request",
            "tenant_id": "t2",
            "user_id": "u2",
            "user_text": "tenant-b",
        }
    )
    with pytest.raises(ValueError, match="identity conflict") as error:
        await repository.append_turn(collision)
    assert "tenant-a-private" not in str(error.value)


async def test_database_turn_round_trip_preserves_canonical_ref_types(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-turn-refs.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseMemoryFormationTurnJobRepository(
        await managed_database.session_factory(settings)
    )
    stored = await repository.append_turn(
        _turn(1).model_copy(
            update={
                "result_refs": ["result_1"],
                "plan_refs": ["plan_1"],
                "artifact_refs": ["artifact_1"],
            }
        )
    )
    assert stored.result_refs == ["result_1"]
    assert stored.plan_refs == ["plan_1"]
    assert stored.artifact_refs == ["artifact_1"]
