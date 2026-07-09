import asyncio
import re
from collections.abc import Mapping

from app.core.config import Settings
from app.schemas.agent_context import (
    AgentRuntimeContext,
    KnowledgeContext,
    MemoryContext,
)
from app.schemas.agents import AgentDefinition
from app.schemas.common import JsonDict, UserContext
from app.schemas.knowledge import KnowledgeSearchRequest
from app.schemas.memory import MemoryRecallRequest
from app.schemas.routing import RouteRequest
from app.services.knowledge_service import KnowledgeService
from app.services.memory_service import MemoryService


class AgentContextAssemblyService:
    def __init__(
        self,
        settings: Settings,
        memory_service: MemoryService,
        knowledge_service: KnowledgeService,
    ) -> None:
        self.settings = settings
        self.memory_service = memory_service
        self.knowledge_service = knowledge_service

    async def assemble_for_route(
        self,
        *,
        agent: AgentDefinition,
        request: RouteRequest,
        invocation_input: JsonDict,
    ) -> AgentRuntimeContext:
        return await self.assemble(
            agent=agent,
            user=request.user,
            session_id=request.session_id,
            query=request.input.text,
            invocation_input=invocation_input,
            caller_type="router",
            caller_id=agent.agent_id,
            purpose="agent_execution",
        )

    async def assemble(
        self,
        *,
        agent: AgentDefinition,
        user: UserContext,
        session_id: str,
        query: str,
        invocation_input: JsonDict,
        caller_type: str = "agent",
        caller_id: str | None = None,
        purpose: str = "agent_execution",
    ) -> AgentRuntimeContext:
        existing_memory_context = self._memory_context_from_input(invocation_input)
        existing_knowledge_context = self._knowledge_context_from_input(invocation_input)
        memory_task = (
            _resolved(existing_memory_context)
            if existing_memory_context is not None
            else self._memory_context(agent, user, query)
        )
        knowledge_task = (
            _resolved(existing_knowledge_context)
            if existing_knowledge_context is not None
            else self._knowledge_context(agent, user, query, caller_type, caller_id, purpose)
        )
        memory_context, knowledge_context = await asyncio.gather(memory_task, knowledge_task)
        memory_context = self._limit_memory_context(memory_context)
        knowledge_context = self._limit_knowledge_context(knowledge_context)
        runtime = AgentRuntimeContext(
            memory_context=memory_context,
            knowledge_context=knowledge_context,
        )
        _attach_context_fields(invocation_input, runtime)
        return runtime

    async def controlled_knowledge_retrieval(
        self,
        *,
        agent: AgentDefinition,
        user: UserContext,
        variables: Mapping[str, object],
        caller_id: str | None = None,
    ) -> KnowledgeContext:
        config = agent.context.knowledge
        if config.mode != "controlled_retrieval" or not config.controlled_retrieval:
            return KnowledgeContext(status="disabled")
        query = _render_template(
            config.controlled_retrieval.query_template,
            variables,
            set(config.controlled_retrieval.allowed_variables),
        )
        response = await self.knowledge_service.search(
            KnowledgeSearchRequest(
                query=query,
                user=user,
                caller_type="agent",
                caller_id=caller_id or agent.agent_id,
                purpose="agent_execution",
                source_ids=config.source_ids,
                source_tags=config.source_tags,
                top_k=_configured_limit(
                    config.max_items, self.settings.knowledge_default_max_items
                ),
            )
        )
        return response.context

    def _limit_memory_context(self, context: MemoryContext) -> MemoryContext:
        limit = self.settings.context_per_item_char_limit
        truncated = False
        items = []
        for item in context.items:
            if limit and len(item.content) > limit:
                item = item.model_copy(update={"content": item.content[:limit]})
                truncated = True
            items.append(item)
        return context.model_copy(
            update={"items": items, "truncated": context.truncated or truncated}
        )

    def _limit_knowledge_context(self, context: KnowledgeContext) -> KnowledgeContext:
        limit = self.settings.context_per_item_char_limit
        truncated = False
        items = []
        for item in context.items:
            if limit and len(item.content) > limit:
                item = item.model_copy(update={"content": item.content[:limit]})
                truncated = True
            items.append(item)
        return context.model_copy(
            update={"items": items, "truncated": context.truncated or truncated}
        )

    async def _memory_context(
        self,
        agent: AgentDefinition,
        user: UserContext,
        query: str,
    ) -> MemoryContext:
        config = agent.context.memory
        if config.mode != "prefetch":
            return MemoryContext(status="disabled")
        try:
            response = await asyncio.wait_for(
                self.memory_service.recall(
                    MemoryRecallRequest(
                        query=query,
                        user=user,
                        scopes=config.scopes,
                        agent_id=agent.agent_id,
                        subject_id=user.id,
                        max_items=_configured_limit(
                            config.max_items, self.settings.memory_default_max_items
                        ),
                    )
                ),
                timeout=self.settings.memory_prefetch_timeout_seconds,
            )
            return response.context
        except TimeoutError:
            return MemoryContext(status="timeout", errors=["memory_prefetch_timeout"])
        except Exception as exc:
            return MemoryContext(status="error", errors=[str(exc)])

    async def _knowledge_context(
        self,
        agent: AgentDefinition,
        user: UserContext,
        query: str,
        caller_type: str,
        caller_id: str | None,
        purpose: str,
    ) -> KnowledgeContext:
        config = agent.context.knowledge
        if config.mode != "prefetch":
            return KnowledgeContext(status="disabled")
        response = await self.knowledge_service.search(
            KnowledgeSearchRequest(
                query=query,
                user=user,
                caller_type=caller_type,  # type: ignore[arg-type]
                caller_id=caller_id,
                purpose=purpose,  # type: ignore[arg-type]
                source_ids=config.source_ids,
                source_tags=config.source_tags,
                top_k=_configured_limit(
                    config.max_items, self.settings.knowledge_default_max_items
                ),
            )
        )
        return response.context

    def _memory_context_from_input(self, invocation_input: JsonDict) -> MemoryContext | None:
        value = invocation_input.get("memory_context")
        if value is None:
            return None
        return value if isinstance(value, MemoryContext) else MemoryContext.model_validate(value)

    def _knowledge_context_from_input(self, invocation_input: JsonDict) -> KnowledgeContext | None:
        value = invocation_input.get("knowledge_context")
        if value is None:
            return None
        return (
            value if isinstance(value, KnowledgeContext) else KnowledgeContext.model_validate(value)
        )


def _attach_context_fields(input_values: JsonDict, runtime: AgentRuntimeContext) -> None:
    input_values["memory_context"] = runtime.memory_context.model_dump(mode="json")
    input_values["knowledge_context"] = runtime.knowledge_context.model_dump(mode="json")


def _configured_limit(value: int | None, default: int) -> int:
    return default if value is None else value


async def _resolved(value):
    return value


def _render_template(
    template: str,
    variables: Mapping[str, object],
    allowed_variables: set[str],
) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if allowed_variables and key not in allowed_variables:
            return ""
        return str(_lookup_variable(variables, key) or "")

    return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", replace, template)


def _lookup_variable(variables: Mapping[str, object], key: str) -> object:
    current: object = variables
    for part in key.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current
