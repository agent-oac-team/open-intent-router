from sqlalchemy import inspect, text

from app.core.config import Settings
from app.db.session import create_all_tables, create_engine, create_session_factory
from app.repositories.context_stores import (
    DatabaseKnowledgeRepository,
    DatabaseMemoryItemRepository,
)
from app.schemas.knowledge import KnowledgeChunk, KnowledgeRetrievalLog, KnowledgeSource
from app.schemas.memory import MemoryEvent, MemoryItem


async def test_create_all_tables_adds_agent_context_column_to_existing_database(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    settings = Settings(storage_backend="database", database_url=database_url)
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE agent_definitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id VARCHAR(128) UNIQUE,
                    name VARCHAR(200),
                    description TEXT,
                    type VARCHAR(64),
                    enabled BOOLEAN
                )
                """
            )
        )
    await engine.dispose()

    await create_all_tables(settings)

    engine = create_engine(settings)
    async with engine.begin() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"] for column in inspect(sync_conn).get_columns("agent_definitions")
            }
        )
    await engine.dispose()
    assert "context_text" in columns


async def test_database_context_repositories_round_trip(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'context.db'}"
    settings = Settings(storage_backend="database", database_url=database_url)
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    memory_repository = DatabaseMemoryItemRepository(session_factory)
    knowledge_repository = DatabaseKnowledgeRepository(session_factory)

    memory = await memory_repository.add(
        MemoryItem(
            scope="user_preference",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            agent_id="agent_a",
            content="prefers concise answers",
            metadata={"source_trace": "test"},
        )
    )
    await memory_repository.add_event(
        MemoryEvent(
            event_type="memory_written",
            memory_id=memory.memory_id,
            user_id="u1",
            tenant_id="t1",
            agent_id="agent_a",
            payload={"scope": "user_preference"},
        )
    )
    active = await memory_repository.list_active(
        user_id="u1",
        tenant_id="t1",
        scopes=["user_preference"],
        subject_id="u1",
        agent_id="agent_a",
    )
    events = await memory_repository.list_events(user_id="u1", tenant_id="t1")

    assert active[0].content == "prefers concise answers"
    assert active[0].metadata["source_trace"] == "test"
    assert events[0].event_type == "memory_written"

    source = await knowledge_repository.upsert_source(
        KnowledgeSource(
            source_id="docs",
            name="Docs",
            allow_tenants=["t1"],
            tags=["product"],
            metadata={"owner": "qa"},
        )
    )
    chunk = await knowledge_repository.add_chunk(
        KnowledgeChunk(
            source_id=source.source_id,
            content="risk rating guide",
            title="Risk Guide",
            uri="https://example.test/risk",
            tags=["product"],
            metadata={"version": "1"},
        )
    )
    await knowledge_repository.add_log(
        KnowledgeRetrievalLog(
            query="risk",
            caller_type="agent",
            caller_id="agent_a",
            purpose="agent_execution",
            user_id="u1",
            tenant_id="t1",
            selected_source_ids=["docs"],
            denied_source_ids=["secret"],
            hit_count=1,
            status="ok",
            errors=[],
            metadata={"hit_count": 1},
        )
    )
    sources = await knowledge_repository.get_sources(["docs"])
    chunks = await knowledge_repository.list_chunks(source_ids=["docs"])
    hits = await knowledge_repository.search_chunks(
        query="risk guide", source_ids=["docs"], limit=5
    )
    logs = await knowledge_repository.list_logs(caller_id="agent_a", tenant_id="t1")

    assert sources[0].metadata["owner"] == "qa"
    assert chunks[0].chunk_id == chunk.chunk_id
    assert hits[0][0].content == "risk rating guide"
    assert logs[0].denied_source_ids == ["secret"]
