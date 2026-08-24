from pathlib import Path

import pytest

from app.core.errors import RegistryError
from app.repositories.file_registry import FileRegistrySource
from app.schemas.common import UserContext


def test_file_registry_loads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(
        """
agents:
  - agent_id: a1
    schema_version: oir-agent-v2
    name: A1
    description: Test Agent
    handling:
      kind: invocation
      adapter_key: mock
""",
        encoding="utf-8",
    )
    agents = FileRegistrySource(str(path)).load_sync()
    assert [agent.agent_id for agent in agents] == ["a1"]
    assert agents[0].source == "file"


def test_file_registry_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(
        """
agents:
  - agent_id: a1
    schema_version: oir-agent-v2
    name: A1
    description: Test Agent
    handling: {kind: invocation, adapter_key: mock}
  - agent_id: a1
    schema_version: oir-agent-v2
    name: A1 Again
    description: Test Agent
    handling: {kind: invocation, adapter_key: mock}
""",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError):
        FileRegistrySource(str(path)).load_sync()


async def test_registry_filters_by_access_policy(registry_service) -> None:
    allowed = await registry_service.available_for_user(
        UserContext(id="u1", roles=["operator"], attributes={"tenant_id": "tenant_a"})
    )
    denied = await registry_service.available_for_user(UserContext(id="u2", roles=["guest"]))
    assert allowed.available_agents == ["summarizer"]
    assert denied.available_agents == []
