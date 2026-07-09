import asyncio
import sys
from uuid import uuid4

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.context_stores import DatabaseKnowledgeRepository, KnowledgeRepository
from app.schemas.common import UserContext
from app.schemas.knowledge import KnowledgeChunk, KnowledgeSearchRequest, KnowledgeSource
from app.services.knowledge_service import KnowledgeService
from app.services.knowledge_vector_store import MilvusKnowledgeVectorStore


async def main() -> int:
    settings = Settings()
    if not settings.knowledge_enabled:
        print("SMOKE_FAIL: 请先设置 KNOWLEDGE_ENABLED=true")
        return 2
    if settings.knowledge_vector_backend != "milvus":
        print("SMOKE_FAIL: 请先设置 KNOWLEDGE_VECTOR_BACKEND=milvus")
        return 2
    if not settings.knowledge_milvus_uri:
        print("SMOKE_FAIL: 请先设置 KNOWLEDGE_MILVUS_URI，例如 .data/oir_knowledge_milvus.db")
        return 2
    if not (settings.knowledge_embedding_api_key or settings.embedding_api_key):
        print("SMOKE_FAIL: 请先配置 KNOWLEDGE_EMBEDDING_API_KEY 或 EMBEDDING_API_KEY")
        return 2
    if not (settings.knowledge_embedding_base_url or settings.embedding_base_url):
        print("SMOKE_FAIL: 请先配置 KNOWLEDGE_EMBEDDING_BASE_URL 或 EMBEDDING_BASE_URL")
        return 2

    if settings.storage_backend == "database":
        await create_all_tables(settings)
        repository = DatabaseKnowledgeRepository(create_session_factory(settings))
    else:
        repository = KnowledgeRepository()

    run_id = uuid4().hex[:12]
    source_id = f"smoke_knowledge_{run_id}"
    target_chunk_id = f"smoke_chunk_target_{run_id}"
    distractor_chunk_id = f"smoke_chunk_distractor_{run_id}"
    await repository.upsert_source(
        KnowledgeSource(
            source_id=source_id,
            name="Smoke Knowledge Source",
            allow_tenants=["smoke_tenant"],
            tags=["smoke", "milvus"],
        )
    )
    target = await repository.add_chunk(
        KnowledgeChunk(
            chunk_id=target_chunk_id,
            source_id=source_id,
            title="Capital Preservation Playbook",
            uri=f"urn:oir:knowledge-smoke:{target_chunk_id}",
            content=(
                "Capital preservation guidance requires liquidity buffers, "
                "low volatility allocation, and drawdown controls."
            ),
        )
    )
    distractor = await repository.add_chunk(
        KnowledgeChunk(
            chunk_id=distractor_chunk_id,
            source_id=source_id,
            title="Office Lunch Menu",
            uri=f"urn:oir:knowledge-smoke:{distractor_chunk_id}",
            content="The office lunch menu includes noodles, fruit, tea, and dessert.",
        )
    )

    vector_store = MilvusKnowledgeVectorStore(settings=settings, repository=repository)
    try:
        indexed = await vector_store.upsert_chunks([target, distractor])
        service = KnowledgeService(
            settings=settings,
            repository=repository,
            vector_store=vector_store,
        )
        response = await service.search(
            KnowledgeSearchRequest(
                query="How should capital preservation handle liquidity buffers?",
                user=UserContext(id="smoke_user", attributes={"tenant_id": "smoke_tenant"}),
                caller_type="admin",
                purpose="debug",
                source_ids=[source_id],
                top_k=2,
            )
        )
    except Exception as exc:
        print(f"SMOKE_FAIL: knowledge Milvus 真实向量检索失败: {exc}")
        return 1

    hit_ids = [item.item_id for item in response.context.items]
    if response.context.status != "ok" or not hit_ids:
        print(f"SMOKE_FAIL: knowledge_context 未命中: {response.model_dump(mode='json')}")
        return 1
    if hit_ids[0] != target_chunk_id:
        print(
            "SMOKE_FAIL: Milvus 语义检索首位不是目标 chunk: "
            f"expected={target_chunk_id} actual={hit_ids[0]} all={hit_ids}"
        )
        return 1
    debug = await service.debug_state(source_ids=[source_id], tenant_id="smoke_tenant")
    if not debug.logs:
        print("SMOKE_FAIL: knowledge_retrieval_logs 未写入")
        return 1

    print(
        "SMOKE_OK: knowledge Milvus Lite 真实向量检索闭环通过；"
        f"collection={settings.knowledge_milvus_collection} indexed={indexed} "
        f"top_hit={hit_ids[0]} logs={len(debug.logs)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
