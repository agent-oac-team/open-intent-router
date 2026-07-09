import asyncio
import sys

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.context_stores import DatabaseMemoryItemRepository, MemoryItemRepository
from app.schemas.common import UserContext
from app.schemas.memory import MemoryRecallRequest, MemoryWriteCandidate
from app.services.mem0_config import mem0_health_check
from app.services.memory_service import MemoryService


async def main() -> int:
    settings = Settings()
    if settings.memory_strategy_provider != "mem0":
        print("SMOKE_FAIL: 请先设置 MEMORY_STRATEGY_PROVIDER=mem0")
        return 2
    health = mem0_health_check(settings)
    if health["status"] == "error":
        print(f"SMOKE_FAIL: mem0 配置错误: {health['checks']}")
        return 2
    if not health.get("embedding_api_key_configured"):
        print("SMOKE_FAIL: 请先配置 EMBEDDING_API_KEY 或 KNOWLEDGE_EMBEDDING_API_KEY")
        return 2
    if not health.get("llm_api_key_configured"):
        print("SMOKE_FAIL: 请先配置 ROUTER_LLM_API_KEY 或 MEMORY_MEM0_LLM_API_KEY")
        return 2
    if settings.storage_backend == "database":
        await create_all_tables(settings)
        repository = DatabaseMemoryItemRepository(create_session_factory(settings))
    else:
        repository = MemoryItemRepository()
    service = MemoryService(settings=settings, repository=repository)
    try:
        decisions = await service.write_candidates(
            candidates=[
                MemoryWriteCandidate(
                    scope="user_preference",
                    content="smoke user prefers concise answers",
                    confidence=0.95,
                    source="smoke",
                )
            ],
            user_id="smoke_user",
            tenant_id="smoke_tenant",
        )
        if not decisions or decisions[0].status != "accepted":
            print(f"SMOKE_FAIL: 写入未 accepted: {decisions}")
            return 1
        recall = await service.recall(
            MemoryRecallRequest(
                query="concise answers",
                user=UserContext(id="smoke_user", attributes={"tenant_id": "smoke_tenant"}),
                scopes=["user_preference"],
                max_items=3,
            )
        )
        if recall.context.status != "ok" or not recall.context.items:
            print(f"SMOKE_FAIL: 召回未命中: {recall.model_dump(mode='json')}")
            return 1
    except Exception as exc:
        print(f"SMOKE_FAIL: mem0 记忆闭环失败: {exc}")
        print(f"SMOKE_DEBUG: {health}")
        return 1
    debug = await service.debug_state(user_id="smoke_user", tenant_id="smoke_tenant")
    print(
        "SMOKE_OK: mem0 写入、PostgreSQL/OIR ledger、Milvus Lite collection、"
        f"memory_context 召回闭环通过；events={len(debug.events)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
