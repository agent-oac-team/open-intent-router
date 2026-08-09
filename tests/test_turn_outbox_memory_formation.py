from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.core.config import Settings
from app.core.memory_runtime import build_memory_runtime_policy
from app.db.session import create_all_tables, create_engine, create_session_factory
from app.dependencies import (
    _memory_data_settings,
    get_structured_formation_publisher,
    get_turn_capture_service,
)
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.repositories.turn_outbox import (
    DatabaseTurnOutboxRepository,
    MemoryTurnOutboxRepository,
)
from app.repositories.turn_route_completion import (
    DatabaseRouteTurnCompletionStore,
    MemoryRouteTurnCompletionStore,
)
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.common import UserContext
from app.schemas.memory import MemoryRecallRequest
from app.schemas.turns import (
    CanonicalTurn,
    FormationEligibilitySnapshot,
    TurnOutboxEvent,
    TurnUserInput,
)
from app.services.memory_formation import (
    FormationTriggerCoordinator,
    TurnCapsuleBuilder,
    TurnOutboxFormationConsumer,
)
from app.services.memory_service import MemoryService
from app.services.turn_service import TurnService
from host_apps.oac.config import (
    OacHostProfile,
    OacHostSettings,
    validate_governance_profile,
)


def _settings(**overrides) -> Settings:
    values = {
        "storage_backend": "memory",
        "memory_mode": "observe",
        "memory_formation_window_turns": 1,
        **overrides,
    }
    return Settings(
        **values,
    )


async def _completed_turn_with_outbox(settings: Settings):
    turns = MemoryTurnRepository()
    outbox = MemoryTurnOutboxRepository()
    service = TurnService(
        turns,
        route_completion_store=MemoryRouteTurnCompletionStore(
            turn_repository=turns,
            outbox_repository=outbox,
        ),
    )
    started = await service.start_turn(
        tenant_id="oac",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="oac",
        user_input=TurnUserInput(text="请记住我偏好简短回答"),
    )
    completed = await service.complete_route_only(
        tenant_id="oac",
        user_id="user-1",
        request_id="request-1",
        response_kind="reply",
        response_text="好的，我会保持简短。",
    )
    return turns, outbox, started.turn, completed


async def test_route_only_completion_publishes_turn_outbox_and_forms_once() -> None:
    settings = _settings()
    turns, outbox, _, completed = await _completed_turn_with_outbox(settings)
    formation = MemoryFormationTurnJobRepository()
    events = MemoryItemRepository()
    consumer = TurnOutboxFormationConsumer(
        settings=settings,
        outbox_repository=outbox,
        turn_repository=turns,
        builder=TurnCapsuleBuilder(settings),
        coordinator=FormationTriggerCoordinator(settings=settings, repository=formation),
        event_repository=events,
        owner="consumer-1",
    )

    first = await consumer.run_once()
    await outbox.add_idempotent(
        TurnOutboxEvent(
            outbox_id="outbox-replay",
            turn_id=completed.turn_id,
            event_type="turn.completed",
            idempotency_key="turn.completed.replay",
            available_at=datetime.now(UTC),
        )
    )
    replay = await consumer.run_once()

    assert first["status"] == "captured"
    assert replay["status"] == "captured"
    assert list(formation.turns) == [completed.turn_id]
    assert len(formation.jobs) == 1
    assert formation.turns[completed.turn_id].user_text == "请记住我偏好简短回答"
    assert formation.turns[completed.turn_id].assistant_text == "好的，我会保持简短。"


def test_turn_capsule_builder_rejects_nonterminal_turn() -> None:
    now = datetime.now(UTC)
    turn = CanonicalTurn(
        turn_id="turn-pending",
        tenant_id="oac",
        user_id="user-1",
        session_id="session-1",
        request_id="request-pending",
        source="oac",
        user_input=TurnUserInput(text="未完成"),
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(ValueError, match="completed Canonical Turn"):
        TurnCapsuleBuilder(_settings()).build_canonical_capsule(turn)


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (build_memory_runtime_policy("off"), "formation_mode_off"),
        (
            build_memory_runtime_policy("on", execution_plane="decision_shadow"),
            "decision_shadow_no_memory_side_effect",
        ),
    ],
)
async def test_outbox_consumer_audits_mode_off_without_memory_side_effect(policy, reason) -> None:
    settings = _settings(memory_mode=policy.mode)
    turns, outbox, _, _ = await _completed_turn_with_outbox(settings)
    formation = MemoryFormationTurnJobRepository()
    events = MemoryItemRepository()
    consumer = TurnOutboxFormationConsumer(
        settings=settings,
        outbox_repository=outbox,
        turn_repository=turns,
        builder=TurnCapsuleBuilder(settings),
        coordinator=FormationTriggerCoordinator(
            settings=settings, repository=formation, runtime_policy=policy
        ),
        event_repository=events,
        owner="consumer-off",
        runtime_policy=policy,
    )

    result = await consumer.run_once()

    assert result == {"status": "skipped", "processed": 1, "reason": reason}
    assert formation.turns == {}
    assert formation.jobs == {}
    assert events.events[0].payload == {"reason_code": reason, "source": "turn_outbox"}


async def test_outbox_consumer_honors_persisted_suppression_after_mode_changes() -> None:
    settings = _settings(memory_mode="on")
    turns, outbox, _, completed = await _completed_turn_with_outbox(settings)
    original = next(iter(outbox.events.values()))
    snapshot = FormationEligibilitySnapshot(
        mode="off",
        suppressed=True,
        reason_code="formation_mode_off",
        policy_version="formation-policy-v1",
    )
    outbox.events[original.outbox_id] = original.model_copy(
        update={
            "payload": {
                "turn_id": completed.turn_id,
                "tenant_id": completed.tenant_id,
                "user_id": completed.user_id,
                "request_id": completed.request_id,
                "formation_eligibility": snapshot.model_dump(mode="json"),
            }
        }
    )
    formation = MemoryFormationTurnJobRepository()
    events = MemoryItemRepository()
    consumer = TurnOutboxFormationConsumer(
        settings=settings,
        outbox_repository=outbox,
        turn_repository=turns,
        builder=TurnCapsuleBuilder(settings),
        coordinator=FormationTriggerCoordinator(settings=settings, repository=formation),
        event_repository=events,
        owner="consumer-snapshot",
    )

    result = await consumer.run_once()

    assert result == {
        "status": "skipped",
        "processed": 1,
        "reason": "formation_mode_off",
    }
    assert formation.turns == {}
    assert events.events[0].payload["reason_code"] == "formation_mode_off"


async def test_database_route_completion_writes_turn_and_outbox_atomically(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'route-outbox.db'}",
    )
    await create_all_tables(settings)
    factory = create_session_factory(settings)
    turns = DatabaseTurnRepository(factory)
    service = TurnService(
        turns,
        route_completion_store=DatabaseRouteTurnCompletionStore(factory),
    )
    started = await service.start_turn(
        tenant_id="oac",
        user_id="user-1",
        session_id="session-1",
        request_id="request-db",
        source="oac",
        user_input=TurnUserInput(text="hello"),
    )

    completed = await service.complete_route_only(
        tenant_id="oac",
        user_id="user-1",
        request_id="request-db",
        response_kind="reply",
        response_text="world",
    )
    claimed = await DatabaseTurnOutboxRepository(factory).claim(
        owner="test", now=datetime.now(UTC), lease_seconds=60
    )

    assert completed.turn_id == started.turn.turn_id
    assert claimed is not None
    assert claimed.turn_id == completed.turn_id
    assert claimed.event_type == "turn.completed"


async def test_off_mode_disables_recall() -> None:
    service = MemoryService(
        settings=_settings(memory_mode="off"),
        repository=MemoryItemRepository(),
    )

    response = await service.recall(
        MemoryRecallRequest(
            query="偏好",
            user=UserContext(id="user-1", attributes={"tenant_id": "oac"}),
        )
    )

    assert response.context.status == "disabled"
    assert response.context.metadata["memory_enabled"] is False
    assert response.context.metadata["memory_recall_enabled"] is False


def test_state_rehearsal_and_legacy_history_configuration_are_fail_closed() -> None:
    core = Settings(database_url="sqlite+aiosqlite:///main.db")
    with pytest.raises(ValueError, match="isolated database URL"):
        validate_governance_profile(
            OacHostProfile(core=core, host=OacHostSettings(shadow_mode="state_rehearsal"))
        )
    with pytest.raises(ValueError, match="must differ from the primary database"):
        validate_governance_profile(
            OacHostProfile(
                core=core,
                host=OacHostSettings(
                    shadow_mode="state_rehearsal",
                    state_rehearsal_database_url=core.database_url,
                ),
            )
        )
    with pytest.raises(ValidationError, match="legacy history import is not supported"):
        Settings(memory_import_legacy_history_enabled=True)

    policy = build_memory_runtime_policy("on", execution_plane="state_rehearsal")
    memory_settings = _memory_data_settings(
        core,
        policy,
        database_url="sqlite+aiosqlite:///rehearsal.db",
        collection="oir_memory_vectors_rehearsal",
    )
    assert memory_settings.database_url.endswith("rehearsal.db")
    assert memory_settings.memory_milvus_collection == "oir_memory_vectors_rehearsal"


def test_production_composition_does_not_publish_isolated_runtime_records() -> None:
    assert get_structured_formation_publisher() is None
    assert get_turn_capture_service() is None

    shadow = build_memory_runtime_policy("on", execution_plane="decision_shadow")
    assert shadow.memory_enabled is False
    assert shadow.effective_recall_enabled is False
    assert shadow.effective_formation_mode == "off"


async def test_state_rehearsal_starts_with_empty_isolated_memory_domain(tmp_path) -> None:
    main_path = tmp_path / "main.db"
    rehearsal_path = tmp_path / "rehearsal.db"
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{main_path}",
    )
    memory_settings = _memory_data_settings(
        settings,
        build_memory_runtime_policy("on", execution_plane="state_rehearsal"),
        database_url=f"sqlite+aiosqlite:///{rehearsal_path}",
        collection="oir_memory_vectors_rehearsal",
    )

    await create_all_tables(memory_settings)
    engine = create_engine(memory_settings)
    async with engine.connect() as connection:
        counts = {
            table: await connection.scalar(text(f"SELECT count(*) FROM {table}"))
            for table in (
                "memory_items",
                "memory_events",
                "memory_formation_turns",
                "memory_formation_jobs",
            )
        }
    await engine.dispose()

    assert counts == {table: 0 for table in counts}
    assert rehearsal_path.exists()
    assert not main_path.exists()
