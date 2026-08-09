import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.models import (
    AgentResultModel,
    AgentRunModel,
    Base,
    CanonicalTurnModel,
    MemoryEventModel,
    MemoryFormationJobModel,
    MemoryFormationTurnModel,
    MemoryIndexOperationModel,
    MemoryItemModel,
    MemoryRevisionModel,
    TurnOutboxModel,
)
from app.db.session import _ensure_compatible_columns, create_all_tables, create_session_factory
from app.llm.conversation_formation import (
    ConversationFormationResponse,
    FakeConversationFormationModel,
)
from app.repositories.canonical_invocations import DatabaseCanonicalInvocationStore
from app.repositories.context_stores import DatabaseMemoryItemRepository
from app.repositories.memory_formation import DatabaseMemoryFormationTurnJobRepository
from app.repositories.memory_index_operations import DatabaseMemoryIndexOutboxRepository
from app.repositories.memory_revisions import DatabaseMemoryRevisionLedgerRepository
from app.repositories.memory_traces import DatabaseMemoryFormationTraceRepository
from app.repositories.turn_outbox import DatabaseTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository
from app.schemas.common import UserContext
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryDecisionStatus,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationReasonCode,
    MemoryFormationTurn,
    MemoryIndexOperation,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryOperation,
    MemoryRecallRequest,
    MemoryRevision,
)
from app.schemas.turns import FormationEligibilitySnapshot, TurnUserInput
from app.services.memory_adapter import (
    MemoryIndexOperationResult,
    MemoryProviderOperationStatus,
    RepositoryMemoryAdapter,
)
from app.services.memory_candidate_policy import CandidatePolicyResult, MemoryCandidatePolicy
from app.services.memory_formation import (
    FormationIdleSweeper,
    FormationJobWorker,
    FormationTriggerCoordinator,
    TurnCapsuleBuilder,
    TurnOutboxFormationConsumer,
)
from app.services.memory_integration import MemoryFormationProcessor
from app.services.memory_management import MemoryManagementService
from app.services.memory_observability import MemoryObservabilityService
from app.services.memory_service import MemoryService
from app.services.turn_service import TurnService


def _postgresql_url() -> str:
    value = os.getenv("OIR_TEST_POSTGRESQL_URL") or dotenv_values(".env").get("DATABASE_URL")
    if not isinstance(value, str) or not value.startswith(
        ("postgresql://", "postgresql+asyncpg://")
    ):
        pytest.skip("real PostgreSQL integration URL is not configured")
    return value


async def test_real_postgresql_legacy_memory_migration_is_rollback_safe_and_idempotent() -> None:
    database_url = _postgresql_url().replace("postgresql://", "postgresql+asyncpg://", 1)
    schema_name = f"oir_memory_migration_{uuid4().hex}"
    quoted_schema = f'"{schema_name}"'
    admin_engine = create_async_engine(database_url)
    scoped_engine = None

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        scoped_engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema_name}},
        )
        async with scoped_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    CREATE TABLE memory_items (
                        memory_id VARCHAR(128) PRIMARY KEY,
                        scope VARCHAR(64) NOT NULL,
                        subject_type VARCHAR(64) NOT NULL,
                        subject_id VARCHAR(128) NOT NULL,
                        user_id VARCHAR(128),
                        tenant_id VARCHAR(128),
                        agent_id VARCHAR(128),
                        content TEXT NOT NULL,
                        structured_value_text TEXT NOT NULL,
                        source VARCHAR(64) NOT NULL,
                        confidence INTEGER NOT NULL,
                        importance INTEGER NOT NULL,
                        visibility VARCHAR(32) NOT NULL,
                        ttl_expires_at TIMESTAMP WITH TIME ZONE,
                        metadata_text TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO memory_items (
                        memory_id, scope, subject_type, subject_id, user_id, tenant_id,
                        content, structured_value_text, source, confidence, importance,
                        visibility, metadata_text, created_at, updated_at
                    ) VALUES (
                        'legacy_ready', 'user_preference', 'user', 'legacy_user',
                        'legacy_user', 'legacy_tenant', 'prefers concise answers', '{}',
                        'manual', 95, 50, 'user', '{"mem0_memory_id":"mem0_legacy"}',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )

        with pytest.raises(RuntimeError, match="forced migration failure"):
            async with scoped_engine.begin() as conn:
                await conn.run_sync(_run_schema_migration)
                raise RuntimeError("forced migration failure")

        async with scoped_engine.connect() as conn:
            rolled_back = await conn.run_sync(
                lambda sync_conn: {
                    "tables": set(inspect(sync_conn).get_table_names()),
                    "columns": {
                        column["name"] for column in inspect(sync_conn).get_columns("memory_items")
                    },
                }
            )
        assert "memory_revisions" not in rolled_back["tables"]
        assert "lifecycle_status" not in rolled_back["columns"]
        assert "current_revision_id" not in rolled_back["columns"]

        for _ in range(3):
            async with scoped_engine.begin() as conn:
                await conn.run_sync(_run_schema_migration)

        async with scoped_engine.connect() as conn:
            migrated_columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]: column
                    for column in inspect(sync_conn).get_columns("memory_items")
                }
            )
            legacy = (
                (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_key, current_revision_id, lifecycle_status, index_status
                            FROM memory_items
                            WHERE memory_id = 'legacy_ready'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )
            legacy_revision_count = await conn.scalar(
                text("SELECT count(*) FROM memory_revisions WHERE memory_id = 'legacy_ready'")
            )

        lifecycle_columns = {
            "memory_key",
            "candidate_hash",
            "current_revision_id",
            "formation_job_id",
            "lifecycle_status",
            "index_status",
            "canonical_refs_text",
        }
        assert lifecycle_columns <= migrated_columns.keys()
        assert all(migrated_columns[name]["nullable"] for name in lifecycle_columns)
        assert legacy == {
            "memory_key": "legacy:legacy_tenant:legacy_ready",
            "current_revision_id": "mrev_legacy_legacy_ready",
            "lifecycle_status": "active",
            "index_status": "ready",
        }
        assert legacy_revision_count == 1

        async with scoped_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO memory_items (
                        memory_id, scope, subject_type, subject_id, user_id, tenant_id,
                        content, structured_value_text, source, confidence, importance,
                        visibility, metadata_text, created_at, updated_at
                    ) VALUES (
                        'old_writer_pending', 'stable_fact', 'user', 'legacy_user',
                        'legacy_user', 'legacy_tenant', 'uses the compact layout', '{}',
                        'manual', 90, 50, 'user', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        async with scoped_engine.connect() as conn:
            before_recovery = (
                (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_key, current_revision_id, lifecycle_status, index_status
                            FROM memory_items
                            WHERE memory_id = 'old_writer_pending'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert all(value is None for value in before_recovery.values())

        for _ in range(2):
            async with scoped_engine.begin() as conn:
                await conn.run_sync(_run_schema_migration)

        async with scoped_engine.connect() as conn:
            recovered = (
                (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_key, current_revision_id, lifecycle_status, index_status
                            FROM memory_items
                            WHERE memory_id = 'old_writer_pending'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )
            revision_counts = dict(
                (
                    await conn.execute(
                        text(
                            """
                            SELECT memory_id, count(*) AS revision_count
                            FROM memory_revisions
                            WHERE memory_id IN ('legacy_ready', 'old_writer_pending')
                            GROUP BY memory_id
                            """
                        )
                    )
                ).all()
            )
        assert recovered == {
            "memory_key": "legacy:legacy_tenant:old_writer_pending",
            "current_revision_id": "mrev_legacy_old_writer_pending",
            "lifecycle_status": "active",
            "index_status": "pending",
        }
        assert revision_counts == {"legacy_ready": 1, "old_writer_pending": 1}
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        await admin_engine.dispose()


def _run_schema_migration(sync_conn) -> None:
    Base.metadata.create_all(sync_conn)
    _ensure_compatible_columns(sync_conn)


class _ReadyDatabaseMemoryAdapter(RepositoryMemoryAdapter):
    async def execute_index_operation(self, operation, *, item):
        return MemoryIndexOperationResult(
            operation=operation.operation,
            status=MemoryProviderOperationStatus.SUCCESS,
            memory_id=operation.memory_id,
            external_memory_id=f"pg-index:{operation.memory_id}",
        )


async def test_real_postgresql_route_turn_to_recall_pork_preference() -> None:
    suffix = uuid4().hex
    tenant_id = f"pg_route_memory_tenant_{suffix}"
    user_id = f"pg_route_memory_user_{suffix}"
    session_id = f"pg_route_memory_session_{suffix}"
    request_id = f"pg_route_memory_request_{suffix}"
    settings = Settings(
        storage_backend="database",
        database_url=_postgresql_url(),
        memory_strategy_provider="memory",
        memory_mode="on",
        memory_formation_window_turns=1,
        memory_formation_model_timeout_seconds=1,
    )
    await create_all_tables(settings)
    factory = create_session_factory(settings)
    turns = DatabaseTurnRepository(factory)
    formation = DatabaseMemoryFormationTurnJobRepository(factory)
    memories = DatabaseMemoryItemRepository(factory)
    adapter = _ReadyDatabaseMemoryAdapter(memories)
    memory = MemoryService(
        settings=settings,
        repository=memories,
        adapter=adapter,
        formation_repository=formation,
    )
    try:
        started = await TurnService(turns).start_turn(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            source="host_chat",
            user_input=TurnUserInput(
                text="明天要拜访一位关注稳健理财的客户，帮我做访前准备。我喜欢吃猪肉。"
            ),
        )
        store = DatabaseCanonicalInvocationStore(factory)
        now = datetime.now(UTC)
        run, _, _ = await store.start_run(
            AgentRun(
                run_id=f"pg_route_memory_run_{suffix}",
                request_id=request_id,
                session_id=session_id,
                agent_id="visit-preparation",
                user_id=user_id,
                tenant_id=tenant_id,
                status="running",
                invoker_type="mock",
                created_at=now,
                updated_at=now,
            )
        )
        _, _, completed_turn = await store.complete_run(
            run=run.model_copy(update={"status": "completed", "output": {"ok": True}}),
            result=AgentResult(
                result_id=f"pg_route_memory_result_{suffix}",
                run_id=run.run_id,
                session_id=session_id,
                agent_id=run.agent_id,
                user_id=user_id,
                tenant_id=tenant_id,
                status="completed",
                message="访前准备已完成",
                output={"ok": True},
                created_at=now,
            ),
            response_text="访前准备已完成",
            eligibility=FormationEligibilitySnapshot(
                mode="enforced",
                policy_version="pg-route-memory-v1",
            ),
        )
        assert completed_turn.turn_id == started.turn.turn_id

        consumer = TurnOutboxFormationConsumer(
            settings=settings,
            outbox_repository=DatabaseTurnOutboxRepository(factory),
            turn_repository=turns,
            builder=TurnCapsuleBuilder(settings),
            coordinator=FormationTriggerCoordinator(settings=settings, repository=formation),
            event_repository=memories,
            owner=f"pg-consumer-{suffix}",
        )
        assert (await consumer.run_once())["status"] == "captured"
        candidate = MemoryFormationCandidate(
            candidate_id=f"pg_pork_candidate_{suffix}",
            proposed_operation="add",
            scope="user_preference",
            content="User likes eating pork.",
            structured_value={"slot": "food_preference", "value": "pork"},
            semantic=MemoryCandidateSemantics(
                target="assistant_response",
                slot="food_preference",
                value="pork",
                temporal_scope="long_term",
                polarity="affirmed",
                certainty="certain",
                change_intent="set",
            ),
            subject_id_hint=user_id,
            tenant_id_hint=tenant_id,
            memory_key_hint="food_preference",
            confidence=0.99,
            evidence_refs=[
                MemoryEvidenceRef(
                    turn_id=completed_turn.turn_id,
                    role="user",
                    quote="我喜欢吃猪肉",
                )
            ],
            reason="deterministic PostgreSQL E2E preference",
        )
        processor = MemoryFormationProcessor(
            repository=formation,
            memory_repository=memories,
            model=FakeConversationFormationModel(
                ConversationFormationResponse(candidates=[candidate])
            ),
            policy=MemoryCandidatePolicy(settings=settings, repository=memories),
            lifecycle=memory.lifecycle,
        )
        job = await FormationJobWorker(
            settings=settings,
            repository=formation,
            processor=processor,
            owner=f"pg-formation-{suffix}",
        ).run_once()
        assert job is not None and job.status.value == "completed"
        assert job.trace_summary.get("operation_counts") == {"add": 1}

        indexed = await memory.index_worker.run_once()
        assert indexed is not None and indexed.completed is True
        assert await memory.index_worker.run_once() is None
        formed = await memories.list_active(
            tenant_id=tenant_id,
            user_id=user_id,
            scopes=["user_preference"],
            limit=10,
        )
        assert len(formed) == 1
        assert formed[0].content == "User likes eating pork."
        assert formed[0].index_status == "ready"

        recall = await memory.recall(
            MemoryRecallRequest(
                query="我喜欢吃什么？",
                user=UserContext(id=user_id, attributes={"tenant_id": tenant_id}),
                scopes=["user_preference"],
                metadata_filters={
                    "request_id": f"pg_route_memory_recall_{suffix}",
                    "session_id": session_id,
                    "consumer": "agent:visit-preparation",
                },
            )
        )
        assert [item.memory_id for item in recall.context.items] == [formed[0].memory_id]

        debug = await MemoryObservabilityService(
            settings=settings,
            memory_service=memory,
            formation_repository=formation,
            trace_repository=DatabaseMemoryFormationTraceRepository(factory),
            turn_repository=turns,
            outbox_repository=DatabaseTurnOutboxRepository(factory),
        ).debug_state(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        assert debug.request_trace is not None
        assert debug.request_trace.overall_stage == "persisted"
        assert debug.request_trace.turn_id == completed_turn.turn_id
        assert debug.request_trace.memory_ids == [formed[0].memory_id]
    finally:
        await _cleanup(factory, tenant_id=tenant_id)
        await factory.kw["bind"].dispose()


async def test_real_postgresql_multi_worker_formation_revision_and_outbox() -> None:
    suffix = uuid4().hex
    tenant_id = f"pg_acceptance_tenant_{suffix}"
    user_id = f"pg_acceptance_user_{suffix}"
    session_id = f"pg_acceptance_session_{suffix}"
    settings = Settings(
        storage_backend="database",
        database_url=_postgresql_url(),
        memory_mode="observe",
        memory_formation_window_turns=5,
        memory_formation_idle_seconds=30,
        memory_formation_model_timeout_seconds=1,
        memory_formation_lease_seconds=5,
    )
    await create_all_tables(settings)
    first_factory = create_session_factory(settings)
    second_factory = create_session_factory(settings)
    first = DatabaseMemoryFormationTurnJobRepository(first_factory)
    second = DatabaseMemoryFormationTurnJobRepository(second_factory)
    coordinator = FormationTriggerCoordinator(settings=settings, repository=first)
    base = datetime(2026, 7, 14, 5, tzinfo=UTC)

    try:
        for index in range(1, 5):
            await coordinator.append_and_check(
                _turn(
                    index,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    completed_at=base + timedelta(seconds=index),
                )
            )
        old_deadline = base + timedelta(seconds=34)
        await asyncio.gather(
            coordinator.append_and_check(
                _turn(
                    5,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    completed_at=base + timedelta(seconds=5),
                )
            ),
            FormationIdleSweeper(settings=settings, repository=second).run_once(now=old_deadline),
        )
        claims = await asyncio.gather(
            first.claim_job(owner="pg-worker-a", now=old_deadline, lease_seconds=5),
            second.claim_job(owner="pg-worker-b", now=old_deadline, lease_seconds=5),
        )
        assert sum(claim is not None for claim in claims) == 1
        async with first_factory() as session:
            job_count = await session.scalar(
                select(func.count())
                .select_from(MemoryFormationJobModel)
                .where(MemoryFormationJobModel.tenant_id == tenant_id)
            )
        assert job_count == 1

        items = DatabaseMemoryItemRepository(first_factory)
        revisions = DatabaseMemoryRevisionLedgerRepository(first_factory)
        memory_id = f"pg_memory_{suffix}"
        item = MemoryItem(
            memory_id=memory_id,
            memory_key=f"tenant:{tenant_id}:user:{user_id}:preference:response_style",
            scope="user_preference",
            subject_id=user_id,
            user_id=user_id,
            tenant_id=tenant_id,
            content="initial",
        )
        await items.add(item)
        first_revision, _ = await revisions.append_revision_and_set_current(
            _revision(memory_id, item.memory_key or "", "initial", f"pg_rev_1_{suffix}"),
            tenant_id=tenant_id,
            user_id=user_id,
            subject_type="user",
            subject_id=user_id,
            expected_current_revision_id=None,
        )
        revision_attempts = await asyncio.gather(
            revisions.append_revision_and_set_current(
                _revision(memory_id, item.memory_key or "", "value-a", f"pg_rev_2a_{suffix}"),
                tenant_id=tenant_id,
                user_id=user_id,
                subject_type="user",
                subject_id=user_id,
                expected_current_revision_id=first_revision.revision_id,
            ),
            revisions.append_revision_and_set_current(
                _revision(memory_id, item.memory_key or "", "value-b", f"pg_rev_2b_{suffix}"),
                tenant_id=tenant_id,
                user_id=user_id,
                subject_type="user",
                subject_id=user_id,
                expected_current_revision_id=first_revision.revision_id,
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(value, Exception) for value in revision_attempts) == 1
        assert sum(isinstance(value, ValueError) for value in revision_attempts) == 1
        assert [
            value.revision_no
            for value in await revisions.list_for_memory(
                memory_id,
                tenant_id=tenant_id,
                user_id=user_id,
                subject_type="user",
                subject_id=user_id,
            )
        ] == [1, 2]

        outbox_a = DatabaseMemoryIndexOutboxRepository(first_factory)
        outbox_b = DatabaseMemoryIndexOutboxRepository(second_factory)
        for index in (1, 2):
            await outbox_a.add(
                MemoryIndexOperation(
                    index_operation_id=f"pg_outbox_{index}_{suffix}",
                    idempotency_key=f"pg-outbox:{suffix}:{index}",
                    operation="add",
                    memory_id=f"pg_outbox_memory_{index}_{suffix}",
                    revision_id=f"pg_outbox_revision_{index}_{suffix}",
                    tenant_id=tenant_id,
                )
            )
        outbox_claims = await asyncio.gather(
            outbox_a.claim(
                owner="pg-outbox-a",
                now=old_deadline,
                lease_seconds=5,
                tenant_id=tenant_id,
            ),
            outbox_b.claim(
                owner="pg-outbox-b",
                now=old_deadline,
                lease_seconds=5,
                tenant_id=tenant_id,
            ),
        )
        assert all(claim is not None for claim in outbox_claims)
        assert len({claim.index_operation_id for claim in outbox_claims if claim}) == 2

        governance_operation = await outbox_a.add(
            MemoryIndexOperation(
                index_operation_id=f"pg_governance_{suffix}",
                idempotency_key=f"pg-governance:{suffix}",
                operation="delete",
                memory_id=f"pg_governance_memory_{suffix}",
                tenant_id=tenant_id,
                status="completed",
            )
        )
        governance_claims = await asyncio.gather(
            outbox_a.accept_governance_repair(
                governance_operation.index_operation_id,
                tenant_id=tenant_id,
                expected_status=governance_operation.status,
                idempotency_key="repair-a",
                now=old_deadline,
                requeue=False,
                expected_version="version-1",
                expected_anomaly="canonical_not_closed",
            ),
            outbox_b.accept_governance_repair(
                governance_operation.index_operation_id,
                tenant_id=tenant_id,
                expected_status=governance_operation.status,
                idempotency_key="repair-b",
                now=old_deadline,
                requeue=False,
                expected_version="version-1",
                expected_anomaly="canonical_not_closed",
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(value, Exception) for value in governance_claims) == 1
        assert sum(isinstance(value, ValueError) for value in governance_claims) == 1
        winner_index = next(
            index
            for index, value in enumerate(governance_claims)
            if not isinstance(value, Exception)
        )
        winner_key = ("repair-a", "repair-b")[winner_index]
        _, replay = await outbox_a.accept_governance_repair(
            governance_operation.index_operation_id,
            tenant_id=tenant_id,
            expected_status=governance_operation.status,
            idempotency_key=winner_key,
            now=old_deadline,
            requeue=False,
            expected_version="version-1",
            expected_anomaly="canonical_not_closed",
        )
        assert replay is True
    finally:
        await _cleanup(first_factory, tenant_id=tenant_id)
        await first_factory.kw["bind"].dispose()
        await second_factory.kw["bind"].dispose()


async def test_real_postgresql_request_trace_resolves_pending_update_and_delete() -> None:
    suffix = uuid4().hex
    tenant_id = f"pg_decision_tenant_{suffix}"
    user_id = f"pg_decision_user_{suffix}"
    session_id = f"pg_decision_session_{suffix}"
    request_id = f"pg_decision_request_{suffix}"
    settings = Settings(
        storage_backend="database",
        database_url=_postgresql_url(),
        memory_strategy_provider="memory",
        memory_mode="observe",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    items = DatabaseMemoryItemRepository(session_factory)
    memory = MemoryService(settings=settings, repository=items)
    formation = DatabaseMemoryFormationTurnJobRepository(session_factory)

    try:
        await formation.append_turn(
            MemoryFormationTurn(
                turn_id=f"pg_decision_turn_{suffix}",
                request_id=request_id,
                session_id=session_id,
                run_id=f"pg_decision_run_{suffix}",
                user_id=user_id,
                tenant_id=tenant_id,
                result_status="completed",
                completed_at=datetime.now(UTC),
            )
        )
        job = await formation.create_job_for_pending(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            trigger="idle",
            mode="observe",
            model_version="model-v1",
            prompt_version="prompt-v1",
            policy_version="policy-v1",
        )
        assert job
        memory_key = f"tenant:{tenant_id}:user:{user_id}:preference:response_style"
        added = await memory.lifecycle.apply(
            CandidatePolicyResult(
                candidate=_pg_candidate(
                    suffix=suffix,
                    operation="add",
                    content="Initial preference",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    memory_key=memory_key,
                ),
                operation=_pg_operation(
                    suffix=f"add-{suffix}",
                    operation=MemoryOperation.ADD,
                    status=MemoryDecisionStatus.ACCEPTED,
                    reason=MemoryFormationReasonCode.ACCEPTED_NEW,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    memory_key=memory_key,
                    job_id=job.job_id,
                ),
                redacted_trace={},
            )
        )
        assert added.item and added.revision

        pending_results = []
        for proposed_operation, reason in (
            ("update", MemoryFormationReasonCode.CONFIDENCE_PENDING),
            ("delete", MemoryFormationReasonCode.AMBIGUOUS_DELETE),
        ):
            pending_results.append(
                await memory.lifecycle.apply(
                    CandidatePolicyResult(
                        candidate=_pg_candidate(
                            suffix=f"{proposed_operation}-{suffix}",
                            operation=proposed_operation,
                            content=f"Pending {proposed_operation}",
                            tenant_id=tenant_id,
                            user_id=user_id,
                            memory_key=memory_key,
                        ),
                        operation=_pg_operation(
                            suffix=f"{proposed_operation}-{suffix}",
                            operation=MemoryOperation.PENDING,
                            status=MemoryDecisionStatus.PENDING,
                            reason=reason,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            memory_key=memory_key,
                            job_id=job.job_id,
                            memory_id=added.item.memory_id,
                            revision_id=added.revision.revision_id,
                        ),
                        redacted_trace={},
                    )
                )
            )

        observability = MemoryObservabilityService(
            settings=settings,
            memory_service=memory,
            formation_repository=formation,
            trace_repository=DatabaseMemoryFormationTraceRepository(session_factory),
        )
        debug = await observability.debug_state(
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
        )
        pending = {
            decision.proposed_operation: decision
            for decision in debug.formation_traces[0].decisions
            if decision.decision_status == "pending"
        }
        assert set(pending) == {"update", "delete"}
        assert all(decision.decision_id for decision in pending.values())
        assert all(
            event.event_id not in {result.event.event_id for result in pending_results}
            for event in debug.events
        )

        management = MemoryManagementService(memory_service=memory)
        updated = await management.resolve_pending(
            decision_id=pending["update"].decision_id or "",
            action="confirm",
            tenant_id=tenant_id,
            user_id=user_id,
            actor=user_id,
            reason="isolated PostgreSQL acceptance",
            idempotency_key=f"pg-confirm-update-{suffix}",
            expected_revision_id=added.revision.revision_id,
        )
        rejected = await management.resolve_pending(
            decision_id=pending["delete"].decision_id or "",
            action="reject",
            tenant_id=tenant_id,
            user_id=user_id,
            actor=user_id,
            reason="isolated PostgreSQL acceptance",
            idempotency_key=f"pg-reject-delete-{suffix}",
            expected_revision_id=added.revision.revision_id,
        )
        current = await items.get_by_id(added.item.memory_id, tenant_id=tenant_id)
        assert updated.operation == "confirm"
        assert rejected.operation == "reject"
        assert current and current.content == "Pending update"
        assert current.lifecycle_status == "active"
    finally:
        await _cleanup(session_factory, tenant_id=tenant_id)
        await session_factory.kw["bind"].dispose()


def _turn(
    index: int,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    completed_at: datetime,
) -> MemoryFormationTurn:
    return MemoryFormationTurn(
        turn_id=f"pg_turn_{index}_{session_id}",
        request_id=f"pg_request_{index}_{session_id}",
        session_id=session_id,
        run_id=f"pg_run_{index}_{session_id}",
        user_id=user_id,
        tenant_id=tenant_id,
        user_text=f"user {index}",
        assistant_text=f"assistant {index}",
        result_status="completed",
        completed_at=completed_at,
    )


def _revision(memory_id: str, memory_key: str, content: str, revision_id: str) -> MemoryRevision:
    return MemoryRevision(
        revision_id=revision_id,
        memory_id=memory_id,
        revision_no=1,
        memory_key=memory_key,
        operation="add" if content == "initial" else "update",
        content=content,
        policy_version="pg-acceptance-v1",
    )


def _pg_candidate(
    *, suffix: str, operation: str, content: str, tenant_id: str, user_id: str, memory_key: str
) -> MemoryFormationCandidate:
    return MemoryFormationCandidate(
        candidate_id=f"pg_candidate_{suffix}",
        proposed_operation=operation,
        scope="user_preference",
        content=content,
        subject_id_hint=user_id,
        tenant_id_hint=tenant_id,
        memory_key_hint=memory_key,
        confidence=0.8,
        evidence_refs=[
            {
                "turn_id": f"pg_decision_turn_{suffix}",
                "role": "user",
                "quote": content,
            }
        ],
        reason="isolated PostgreSQL acceptance",
    )


def _pg_operation(
    *,
    suffix: str,
    operation: MemoryOperation,
    status: MemoryDecisionStatus,
    reason: MemoryFormationReasonCode,
    tenant_id: str,
    user_id: str,
    memory_key: str,
    job_id: str,
    memory_id: str | None = None,
    revision_id: str | None = None,
) -> MemoryLifecycleOperation:
    return MemoryLifecycleOperation(
        operation_id=f"pg_operation_{suffix}",
        operation=operation,
        decision_status=status,
        reason_code=reason,
        tenant_id=tenant_id,
        user_id=user_id,
        subject_type="user",
        subject_id=user_id,
        memory_key=memory_key,
        candidate_hash=f"sha256:{suffix}",
        memory_id=memory_id,
        revision_id=revision_id,
        formation_job_id=job_id,
    )


async def _cleanup(session_factory, *, tenant_id: str) -> None:
    async with session_factory() as session:
        memory_ids = select(MemoryItemModel.memory_id).where(MemoryItemModel.tenant_id == tenant_id)
        turn_ids = select(CanonicalTurnModel.turn_id).where(
            CanonicalTurnModel.tenant_id == tenant_id
        )
        await session.execute(delete(TurnOutboxModel).where(TurnOutboxModel.turn_id.in_(turn_ids)))
        await session.execute(
            delete(MemoryRevisionModel).where(MemoryRevisionModel.memory_id.in_(memory_ids))
        )
        for model in (
            MemoryEventModel,
            MemoryIndexOperationModel,
            MemoryFormationJobModel,
            MemoryFormationTurnModel,
            MemoryItemModel,
            AgentResultModel,
            AgentRunModel,
            CanonicalTurnModel,
        ):
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.commit()
