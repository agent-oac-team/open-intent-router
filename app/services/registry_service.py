from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.errors import RegistryError, RegistryUnavailableError
from app.repositories.file_registry import FileRegistrySource
from app.repositories.interfaces import AgentDefinitionRepository
from app.schemas.agents import (
    AgentAdminListResponse,
    AgentDefinitionV2,
    AgentListResponse,
    AgentPublicV2,
    AvailableAgentsResponse,
    CandidateAgentV2,
)
from app.schemas.common import UserContext
from app.schemas.registry_mutation import RegistryMutationCommand, RegistryMutationResult

RegistryStatus = Literal["ok", "degraded", "error"]


@dataclass
class RegistryState:
    status: RegistryStatus = "error"
    active_source: str = "none"
    message: str = ""
    agents: list[AgentDefinitionV2] = field(default_factory=list)


class AgentRegistryService:
    def __init__(
        self,
        settings: Settings,
        repository: AgentDefinitionRepository | None = None,
        file_source: FileRegistrySource | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.file_source = file_source or FileRegistrySource(settings.registry_file_path)
        self.state = RegistryState()

    async def load(self) -> RegistryState:
        backend = self.settings.registry_backend
        if backend == "file":
            return await self._load_file(status="ok")
        if backend == "database":
            return await self._load_database(required=True)
        return await self._load_hybrid()

    async def reload(self) -> RegistryState:
        return await self.load()

    async def list_definitions(self, *, enabled_only: bool = False) -> list[AgentDefinitionV2]:
        if not self.state.agents:
            await self.load()
        agents = self.state.agents
        if enabled_only:
            agents = [agent for agent in agents if agent.enabled]
        return sorted(agents, key=lambda item: (-item.priority, item.agent_id))

    async def get_definition(self, agent_id: str) -> AgentDefinitionV2 | None:
        if self.state.active_source == "database" and self.repository:
            return await self.repository.get(agent_id)
        for agent in await self.list_definitions():
            if agent.agent_id == agent_id:
                return agent
        return None

    async def upsert_definition(
        self, definition: AgentDefinitionV2, *, expected_revision: int | None = None
    ) -> AgentDefinitionV2:
        repository = self._require_writable_repository()
        saved = await repository.upsert(definition, expected_revision=expected_revision)
        await self.reload()
        return saved

    async def set_enabled(
        self, agent_id: str, enabled: bool, *, expected_revision: int | None = None
    ) -> AgentDefinitionV2 | None:
        repository = self._require_writable_repository()
        saved = await repository.set_enabled(agent_id, enabled, expected_revision=expected_revision)
        await self.reload()
        return saved

    async def delete_definition(
        self, agent_id: str, *, expected_revision: int | None = None
    ) -> bool:
        repository = self._require_writable_repository()
        deleted = await repository.delete(agent_id, expected_revision=expected_revision)
        await self.reload()
        return deleted

    async def mutate_definition(self, command: RegistryMutationCommand) -> RegistryMutationResult:
        repository = self._require_writable_repository()
        result = await repository.mutate(command)
        await self.reload()
        return result

    async def list_public(self) -> AgentListResponse:
        return AgentListResponse(
            agents=[agent.to_public() for agent in await self.list_definitions()]
        )

    async def list_admin(self) -> AgentAdminListResponse:
        return AgentAdminListResponse(
            agents=[agent.to_admin() for agent in await self.list_definitions()]
        )

    async def available_for_user(self, user: UserContext) -> AvailableAgentsResponse:
        agents = await self.available_definitions(user)
        candidates = [agent.to_candidate() for agent in agents]
        return AvailableAgentsResponse(
            available_agents=[agent.agent_id for agent in agents],
            candidate_agents_for_llm=candidates,
            source=self.state.active_source,
        )

    async def available_definitions(self, user: UserContext) -> list[AgentDefinitionV2]:
        return [
            agent
            for agent in await self.list_definitions(enabled_only=True)
            if agent.is_available_to(user)
        ]

    async def candidates_for_user(self, user: UserContext) -> list[CandidateAgentV2]:
        return (await self.available_for_user(user)).candidate_agents_for_llm

    async def public_agent(self, agent_id: str) -> AgentPublicV2 | None:
        agent = await self.get_definition(agent_id)
        return agent.to_public() if agent else None

    async def _load_file(self, *, status: RegistryStatus) -> RegistryState:
        agents = await self.file_source.load()
        self.state = RegistryState(status=status, active_source="file", agents=agents)
        return self.state

    async def _load_database(self, *, required: bool) -> RegistryState:
        repository = self._repository()
        try:
            agents = await repository.list(enabled_only=False)
        except (SQLAlchemyError, OSError) as exc:
            if required:
                self.state = RegistryState(
                    status="error",
                    active_source="database",
                    message=str(exc),
                )
                raise RegistryUnavailableError("Database registry is unavailable") from exc
            raise

        if not agents and self.settings.registry_file_fallback_on_empty:
            return await self._load_file(status="degraded")
        self.state = RegistryState(status="ok", active_source="database", agents=agents)
        return self.state

    async def _load_hybrid(self) -> RegistryState:
        try:
            return await self._load_database(required=False)
        except (SQLAlchemyError, OSError, RegistryUnavailableError) as exc:
            try:
                agents = await self.file_source.load()
            except RegistryError:
                self.state = RegistryState(
                    status="error",
                    active_source="none",
                    message=str(exc),
                )
                raise
            self.state = RegistryState(
                status="degraded",
                active_source="file",
                message="Database unavailable; using file registry fallback",
                agents=agents,
            )
            return self.state

    def _repository(self) -> AgentDefinitionRepository:
        if self.repository:
            return self.repository
        raise RegistryUnavailableError("Registry repository is unavailable")

    def _require_writable_repository(self) -> AgentDefinitionRepository:
        if self.settings.registry_backend == "file":
            raise RegistryError("File registry backend is read-only for CRUD operations")
        return self._repository()
