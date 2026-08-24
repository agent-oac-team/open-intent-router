import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.core.errors import RegistryError
from app.schemas.agents import AgentDefinitionV2


class FileRegistrySource:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    async def load(self) -> list[AgentDefinitionV2]:
        return self.load_sync()

    def load_sync(self) -> list[AgentDefinitionV2]:
        if not self.path.exists():
            raise RegistryError(f"Registry file not found: {self.path}")
        data = self._read()
        raw_agents = data.get("agents")
        if not isinstance(raw_agents, list):
            raise RegistryError("Registry file must contain an agents list")

        seen: set[str] = set()
        agents: list[AgentDefinitionV2] = []
        for item in raw_agents:
            if not isinstance(item, dict):
                raise RegistryError("Each registry item must be an object")
            item = dict(item)
            item["source"] = "file"
            try:
                agent = AgentDefinitionV2.model_validate(item)
            except ValidationError as exc:
                raise RegistryError(
                    "Registry file contains an invalid Native v2 Definition"
                ) from exc
            if agent.agent_id in seen:
                raise RegistryError(f"Duplicate agent_id in registry file: {agent.agent_id}")
            seen.add(agent.agent_id)
            agents.append(agent)
        return agents

    def _read(self) -> dict[str, Any]:
        suffix = self.path.suffix.lower()
        text = self.path.read_text(encoding="utf-8")
        if suffix == ".json":
            data = json.loads(text)
        elif suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(text)
        else:
            raise RegistryError("Registry file must be YAML or JSON")
        if not isinstance(data, dict):
            raise RegistryError("Registry file root must be an object")
        return data
