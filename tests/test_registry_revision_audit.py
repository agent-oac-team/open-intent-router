from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.repositories.database import (
    DatabaseAgentDefinitionRepository,
)
from app.repositories.database import (
    RegistryVersionConflict as DatabaseVersionConflict,
)
from app.repositories.memory import (
    MemoryAgentDefinitionRepository,
)
from app.repositories.memory import (
    RegistryVersionConflict as MemoryVersionConflict,
)
from app.repositories.registry_audit import (
    DatabaseRegistryAuditStore,
    MemoryRegistryAuditStore,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.registry_audit import RegistryAuditRecord


def _agent(description: str = "description") -> AgentDefinitionV2:
    return AgentDefinitionV2(
        schema_version="oir-agent-v2",
        agent_id="agent-1",
        name="Agent",
        description=description,
        handling={"kind": "invocation", "adapter_key": "mock", "config": {"function": "run"}},
    )


@pytest.fixture(params=["memory", "database"])
async def registry_repositories(request, tmp_path, managed_database):
    if request.param == "memory":
        return (
            MemoryAgentDefinitionRepository(),
            MemoryRegistryAuditStore(),
            MemoryVersionConflict,
        )
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'registry-revision.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    return (
        DatabaseAgentDefinitionRepository(factory),
        DatabaseRegistryAuditStore(factory),
        DatabaseVersionConflict,
    )


async def test_registry_revision_rejects_stale_update_and_tracks_enable(registry_repositories):
    repository, _, conflict = registry_repositories
    created = await repository.upsert(_agent(), expected_revision=0)
    updated = await repository.upsert(_agent("updated"), expected_revision=created.revision)
    enabled = await repository.set_enabled("agent-1", False, expected_revision=updated.revision)

    assert created.revision == 1
    assert updated.revision == 2
    assert enabled and enabled.revision == 3 and enabled.enabled is False
    with pytest.raises(conflict, match="revision conflict"):
        await repository.upsert(_agent("stale"), expected_revision=1)
    current = await repository.get("agent-1")
    assert current and current.description == "updated"


async def test_registry_audit_persists_operator_source_and_diff(registry_repositories):
    _, audit, _ = registry_repositories
    record = RegistryAuditRecord(
        revision_id="revision-1",
        agent_id="agent-1",
        revision=2,
        operation="update",
        operator_id="admin-1",
        source="oac_host_adapter",
        before={"description": "old"},
        after={"description": "new"},
        created_at=datetime.now(UTC),
    )
    await audit.append(record)

    stored = await audit.list_for_agent("agent-1")
    assert stored == [record]
