import asyncio
import os
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.core.config import Settings
from app.db.models import (
    MemoryEventModel,
    MemoryFormationJobModel,
    MemoryFormationTurnModel,
    MemoryIndexOperationModel,
    MemoryItemModel,
    MemoryRevisionModel,
    PlanModel,
    PlanStepModel,
)
from app.db.session import create_all_tables, create_session_factory
from app.llm.conversation_formation import OpenAICompatibleConversationFormationModel
from app.repositories.context_stores import DatabaseMemoryItemRepository
from app.repositories.database import DatabasePlanRepository
from app.repositories.memory_formation import DatabaseMemoryFormationTurnJobRepository
from app.repositories.memory_index_operations import DatabaseMemoryIndexOutboxRepository
from app.repositories.memory_lifecycle_store import DatabaseMemoryLifecycleStore
from app.repositories.memory_revisions import DatabaseMemoryRevisionLedgerRepository
from app.schemas.common import UserContext
from app.schemas.memory import (
    MemoryDecisionStatus,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTurn,
    MemoryIndexOperation,
    MemoryIndexStatus,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryOperation,
    MemoryRecallRequest,
    MemoryWriteCandidate,
)
from app.schemas.plans import Plan
from app.schemas.routing import RouteRequest
from app.services.mem0_config import mem0_health_check
from app.services.memory_adapter import Mem0MemoryAdapter
from app.services.memory_candidate_policy import MemoryCandidatePolicy
from app.services.memory_formation import FormationJobWorker
from app.services.memory_indexing import MemoryIndexOperationWorker, MemoryIndexRepairService
from app.services.memory_integration import MemoryFormationProcessor, StructuredFormationPublisher
from app.services.memory_lifecycle import MemoryLifecycleService
from app.services.memory_management import MemoryManagementService
from app.services.memory_service import MemoryService
from app.services.plan_service import PlanService
from app.services.task_continuation import TaskMemoryPlanResolver


async def main() -> int:
    settings = Settings()
    error = _preflight_error(settings)
    if error:
        print(f"SMOKE_FAIL: {error}")
        return 2
    health = mem0_health_check(settings)
    if health["status"] == "error":
        print(f"SMOKE_FAIL: mem0 配置错误: {health['checks']}")
        return 2
    if not health.get("embedding_api_key_configured"):
        print("SMOKE_FAIL: 请先配置 EMBEDDING_API_KEY 或 KNOWLEDGE_EMBEDDING_API_KEY")
        return 2
    if version("mem0ai") != "2.0.11":
        print(f"SMOKE_FAIL: 需要 mem0ai 2.0.11，当前为 {version('mem0ai')}")
        return 2

    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    repository = DatabaseMemoryItemRepository(session_factory)
    outbox = DatabaseMemoryIndexOutboxRepository(session_factory)
    lifecycle_store = DatabaseMemoryLifecycleStore(session_factory)
    revision_repository = DatabaseMemoryRevisionLedgerRepository(session_factory)
    adapter = Mem0MemoryAdapter(settings, repository)
    suffix = uuid4().hex.translate(str.maketrans("0123456789", "ghijklmnop"))
    tenant_id = f"smoke_tenant_{suffix}"
    user_id = f"smoke_user_{suffix}"
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=adapter,
        repository=repository,
        outbox=outbox,
        lifecycle_store=lifecycle_store,
        owner=f"smoke-index-worker-{uuid4().hex}",
        claim_tenant_id=tenant_id,
    )
    repair = MemoryIndexRepairService(
        adapter=adapter,
        repository=repository,
        outbox=outbox,
        lifecycle_store=lifecycle_store,
    )
    lifecycle = MemoryLifecycleService(settings=settings, store=lifecycle_store)
    service = MemoryService(
        settings=settings,
        repository=repository,
        adapter=adapter,
        lifecycle_store=lifecycle_store,
        index_outbox=outbox,
        index_worker=worker,
    )
    item = _smoke_item(tenant_id=tenant_id, user_id=user_id, suffix=suffix)
    failure: Exception | None = None
    try:
        await _verify_explicit_write(service, tenant_id=tenant_id, user_id=user_id)
        await _verify_preference_lifecycle(
            settings=settings,
            service=service,
            repository=repository,
            adapter=adapter,
            revision_repository=revision_repository,
            worker=worker,
            tenant_id=tenant_id,
            user_id=user_id,
            suffix=suffix,
        )
        if os.getenv("OIR_SMOKE_REAL_FORMATION") == "1":
            await _verify_real_formation_provider(
                settings=settings,
                service=service,
                repository=repository,
                worker=worker,
                tenant_id=tenant_id,
                user_id=user_id,
                suffix=suffix,
            )
        await _verify_plan_continuation(
            settings=settings,
            service=service,
            repository=repository,
            worker=worker,
            session_factory=session_factory,
            tenant_id=tenant_id,
            user_id=user_id,
            suffix=suffix,
        )
        await repository.add(item)
        add_operation = await outbox.add(_index_operation(item, "add"))
        await _run_until(worker, add_operation.index_operation_id)
        indexed = await repository.get_by_id(item.memory_id, tenant_id=tenant_id)
        if indexed is None or not indexed.metadata.get("mem0_memory_id"):
            raise RuntimeError("ADD 未保存 external mapping")
        records = await adapter.scan_provider_records(tenant_id=tenant_id, memory_id=item.memory_id)
        if len(records.records) != 1:
            raise RuntimeError(f"infer=False ADD 应只产生一个 vector，实际 {len(records.records)}")

        external_id = indexed.metadata["mem0_memory_id"]
        updated = indexed.model_copy(
            update={
                "content": "smoke user now prefers detailed answers",
                "current_revision_id": f"mrev_smoke_update_{suffix}",
                "current_revision_no": 2,
                "index_status": MemoryIndexStatus.PENDING,
            }
        )
        await repository.add(updated)
        update_operation = await outbox.add(
            _index_operation(updated, "update", external_memory_id=external_id)
        )
        await _run_until(worker, update_operation.index_operation_id)
        updated_records = await adapter.scan_provider_records(
            tenant_id=tenant_id, memory_id=item.memory_id
        )
        if (
            len(updated_records.records) != 1
            or updated_records.records[0].external_memory_id != external_id
            or updated_records.records[0].content != updated.content
        ):
            raise RuntimeError("UPDATE 未在已知 external ID 上原位完成")

        delete_result = await lifecycle.request_delete(_delete_operation(updated))
        if delete_result.index_operation is None:
            raise RuntimeError("DELETE 未创建 index operation")
        await _run_until(worker, delete_result.index_operation.index_operation_id)
        if await repository.get_by_id(item.memory_id, tenant_id=tenant_id) is not None:
            raise RuntimeError("provider 成功后 canonical 正文未硬删除")
        deleted_records = await adapter.scan_provider_records(
            tenant_id=tenant_id, memory_id=item.memory_id
        )
        if deleted_records.records:
            raise RuntimeError("provider vector 未硬删除")

        rebuild_item = _smoke_item(
            tenant_id=tenant_id,
            user_id=user_id,
            suffix=f"rebuild_{suffix}",
        )
        await repository.add(rebuild_item)
        rebuilt = await repair.run_tenant(tenant_id=tenant_id, rebuild=True)
        if rebuilt.status != "ok" or rebuilt.added_count < 1:
            raise RuntimeError(f"rebuild 失败: {rebuilt}")
        rebuilt_records = await adapter.scan_provider_records(
            tenant_id=tenant_id, memory_id=rebuild_item.memory_id
        )
        if len(rebuilt_records.records) != 1:
            raise RuntimeError("rebuild 未从 active PostgreSQL projection 恢复单一 vector")
    except Exception as exc:
        failure = exc
    finally:
        try:
            await _cleanup_smoke_tenant(
                repository=repository,
                lifecycle=lifecycle,
                worker=worker,
                repair=repair,
                adapter=adapter,
                session_factory=session_factory,
                tenant_id=tenant_id,
            )
        except Exception as cleanup_exc:
            if failure is None:
                failure = RuntimeError(f"smoke tenant cleanup 失败: {cleanup_exc}")
            else:
                print(f"SMOKE_CLEANUP_FAIL: {cleanup_exc}")
        await session_factory.kw["bind"].dispose()
    if failure is not None:
        print(f"SMOKE_FAIL: mem0 index consistency 闭环失败: {failure}")
        print(f"SMOKE_DEBUG: {health}")
        return 1
    print(
        "SMOKE_OK: PostgreSQL canonical、mem0ai 2.0.11、Milvus Lite 的 "
        "preference ADD/recall/revision UPDATE/delete、infer=False 单 vector、rebuild，"
        "以及 owned Plan continuation 隔离均通过"
    )
    return 0


def _preflight_error(settings: Settings) -> str | None:
    if settings.memory_strategy_provider != "mem0":
        return "请先设置 MEMORY_STRATEGY_PROVIDER=mem0"
    if settings.storage_backend != "database" or not settings.database_url.startswith(
        ("postgresql://", "postgresql+asyncpg://")
    ):
        return "真实 smoke 要求 STORAGE_BACKEND=database 和 PostgreSQL DATABASE_URL"
    if os.getenv("OIR_SMOKE_REAL_FORMATION") == "1" and (
        not settings.router_llm_base_url or not settings.router_llm_api_key
    ):
        return "OIR_SMOKE_REAL_FORMATION=1 要求 ROUTER_LLM_BASE_URL/ROUTER_LLM_API_KEY"
    return None


async def _verify_explicit_write(service: MemoryService, *, tenant_id: str, user_id: str) -> None:
    decisions = await service.write_candidates(
        candidates=[
            MemoryWriteCandidate(
                scope="user_preference",
                content="smoke explicit candidate prefers concise answers",
                confidence=0.95,
                source="smoke",
            )
        ],
        user_id=user_id,
        tenant_id=tenant_id,
    )
    if not decisions or decisions[0].status != "accepted":
        raise RuntimeError(f"显式写入未 accepted: {decisions}")
    recall = await service.recall(
        MemoryRecallRequest(
            query="concise answers",
            user=UserContext(id=user_id, attributes={"tenant_id": tenant_id}),
            scopes=["user_preference"],
            max_items=3,
        )
    )
    if recall.context.status != "ok" or not recall.context.items:
        raise RuntimeError("显式 write-candidates 兼容召回失败")


async def _verify_preference_lifecycle(
    *,
    settings: Settings,
    service: MemoryService,
    repository: DatabaseMemoryItemRepository,
    adapter: Mem0MemoryAdapter,
    revision_repository: DatabaseMemoryRevisionLedgerRepository,
    worker: MemoryIndexOperationWorker,
    tenant_id: str,
    user_id: str,
    suffix: str,
) -> None:
    policy = MemoryCandidatePolicy(settings=settings, repository=repository)
    add_turn = _formation_turn(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=f"pref_add_{suffix}",
        user_text="以后请用简洁中文回答",
    )
    add_job = _formation_job(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=f"pref_add_{suffix}",
        turn_id=add_turn.turn_id,
    )
    add_candidate = MemoryFormationCandidate(
        candidate_id=f"candidate_pref_add_{suffix}",
        proposed_operation="add",
        scope="user_preference",
        content="用户偏好简洁中文回答",
        structured_value={"slot": "response_detail", "value": "concise"},
        subject_id_hint=user_id,
        tenant_id_hint=tenant_id,
        memory_key_hint="response_detail",
        confidence=0.99,
        evidence_refs=[
            MemoryEvidenceRef(
                turn_id=add_turn.turn_id,
                role="user",
                quote=add_turn.user_text,
            )
        ],
    )
    added = await service.lifecycle.apply(
        await policy.evaluate(job=add_job, candidate=add_candidate, turns=[add_turn])
    )
    if added.item is None or added.revision is None or added.index_operation is None:
        raise RuntimeError(f"preference ADD 未产生完整 canonical 状态: {added}")
    await _run_until(worker, added.index_operation.index_operation_id)
    indexed = await repository.get_by_id(added.item.memory_id, tenant_id=tenant_id)
    if indexed is None or not indexed.metadata.get("mem0_memory_id"):
        raise RuntimeError("preference ADD 未保存 provider mapping")
    external_id = indexed.metadata["mem0_memory_id"]

    recalled = await service.recall(
        MemoryRecallRequest(
            query="简洁中文回答",
            user=UserContext(id=user_id, attributes={"tenant_id": tenant_id}),
            scopes=["user_preference"],
            max_items=10,
        )
    )
    if added.item.memory_id not in {value.memory_id for value in recalled.context.items}:
        raise RuntimeError("preference ADD 后真实 mem0 recall 未命中")

    update_turn = _formation_turn(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=f"pref_update_{suffix}",
        user_text="以后请改为详细中文回答",
    )
    update_job = _formation_job(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=f"pref_update_{suffix}",
        turn_id=update_turn.turn_id,
    )
    update_candidate = MemoryFormationCandidate(
        candidate_id=f"candidate_pref_update_{suffix}",
        proposed_operation="update",
        scope="user_preference",
        content="用户偏好详细中文回答",
        structured_value={
            "slot": "response_detail",
            "value": "detailed",
            "change_intent": "explicit_long_term",
        },
        subject_id_hint=user_id,
        tenant_id_hint=tenant_id,
        memory_key_hint="response_detail",
        target_memory_id=added.item.memory_id,
        confidence=0.99,
        evidence_refs=[
            MemoryEvidenceRef(
                turn_id=update_turn.turn_id,
                role="user",
                quote=update_turn.user_text,
            )
        ],
    )
    updated = await service.lifecycle.apply(
        await policy.evaluate(job=update_job, candidate=update_candidate, turns=[update_turn])
    )
    if (
        updated.item is None
        or updated.revision is None
        or updated.index_operation is None
        or updated.item.memory_id != added.item.memory_id
        or updated.revision.revision_no != 2
    ):
        raise RuntimeError(f"preference UPDATE 未保持 stable ID/revision: {updated}")
    await _run_until(worker, updated.index_operation.index_operation_id)
    provider_records = await adapter.scan_provider_records(
        tenant_id=tenant_id,
        memory_id=added.item.memory_id,
    )
    if (
        len(provider_records.records) != 1
        or provider_records.records[0].external_memory_id != external_id
        or provider_records.records[0].content != update_candidate.content
    ):
        raise RuntimeError("preference UPDATE 未原位更新单一 provider vector")
    revisions = await revision_repository.list_for_memory(
        added.item.memory_id,
        tenant_id=tenant_id,
        user_id=user_id,
        subject_type="user",
        subject_id=user_id,
    )
    if [revision.revision_no for revision in revisions] != [1, 2]:
        raise RuntimeError(f"preference revision chain 非 1→2: {revisions}")

    deleted = await MemoryManagementService(memory_service=service).request_delete(
        memory_id=added.item.memory_id,
        tenant_id=tenant_id,
        user_id=user_id,
        actor=user_id,
        reason="real smoke user deletion",
        idempotency_key=f"smoke-delete-preference-{suffix}",
        expected_revision_id=updated.revision.revision_id,
    )
    if not deleted.index_operation_id:
        raise RuntimeError("preference delete 未创建 provider operation")
    await _run_until(worker, deleted.index_operation_id)
    if await repository.get_by_id(added.item.memory_id, tenant_id=tenant_id) is not None:
        raise RuntimeError("preference delete 后 current 正文仍存在")
    deleted_revisions = await revision_repository.list_for_memory(
        added.item.memory_id,
        tenant_id=tenant_id,
        user_id=user_id,
        subject_type="user",
        subject_id=user_id,
    )
    if deleted_revisions:
        raise RuntimeError("preference delete 后 revision 正文仍存在")
    if (
        await adapter.scan_provider_records(tenant_id=tenant_id, memory_id=added.item.memory_id)
    ).records:
        raise RuntimeError("preference delete 后 provider vector 仍存在")


async def _verify_real_formation_provider(
    *,
    settings: Settings,
    service: MemoryService,
    repository: DatabaseMemoryItemRepository,
    worker: MemoryIndexOperationWorker,
    tenant_id: str,
    user_id: str,
    suffix: str,
) -> None:
    formation_settings = settings.model_copy(
        update={
            "memory_formation_mode": "enforced",
            "memory_formation_window_turns": 1,
            "memory_formation_max_attempts": 3,
            "memory_formation_retry_base_seconds": 0.25,
            "memory_formation_retry_max_seconds": 1.0,
        }
    )
    formation = DatabaseMemoryFormationTurnJobRepository(repository.session_factory)
    turn = _formation_turn(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=f"real_provider_{suffix}",
        user_text="明天要拜访一位关注稳健理财的客户，帮我做访前准备。我喜欢吃猪肉。",
    )
    job = _formation_job(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=f"real_provider_{suffix}",
        turn_id=turn.turn_id,
    ).model_copy(
        update={
            "model_version": formation_settings.memory_formation_model_version,
            "prompt_version": formation_settings.memory_formation_prompt_version,
            "policy_version": formation_settings.memory_formation_policy_version,
            "max_attempts": formation_settings.memory_formation_max_attempts,
        }
    )
    await formation.append_turn(turn)
    await formation.add_job(job)
    processor = MemoryFormationProcessor(
        repository=formation,
        memory_repository=repository,
        model=OpenAICompatibleConversationFormationModel(formation_settings),
        policy=MemoryCandidatePolicy(settings=formation_settings, repository=repository),
        lifecycle=service.lifecycle,
    )
    formation_worker = FormationJobWorker(
        settings=formation_settings,
        repository=formation,
        processor=processor,
        owner=f"smoke-real-formation-{suffix}",
    )
    completed = None
    for attempt in range(formation_settings.memory_formation_max_attempts):
        result = await formation_worker.run_once()
        if result is not None and result.status == "completed":
            completed = result
            break
        if result is not None and result.status == "dead_letter":
            raise RuntimeError(f"真实 Formation Provider 重试耗尽: {result.last_error_code}")
        await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
    if completed is None:
        raise RuntimeError("真实 Formation Provider 未在受控重试内完成")
    formed = await repository.list_active(
        tenant_id=tenant_id,
        user_id=user_id,
        scopes=["user_preference"],
        limit=50,
    )
    provider_items = [item for item in formed if item.formation_job_id == job.job_id]
    if not provider_items or not any(
        "猪肉" in item.content or "pork" in item.content.lower() for item in provider_items
    ):
        raise RuntimeError("真实 Formation Provider 未形成猪肉偏好")
    for item in provider_items:
        await _run_until_memory_ready(
            repository=repository,
            worker=worker,
            tenant_id=tenant_id,
            memory_id=item.memory_id,
        )
    recalled = await service.recall(
        MemoryRecallRequest(
            query="我喜欢吃什么？ pork",
            user=UserContext(id=user_id, attributes={"tenant_id": tenant_id}),
            scopes=["user_preference"],
            max_items=10,
        )
    )
    if not {item.memory_id for item in provider_items} & {
        item.memory_id for item in recalled.context.items
    }:
        raise RuntimeError("真实 Formation Provider 形成的猪肉偏好未被 mem0 召回")


async def _verify_plan_continuation(
    *,
    settings: Settings,
    service: MemoryService,
    repository: DatabaseMemoryItemRepository,
    worker: MemoryIndexOperationWorker,
    session_factory,
    tenant_id: str,
    user_id: str,
    suffix: str,
) -> None:
    formation_settings = settings.model_copy(
        update={
            "memory_formation_mode": "enforced",
            "memory_formation_model_timeout_seconds": min(
                settings.memory_formation_model_timeout_seconds,
                settings.memory_formation_lease_seconds / 2,
            ),
        }
    )
    formation_repository = DatabaseMemoryFormationTurnJobRepository(session_factory)
    plan_service = PlanService(DatabasePlanRepository(session_factory))
    plan = await plan_service.save_plan(
        Plan(
            plan_id=f"plan_smoke_{suffix}",
            user_id=user_id,
            tenant_id=tenant_id,
            session_id=f"plan_session_{suffix}",
            status="running",
            steps=[
                {
                    "step_id": "step_smoke_1",
                    "agent_id": "smoke_agent",
                    "description": "Continue the isolated smoke task",
                    "status": "running",
                }
            ],
        ),
        publish=False,
    )
    published = await StructuredFormationPublisher(
        settings=formation_settings,
        repository=formation_repository,
    ).publish_plan(
        plan,
        event_type="create",
        event_id=f"event_plan_smoke_{suffix}",
        source_version="plan-smoke-v1",
        occurred_at=datetime.now(UTC),
        source_order=plan.state_version,
    )
    if published is None:
        raise RuntimeError("Plan structured projection 未创建 formation job")
    processor = MemoryFormationProcessor(
        repository=formation_repository,
        memory_repository=repository,
        model=None,
        policy=MemoryCandidatePolicy(settings=formation_settings, repository=repository),
        lifecycle=service.lifecycle,
    )
    completed = await FormationJobWorker(
        settings=formation_settings,
        repository=formation_repository,
        processor=processor,
        owner=f"smoke-formation-{suffix}",
    ).run_once()
    if completed is None or completed.status != "completed":
        raise RuntimeError(f"Plan structured formation 未完成: {completed}")
    task_items = await repository.list_active(
        tenant_id=tenant_id,
        user_id=user_id,
        scopes=["task_memory"],
        limit=20,
    )
    task_item = next(
        (value for value in task_items if value.structured_value.get("plan_id") == plan.plan_id),
        None,
    )
    if task_item is None:
        raise RuntimeError("Plan structured formation 未创建 task_memory")
    await _run_until_memory_ready(
        repository=repository,
        worker=worker,
        tenant_id=tenant_id,
        memory_id=task_item.memory_id,
    )
    resolver = TaskMemoryPlanResolver(memory_service=service, plan_service=plan_service)
    continued = await resolver.resolve(
        RouteRequest.model_validate(
            {
                "session_id": f"continue_session_{suffix}",
                "user": {
                    "id": user_id,
                    "roles": ["operator"],
                    "attributes": {"tenant_id": tenant_id},
                },
                "input": {"text": "继续上次任务"},
            }
        )
    )
    unrelated = await resolver.resolve(
        RouteRequest.model_validate(
            {
                "session_id": f"new_task_session_{suffix}",
                "user": {
                    "id": user_id,
                    "roles": ["operator"],
                    "attributes": {"tenant_id": tenant_id},
                },
                "input": {"text": "总结一篇全新的文章"},
            }
        )
    )
    if continued is None or continued.plan_id != plan.plan_id:
        raise RuntimeError("owned Plan 未被 continuation query 定位")
    if unrelated is not None:
        raise RuntimeError("无关新任务被错误续接到旧 Plan")


def _formation_turn(
    *, tenant_id: str, user_id: str, suffix: str, user_text: str
) -> MemoryFormationTurn:
    return MemoryFormationTurn(
        turn_id=f"turn_{suffix}",
        request_id=f"request_{suffix}",
        session_id=f"session_{suffix}",
        run_id=f"run_{suffix}",
        user_id=user_id,
        tenant_id=tenant_id,
        user_text=user_text,
        assistant_text="acknowledged",
        result_status="completed",
        completed_at=datetime.now(UTC),
    )


def _formation_job(
    *, tenant_id: str, user_id: str, suffix: str, turn_id: str
) -> MemoryFormationJob:
    return MemoryFormationJob(
        job_id=f"job_{suffix}",
        trigger="manual",
        mode="enforced",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=f"session_{suffix}",
        source_refs=[turn_id],
        idempotency_key=f"smoke:{tenant_id}:{user_id}:{suffix}",
        model_version="smoke-model-v1",
        prompt_version="smoke-prompt-v1",
        policy_version="smoke-policy-v1",
    )


async def _run_until_memory_ready(
    *,
    repository: DatabaseMemoryItemRepository,
    worker: MemoryIndexOperationWorker,
    tenant_id: str,
    memory_id: str,
    limit: int = 100,
) -> None:
    for _ in range(limit):
        item = await repository.get_by_id(memory_id, tenant_id=tenant_id)
        if item is not None and item.index_status == MemoryIndexStatus.READY:
            return
        if await worker.run_once() is None:
            break
    raise RuntimeError(f"memory index 未 ready: {memory_id}")


async def _run_until(
    worker: MemoryIndexOperationWorker, index_operation_id: str, limit: int = 100
) -> None:
    for _ in range(limit):
        result = await worker.run_once()
        if result is None:
            break
        if result.operation.index_operation_id == index_operation_id:
            if not result.completed:
                raise RuntimeError(f"index operation failed: {result.provider_result.error_code}")
            return
    raise RuntimeError(f"index operation 未在 {limit} 次 claim 内完成: {index_operation_id}")


async def _cleanup_smoke_tenant(
    *,
    repository: DatabaseMemoryItemRepository,
    lifecycle: MemoryLifecycleService,
    worker: MemoryIndexOperationWorker,
    repair: MemoryIndexRepairService,
    adapter: Mem0MemoryAdapter,
    session_factory,
    tenant_id: str,
) -> None:
    for _ in range(100):
        if await worker.run_once() is None:
            break
    for item in await repository.list_indexable(tenant_id=tenant_id, limit=100):
        deleted = await lifecycle.request_delete(_delete_operation(item))
        if deleted.index_operation is None:
            raise RuntimeError(f"smoke cleanup 未创建 DELETE: {item.memory_id}")
        await _run_until(worker, deleted.index_operation.index_operation_id)
    await _purge_tenant_records(session_factory, tenant_id=tenant_id)
    repaired = await repair.run_tenant(tenant_id=tenant_id, rebuild=False)
    if repaired.status != "ok" or repaired.failed_count:
        raise RuntimeError(f"smoke cleanup orphan repair 失败: {repaired}")
    provider_records = await adapter.scan_provider_records(tenant_id=tenant_id)
    if provider_records.status != "success" or provider_records.records:
        raise RuntimeError("smoke cleanup 后 provider tenant 仍有残留")
    await _purge_tenant_records(session_factory, tenant_id=tenant_id)


async def _purge_tenant_records(session_factory, *, tenant_id: str) -> None:
    async with session_factory() as session:
        plan_ids = list(
            await session.scalars(select(PlanModel.plan_id).where(PlanModel.tenant_id == tenant_id))
        )
        memory_ids = list(
            await session.scalars(
                select(MemoryItemModel.memory_id).where(MemoryItemModel.tenant_id == tenant_id)
            )
        )
        if plan_ids:
            await session.execute(delete(PlanStepModel).where(PlanStepModel.plan_id.in_(plan_ids)))
        await session.execute(delete(PlanModel).where(PlanModel.tenant_id == tenant_id))
        if memory_ids:
            await session.execute(
                delete(MemoryRevisionModel).where(MemoryRevisionModel.memory_id.in_(memory_ids))
            )
        for model in (
            MemoryIndexOperationModel,
            MemoryEventModel,
            MemoryFormationJobModel,
            MemoryFormationTurnModel,
            MemoryItemModel,
        ):
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.commit()
        for model in (
            PlanModel,
            MemoryIndexOperationModel,
            MemoryEventModel,
            MemoryFormationJobModel,
            MemoryFormationTurnModel,
            MemoryItemModel,
        ):
            remaining = await session.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            )
            if remaining:
                raise RuntimeError(f"smoke cleanup 未清空 {model.__tablename__}")
        if plan_ids:
            remaining_steps = await session.scalar(
                select(func.count())
                .select_from(PlanStepModel)
                .where(PlanStepModel.plan_id.in_(plan_ids))
            )
            if remaining_steps:
                raise RuntimeError("smoke cleanup 未清空 plan_steps")
        if memory_ids:
            remaining_revisions = await session.scalar(
                select(func.count())
                .select_from(MemoryRevisionModel)
                .where(MemoryRevisionModel.memory_id.in_(memory_ids))
            )
            if remaining_revisions:
                raise RuntimeError("smoke cleanup 未清空 memory_revisions")


def _smoke_item(*, tenant_id: str, user_id: str, suffix: str) -> MemoryItem:
    return MemoryItem(
        memory_id=f"mem_smoke_{suffix}",
        memory_key=f"tenant:{tenant_id}:user:{user_id}:preference:response_detail",
        scope="user_preference",
        subject_id=user_id,
        user_id=user_id,
        tenant_id=tenant_id,
        content="smoke user prefers concise answers",
        current_revision_id=f"mrev_smoke_{suffix}",
        current_revision_no=1,
        index_status="pending",
        source="smoke",
    )


def _index_operation(
    item: MemoryItem, operation: str, *, external_memory_id: str | None = None
) -> MemoryIndexOperation:
    return MemoryIndexOperation(
        idempotency_key=(f"smoke:{operation}:{item.memory_id}:{item.current_revision_id}"),
        operation=operation,
        memory_id=item.memory_id,
        revision_id=item.current_revision_id,
        tenant_id=item.tenant_id or "",
        external_memory_id=external_memory_id,
    )


def _delete_operation(item: MemoryItem) -> MemoryLifecycleOperation:
    memory_key = item.memory_key or f"legacy-memory:{item.memory_id}"
    return MemoryLifecycleOperation(
        operation_id=f"smoke-delete:{item.memory_id}:{item.current_revision_id}",
        operation=MemoryOperation.DELETE,
        decision_status=MemoryDecisionStatus.ACCEPTED,
        reason_code=MemoryFormationReasonCode.AUTHORIZED_DELETE,
        tenant_id=item.tenant_id or "",
        user_id=item.user_id or "",
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        memory_key=memory_key,
        candidate_hash=f"sha256:{uuid4().hex}{uuid4().hex}",
        memory_id=item.memory_id,
        revision_id=item.current_revision_id,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
