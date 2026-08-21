import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import RegistryVersionConflict
from app.repositories.database import DatabaseAgentDefinitionRepository
from app.repositories.memory import MemoryAgentDefinitionRepository
from app.repositories.registry_audit import DatabaseRegistryAuditStore
from app.schemas.agents import AccessPolicy, AgentDefinition, InvocationSpec
from app.schemas.registry_mutation import RegistryMutationCommand


def _agent(description: str = "description") -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        name="Agent",
        description=description,
        type="mock",
        access_policy=AccessPolicy(any_entitlements=["workspace.ops.access"]),
        invocation=InvocationSpec(type="mock"),
    )


def _command(operation, revision, definition=None, revision_id="revision-1"):
    return RegistryMutationCommand(
        operation=operation,
        agent_id="agent-1",
        actor_id="admin-1",
        source="test",
        expected_revision=revision,
        definition=definition,
        revision_id=revision_id,
    )


@pytest.fixture(params=["memory", "database"])
async def atomic_repository(request, tmp_path, managed_database):
    if request.param == "memory":
        repository = MemoryAgentDefinitionRepository()
        return repository, lambda: repository.registry_audit
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'atomic-registry.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    repository = DatabaseAgentDefinitionRepository(factory)
    audit = DatabaseRegistryAuditStore(factory)
    return repository, lambda: audit.list_for_agent("agent-1")


async def test_registry_mutations_commit_definition_and_audit_together(atomic_repository) -> None:
    repository, audit_records = atomic_repository
    created = await repository.mutate(_command("create", 0, _agent()))
    updated = await repository.mutate(
        _command("update", 1, _agent("updated"), revision_id="revision-2")
    )
    disabled = await repository.mutate(_command("disable", 2, revision_id="revision-3"))

    records = audit_records()
    if asyncio.iscoroutine(records):
        records = await records
    assert created.after and created.after.revision == 1
    assert updated.after and updated.after.description == "updated"
    assert disabled.after and disabled.after.enabled is False
    assert [record.operation for record in records] == ["create", "update", "disable"]


async def test_revision_conflict_has_no_partial_definition_or_audit(atomic_repository) -> None:
    repository, audit_records = atomic_repository
    await repository.mutate(_command("create", 0, _agent()))
    with pytest.raises(RegistryVersionConflict):
        await repository.mutate(
            _command("update", 0, _agent("stale"), revision_id="stale-revision")
        )

    current = await repository.get("agent-1")
    records = audit_records()
    if asyncio.iscoroutine(records):
        records = await records
    assert current and current.description == "description"
    assert len(records) == 1


async def test_database_audit_insert_failure_rolls_back_definition(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'audit-rollback.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseAgentDefinitionRepository(await managed_database.session_factory(settings))
    await repository.mutate(_command("create", 0, _agent(), revision_id="duplicate"))

    with pytest.raises(IntegrityError):
        await repository.mutate(
            _command("update", 1, _agent("must-rollback"), revision_id="duplicate")
        )
    current = await repository.get("agent-1")
    assert current and current.revision == 1 and current.description == "description"


@pytest.mark.skipif(
    not os.getenv("OIR_TEST_POSTGRESQL_URL"),
    reason="OIR_TEST_POSTGRESQL_URL is required for PostgreSQL locking semantics",
)
async def test_postgresql_concurrent_revision_update_has_one_winner(managed_database) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=os.environ["OIR_TEST_POSTGRESQL_URL"],
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseAgentDefinitionRepository(await managed_database.session_factory(settings))
    agent_id = f"concurrency-{uuid4().hex}"
    agent = _agent().model_copy(update={"agent_id": agent_id})
    created = await repository.mutate(
        RegistryMutationCommand(
            operation="create",
            agent_id=agent_id,
            actor_id="test",
            source="postgresql-concurrency-test",
            expected_revision=0,
            definition=agent,
        )
    )

    async def update_once(index: int):
        return await repository.mutate(
            RegistryMutationCommand(
                operation="update",
                agent_id=agent_id,
                actor_id=f"test-{index}",
                source="postgresql-concurrency-test",
                expected_revision=created.after.revision,
                definition=agent.model_copy(update={"description": f"winner-{index}"}),
            )
        )

    results = await asyncio.gather(update_once(1), update_once(2), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, RegistryVersionConflict) for item in results) == 1
