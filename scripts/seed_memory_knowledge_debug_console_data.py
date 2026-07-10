import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.context_stores import (
    DatabaseKnowledgeRepository,
    DatabaseMemoryItemRepository,
    KnowledgeRepository,
    MemoryItemRepository,
)
from app.schemas.common import UserContext
from app.schemas.knowledge import KnowledgeChunk, KnowledgeSearchRequest, KnowledgeSource
from app.schemas.memory import MemoryRecallRequest, MemoryWriteCandidate
from app.services.knowledge_service import KnowledgeService
from app.services.knowledge_vector_store import MilvusKnowledgeVectorStore
from app.services.memory_service import MemoryService


@dataclass
class SeedResult:
    prefix: str
    storage_backend: str
    memory_decisions: list[dict]
    skipped_memory_contents: list[str]
    rejected_memory_decisions: list[dict]
    knowledge_sources: list[str]
    knowledge_chunks: list[str]
    indexed_chunks: int
    verification: dict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed local E2E memory/knowledge data for the debug console."
    )
    parser.add_argument("--prefix", default="e2e_20260709", help="Seed metadata prefix.")
    parser.add_argument(
        "--allow-memory-backend",
        action="store_true",
        help="Allow seeding when STORAGE_BACKEND is not database.",
    )
    parser.add_argument("--skip-memory", action="store_true", help="Skip memory seed.")
    parser.add_argument("--skip-knowledge", action="store_true", help="Skip knowledge seed.")
    parser.add_argument("--no-verify", action="store_true", help="Skip post-seed verification.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = Settings()
    if settings.storage_backend != "database" and not args.allow_memory_backend:
        print(
            "SEED_FAIL: STORAGE_BACKEND must be database for real E2E seeding "
            "(use --allow-memory-backend only for local dry runs)."
        )
        return 2
    if not args.skip_knowledge and settings.knowledge_vector_backend != "milvus":
        print("SEED_FAIL: KNOWLEDGE_VECTOR_BACKEND must be milvus for this E2E seed.")
        return 2

    memory_repository, knowledge_repository = await _repositories(settings)
    memory_service = MemoryService(settings=settings, repository=memory_repository)
    knowledge_service = KnowledgeService(settings=settings, repository=knowledge_repository)

    memory_decisions: list[dict] = []
    skipped_memory_contents: list[str] = []
    rejected_memory_decisions: list[dict] = []
    if not args.skip_memory:
        memory_decisions, skipped_memory_contents, rejected_memory_decisions = await _seed_memory(
            memory_service,
            memory_repository,
            prefix=args.prefix,
        )

    knowledge_sources: list[str] = []
    knowledge_chunks: list[KnowledgeChunk] = []
    indexed_chunks = 0
    if not args.skip_knowledge:
        knowledge_sources, knowledge_chunks = await _seed_knowledge(
            knowledge_repository,
            prefix=args.prefix,
        )
        vector_store = MilvusKnowledgeVectorStore(
            settings=settings, repository=knowledge_repository
        )
        indexed_chunks = await vector_store.upsert_chunks(knowledge_chunks)
        knowledge_service = KnowledgeService(
            settings=settings,
            repository=knowledge_repository,
            vector_store=vector_store,
        )

    verification = {}
    if not args.no_verify:
        verification = await _verify(
            memory_service=memory_service,
            knowledge_service=knowledge_service,
            skip_memory=args.skip_memory,
            skip_knowledge=args.skip_knowledge,
        )

    result = SeedResult(
        prefix=args.prefix,
        storage_backend=settings.storage_backend,
        memory_decisions=memory_decisions,
        skipped_memory_contents=skipped_memory_contents,
        rejected_memory_decisions=rejected_memory_decisions,
        knowledge_sources=knowledge_sources,
        knowledge_chunks=[chunk.chunk_id for chunk in knowledge_chunks],
        indexed_chunks=indexed_chunks,
        verification=verification,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    if not args.no_verify and _verification_failed(verification, args):
        return 1
    return 0


async def _repositories(settings: Settings):
    if settings.storage_backend != "database":
        return MemoryItemRepository(), KnowledgeRepository()
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    return DatabaseMemoryItemRepository(session_factory), DatabaseKnowledgeRepository(
        session_factory
    )


async def _seed_memory(
    service: MemoryService,
    repository: MemoryItemRepository,
    *,
    prefix: str,
) -> tuple[list[dict], list[str], list[dict]]:
    user_a = "e2e_user_a"
    tenant_a = "e2e_tenant_a"
    user_b = "e2e_user_b"
    tenant_b = "e2e_tenant_b"
    accepted = [
        MemoryWriteCandidate(
            scope="user_preference",
            content=(
                "e2e_user_a prefers concise Chinese answers and dislikes overly salesy wording."
            ),
            source="e2e_seed",
            confidence=0.95,
            importance=0.8,
            metadata={"seed_prefix": prefix},
        ),
        MemoryWriteCandidate(
            scope="stable_fact",
            content=(
                "e2e_user_a usually prepares client visits for cautious "
                "wealth-management customers."
            ),
            source="e2e_seed",
            confidence=0.9,
            importance=0.7,
            metadata={"seed_prefix": prefix},
        ),
        MemoryWriteCandidate(
            scope="task_memory",
            content=(
                "For the current campaign, e2e_user_a wants WeChat copy to mention "
                "liquidity buffers."
            ),
            source="e2e_seed",
            confidence=0.85,
            importance=0.7,
            metadata={"seed_prefix": prefix},
        ),
        MemoryWriteCandidate(
            scope="session_summary",
            content=(
                "The latest e2e session focused on capital preservation and follow-up questions."
            ),
            source="e2e_seed",
            confidence=0.8,
            importance=0.6,
            metadata={"seed_prefix": prefix},
        ),
    ]
    isolated = [
        MemoryWriteCandidate(
            scope="user_preference",
            content="e2e_user_b prefers English summaries for overseas bond products.",
            source="e2e_seed",
            confidence=0.95,
            metadata={"seed_prefix": prefix},
        )
    ]
    skipped_a, missing_a = await _missing_candidates(
        repository,
        user_id=user_a,
        tenant_id=tenant_a,
        candidates=accepted,
    )
    skipped_b, missing_b = await _missing_candidates(
        repository,
        user_id=user_b,
        tenant_id=tenant_b,
        candidates=isolated,
    )
    decisions = []
    if missing_a:
        decisions.extend(
            await service.write_candidates(
                candidates=missing_a,
                user_id=user_a,
                tenant_id=tenant_a,
            )
        )
    if missing_b:
        decisions.extend(
            await service.write_candidates(
                candidates=missing_b,
                user_id=user_b,
                tenant_id=tenant_b,
            )
        )
    rejected = await service.write_candidates(
        candidates=[
            MemoryWriteCandidate(
                scope="stable_fact",
                content="This low-confidence memory should be rejected.",
                source="e2e_negative_seed",
                confidence=0.2,
                metadata={"seed_prefix": prefix},
            ),
            MemoryWriteCandidate(
                scope="stable_fact",
                content="This sensitive memory should be rejected.",
                source="e2e_negative_seed",
                confidence=0.9,
                metadata={"sensitive": True, "seed_prefix": prefix},
            ),
            MemoryWriteCandidate(
                scope="task_memory",
                content="   ",
                source="e2e_negative_seed",
                confidence=0.9,
                metadata={"seed_prefix": prefix},
            ),
        ],
        user_id=user_a,
        tenant_id=tenant_a,
    )
    return (
        [decision.model_dump(mode="json") for decision in decisions],
        skipped_a + skipped_b,
        [decision.model_dump(mode="json") for decision in rejected],
    )


async def _missing_candidates(
    repository: MemoryItemRepository,
    *,
    user_id: str,
    tenant_id: str,
    candidates: list[MemoryWriteCandidate],
) -> tuple[list[str], list[MemoryWriteCandidate]]:
    active = await repository.list_active(
        user_id=user_id,
        tenant_id=tenant_id,
        scopes=[str(candidate.scope) for candidate in candidates],
        limit=200,
    )
    existing_contents = {item.content for item in active}
    skipped = [
        candidate.content for candidate in candidates if candidate.content in existing_contents
    ]
    missing = [candidate for candidate in candidates if candidate.content not in existing_contents]
    return skipped, missing


async def _seed_knowledge(
    repository: KnowledgeRepository,
    *,
    prefix: str,
) -> tuple[list[str], list[KnowledgeChunk]]:
    sources = [
        KnowledgeSource(
            source_id="wealth_product_docs",
            name="E2E Wealth Product Docs",
            description="E2E seeded wealth product knowledge.",
            allow_tenants=["e2e_tenant_a"],
            tags=["e2e", "product", "wealth"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeSource(
            source_id="risk_policy_docs",
            name="E2E Risk Policy Docs",
            description="E2E seeded risk and suitability policy knowledge.",
            allow_tenants=["e2e_tenant_a"],
            tags=["e2e", "risk", "policy"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeSource(
            source_id="visit_playbook",
            name="E2E Visit Playbook",
            description="E2E seeded visit-preparation playbook.",
            allow_tenants=["e2e_tenant_a"],
            tags=["e2e", "visit", "playbook"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeSource(
            source_id="e2e_admin_docs",
            name="E2E Admin Only Docs",
            description="E2E admin-only compliance notes.",
            allow_roles=["admin"],
            tags=["e2e", "admin"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeSource(
            source_id="e2e_disabled_docs",
            name="E2E Disabled Docs",
            description="E2E disabled knowledge source.",
            enabled=False,
            allow_tenants=["e2e_tenant_a"],
            tags=["e2e", "disabled"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeSource(
            source_id="e2e_tenant_b_docs",
            name="E2E Tenant B Docs",
            description="E2E tenant isolation knowledge source.",
            allow_tenants=["e2e_tenant_b"],
            tags=["e2e", "tenant-b"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
    ]
    for source in sources:
        await repository.upsert_source(source)

    chunks = [
        KnowledgeChunk(
            chunk_id="e2e_chunk_wealth_liquidity",
            source_id="wealth_product_docs",
            title="E2E Liquidity Buffer Playbook",
            uri="urn:oir:e2e:wealth:liquidity",
            content=(
                "稳健配置需要保留流动性缓冲 liquidity buffers, 使用低波动资产 "
                "low volatility allocation, and apply drawdown controls for cautious clients."
            ),
            tags=["e2e", "liquidity", "product"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeChunk(
            chunk_id="e2e_chunk_wealth_duration",
            source_id="wealth_product_docs",
            title="E2E Duration Risk Guide",
            uri="urn:oir:e2e:wealth:duration",
            content=(
                "久期风险 duration risk means bond net value may fluctuate when interest "
                "rates change; explain it with rate sensitivity and holding horizon."
            ),
            tags=["e2e", "duration", "product"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeChunk(
            chunk_id="e2e_chunk_risk_suitability",
            source_id="risk_policy_docs",
            title="E2E Suitability Policy",
            uri="urn:oir:e2e:risk:suitability",
            content=(
                "风险适当性要求说明 risk rating, suitability, and that advisors must not "
                "promise guaranteed return or hide drawdown risk."
            ),
            tags=["e2e", "risk", "policy"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeChunk(
            chunk_id="e2e_chunk_visit_questionnaire",
            source_id="visit_playbook",
            title="E2E Visit Preparation Questions",
            uri="urn:oir:e2e:visit:questionnaire",
            content=(
                "访前准备 should collect client objective, liquidity needs, risk tolerance, "
                "recent concerns, likely objections, and follow-up action items."
            ),
            tags=["e2e", "visit"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeChunk(
            chunk_id="e2e_chunk_admin_only",
            source_id="e2e_admin_docs",
            title="E2E Admin Compliance Note",
            uri="urn:oir:e2e:admin:compliance",
            content=(
                "Internal compliance escalation wording for admin review only; ordinary "
                "operator users must not receive this content."
            ),
            tags=["e2e", "admin"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeChunk(
            chunk_id="e2e_chunk_disabled",
            source_id="e2e_disabled_docs",
            title="E2E Disabled Content",
            uri="urn:oir:e2e:disabled",
            content="This disabled source content must never be returned in E2E search.",
            tags=["e2e", "disabled"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
        KnowledgeChunk(
            chunk_id="e2e_chunk_tenant_b",
            source_id="e2e_tenant_b_docs",
            title="E2E Tenant B Content",
            uri="urn:oir:e2e:tenant-b",
            content="Tenant B exclusive bond product note; tenant A must not receive it.",
            tags=["e2e", "tenant-b"],
            metadata={"seed_prefix": prefix, "canonical": True},
        ),
    ]
    stored_chunks = []
    for chunk in chunks:
        stored_chunks.append(await repository.add_chunk(chunk))
    return [source.source_id for source in sources], stored_chunks


async def _verify(
    *,
    memory_service: MemoryService,
    knowledge_service: KnowledgeService,
    skip_memory: bool,
    skip_knowledge: bool,
) -> dict:
    result = {}
    if not skip_memory:
        single_scope = await memory_service.recall(
            MemoryRecallRequest(
                query="concise Chinese answers",
                user=UserContext(
                    id="e2e_user_a",
                    roles=["operator"],
                    groups=["default"],
                    attributes={"tenant_id": "e2e_tenant_a"},
                ),
                scopes=["user_preference"],
                agent_id="script_writer",
                max_items=3,
            )
        )
        multi_scope = await memory_service.recall(
            MemoryRecallRequest(
                query="liquidity buffers cautious customers",
                user=UserContext(
                    id="e2e_user_a",
                    roles=["operator"],
                    groups=["default"],
                    attributes={"tenant_id": "e2e_tenant_a"},
                ),
                scopes=["user_preference", "stable_fact", "task_memory"],
                agent_id="script_writer",
                max_items=5,
            )
        )
        result["memory_single_scope"] = {
            "status": single_scope.context.status,
            "item_ids": [item.memory_id for item in single_scope.context.items],
            "errors": single_scope.context.errors,
        }
        result["memory_multi_scope"] = {
            "status": multi_scope.context.status,
            "item_ids": [item.memory_id for item in multi_scope.context.items],
            "errors": multi_scope.context.errors,
        }
    if not skip_knowledge:
        wealth = await knowledge_service.search(
            KnowledgeSearchRequest(
                query="稳健配置如何处理流动性缓冲 liquidity buffers",
                user=UserContext(
                    id="e2e_user_a",
                    roles=["operator"],
                    groups=["default"],
                    attributes={"tenant_id": "e2e_tenant_a"},
                ),
                caller_type="admin",
                purpose="debug",
                source_ids=["wealth_product_docs"],
                top_k=3,
            )
        )
        denied = await knowledge_service.search(
            KnowledgeSearchRequest(
                query="admin compliance escalation wording",
                user=UserContext(
                    id="e2e_user_a",
                    roles=["operator"],
                    groups=["default"],
                    attributes={"tenant_id": "e2e_tenant_a"},
                ),
                caller_type="admin",
                purpose="debug",
                source_ids=["e2e_admin_docs", "e2e_disabled_docs", "missing_source"],
                top_k=3,
            )
        )
        result["knowledge_wealth"] = {
            "status": wealth.context.status,
            "item_ids": [item.item_id for item in wealth.context.items],
            "selected_source_ids": wealth.selected_source_ids,
            "denied_source_ids": wealth.denied_source_ids,
            "errors": wealth.errors,
        }
        result["knowledge_denied"] = {
            "status": denied.context.status,
            "item_ids": [item.item_id for item in denied.context.items],
            "selected_source_ids": denied.selected_source_ids,
            "denied_source_ids": denied.denied_source_ids,
            "errors": denied.errors,
        }
    return result


def _verification_failed(verification: dict, args: argparse.Namespace) -> bool:
    if not args.skip_memory:
        single = verification.get("memory_single_scope", {})
        multi = verification.get("memory_multi_scope", {})
        if single.get("status") != "ok" or not single.get("item_ids"):
            return True
        if multi.get("status") != "ok" or not multi.get("item_ids"):
            return True
    if not args.skip_knowledge:
        wealth = verification.get("knowledge_wealth", {})
        denied = verification.get("knowledge_denied", {})
        if wealth.get("status") != "ok":
            return True
        if not wealth.get("item_ids") or wealth["item_ids"][0] != "e2e_chunk_wealth_liquidity":
            return True
        expected_denied = {"e2e_admin_docs", "e2e_disabled_docs", "missing_source"}
        if not expected_denied.issubset(set(denied.get("denied_source_ids", []))):
            return True
    return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
