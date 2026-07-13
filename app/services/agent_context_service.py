import asyncio
import json
import re
from collections.abc import Mapping
from math import ceil
from uuid import uuid4

from app.core.config import Settings
from app.schemas.agent_context import (
    AgentRuntimeContext,
    KnowledgeContext,
    KnowledgeContextItem,
    MemoryContext,
    MemoryContextItem,
)
from app.schemas.agents import AgentDefinition
from app.schemas.common import JsonDict, UserContext
from app.schemas.context import ContextAssemblySession, ContextBudget, ContextCandidate
from app.schemas.knowledge import KnowledgeSearchRequest
from app.schemas.memory import MemoryRecallRequest
from app.schemas.routing import RouteRequest
from app.services.context_pipeline_service import ContextPipelineService
from app.services.context_providers import (
    KnowledgeRetrievalProvider,
    MemoryRetrievalProvider,
    StaticCandidatesProvider,
)
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
        self.pipeline = ContextPipelineService(settings)

    async def assemble_for_route(
        self,
        *,
        agent: AgentDefinition,
        request: RouteRequest,
        invocation_input: JsonDict,
        assembly_session=None,
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
            request=request,
            assembly_session=assembly_session,
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
        request: RouteRequest | None = None,
        assembly_session: ContextAssemblySession | None = None,
    ) -> AgentRuntimeContext:
        existing_memory_context = self._memory_context_from_input(invocation_input)
        existing_knowledge_context = self._knowledge_context_from_input(invocation_input)
        route_request = request or RouteRequest.model_validate(
            {
                "request_id": f"invoke_{uuid4().hex}",
                "session_id": session_id,
                "user": user.model_dump(mode="json"),
                "input": {"text": query or " "},
            }
        )
        session = assembly_session or ContextAssemblySession(
            assembly_id=f"assembly_{uuid4().hex}",
            request_id=route_request.request_id or f"invoke_{uuid4().hex}",
            user_id=user.id,
            tenant_id=user.tenant_id,
        )
        providers = []
        if existing_memory_context is not None:
            providers.append(
                StaticCandidatesProvider(
                    "existing_memory",
                    _memory_candidates(existing_memory_context, route_request, agent.agent_id),
                    status=existing_memory_context.status,
                    error_code=(existing_memory_context.errors or [None])[0],
                )
            )
        else:
            providers.append(
                MemoryRetrievalProvider(self.settings, self.memory_service, stage="agent")
            )
        if existing_knowledge_context is not None:
            providers.append(
                StaticCandidatesProvider(
                    "existing_knowledge",
                    _knowledge_candidates(
                        existing_knowledge_context, route_request, agent.agent_id
                    ),
                    status=existing_knowledge_context.status,
                    error_code=(existing_knowledge_context.errors or [None])[0],
                )
            )
        else:
            providers.append(
                KnowledgeRetrievalProvider(self.settings, self.knowledge_service, stage="agent")
            )
        base_input = {
            key: value
            for key, value in invocation_input.items()
            if key not in {"memory_context", "knowledge_context"}
        }
        base_tokens = _token_estimate(
            _stable_json(base_input), self.settings.context_chars_per_token
        )
        available_tokens = max(1, self.settings.context_agent_token_budget - base_tokens)
        result = await self.pipeline.assemble(
            request=route_request,
            purpose="agent_execution",
            consumer=f"agent:{agent.agent_id}",
            providers=providers,
            agent=agent,
            budget=ContextBudget(
                max_tokens=available_tokens,
                source_budgets={
                    "memory": available_tokens,
                    "knowledge": available_tokens,
                },
                per_item_token_limit=self.settings.context_per_item_token_limit,
                per_item_char_limit=self.settings.context_per_item_char_limit,
                chars_per_token=self.settings.context_chars_per_token,
                allow_summary_placeholder=False,
            ),
            assembly_session=session,
        )
        memory_context = _memory_context_from_result(
            result,
            existing=existing_memory_context,
        )
        knowledge_context = _knowledge_context_from_result(
            result,
            existing=existing_knowledge_context,
        )
        memory_context, knowledge_context, input_tokens = _fit_runtime_context(
            base_input,
            memory_context,
            knowledge_context,
            max_tokens=self.settings.context_agent_token_budget,
            chars_per_token=self.settings.context_chars_per_token,
        )
        runtime = AgentRuntimeContext(
            memory_context=memory_context,
            knowledge_context=knowledge_context,
            metadata={
                "context_pack": {
                    "pack_id": result.pack.pack_id,
                    "trace_id": result.pack.trace_id,
                    "purpose": result.pack.purpose,
                    "consumer": result.pack.consumer,
                    "budget": result.pack.budget.model_dump(mode="json"),
                    "usage": result.pack.usage.model_dump(mode="json"),
                    "policy_version": result.pack.policy_version,
                    "budget_version": result.pack.budget_version,
                    "projection_version": result.pack.projection_version,
                },
                "context_trace": {
                    "trace_id": result.trace.trace_id,
                    "provider_outcomes": [
                        item.model_dump(mode="json") for item in result.trace.provider_outcomes
                    ],
                    "decisions": [item.model_dump(mode="json") for item in result.trace.decisions],
                    "projection_hash": result.trace.projection_hash,
                },
                "input_token_estimate": input_tokens,
            },
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


def _memory_candidates(
    context: MemoryContext,
    request: RouteRequest,
    agent_id: str,
) -> list[ContextCandidate]:
    return [
        ContextCandidate(
            candidate_id=f"memory:{item.memory_id}",
            source="memory",
            scope="user",
            content=item.content,
            structured_value=item.model_dump(mode="json"),
            purpose="agent_execution",
            consumers=[f"agent:{agent_id}"],
            authority="derived",
            visibility=["agent"],
            source_ref=f"memory:{item.memory_id}",
            dedupe_key=f"memory:{item.memory_id}",
            confidence=item.confidence,
            priority=64,
            relevance=item.relevance,
            expires_at=item.ttl_expires_at,
            allowed_agent_ids=[agent_id],
            metadata={**item.metadata, "memory_id": item.memory_id, "scope": item.scope},
        )
        for item in context.items
    ]


def _knowledge_candidates(
    context: KnowledgeContext,
    request: RouteRequest,
    agent_id: str,
) -> list[ContextCandidate]:
    return [
        ContextCandidate(
            candidate_id=f"knowledge:{item.source_id}:{item.item_id}",
            source="knowledge",
            scope="global",
            content=item.content,
            structured_value=item.model_dump(mode="json"),
            purpose="agent_execution",
            consumers=[f"agent:{agent_id}"],
            authority="derived",
            visibility=["agent"],
            source_ref=f"source:{item.source_id}:{item.item_id}",
            dedupe_key=f"source:{item.source_id}:{item.item_id}",
            confidence=item.score,
            priority=68,
            relevance=item.score,
            allowed_agent_ids=[agent_id],
            metadata={
                **item.metadata,
                "item_id": item.item_id,
                "source_id": item.source_id,
                "citation": item.citation.model_dump(mode="json") if item.citation else None,
            },
        )
        for item in context.items
    ]


def _memory_context_from_result(result, *, existing: MemoryContext | None) -> MemoryContext:
    outcome = _provider_outcome(result, "existing_memory", "agent_memory")
    items = []
    for item in result.pack.items:
        if item.source != "memory":
            continue
        value = item.structured_value or {}
        items.append(
            MemoryContextItem(
                memory_id=str(
                    value.get("memory_id") or item.metadata.get("memory_id") or item.item_id
                ),
                scope=str(value.get("scope") or item.metadata.get("scope") or "stable_fact"),
                content=item.content,
                relevance=item.relevance,
                confidence=float(value.get("confidence") or 1.0),
                importance=float(value.get("importance") or 0.5),
                source=str(value.get("source") or "memory"),
                subject_type=value.get("subject_type"),
                subject_id=value.get("subject_id"),
                ttl_expires_at=value.get("ttl_expires_at"),
                metadata=item.metadata,
            )
        )
    status = _context_status(outcome.status if outcome else None, default="empty")
    if existing is not None:
        status = existing.status
    return MemoryContext(
        summary=(
            existing.summary
            if existing and existing.summary
            else _summary(item.content for item in items)
        ),
        items=items,
        status=status,
        truncated=bool(existing and existing.truncated)
        or any(item.truncated for item in result.pack.items if item.source == "memory"),
        errors=(existing.errors if existing else _outcome_errors(outcome)),
        metadata=existing.metadata if existing else {},
    )


def _knowledge_context_from_result(
    result, *, existing: KnowledgeContext | None
) -> KnowledgeContext:
    outcome = _provider_outcome(result, "existing_knowledge", "agent_knowledge")
    items = []
    for item in result.pack.items:
        if item.source != "knowledge":
            continue
        value = item.structured_value or {}
        citation_value = value.get("citation") or item.metadata.get("citation")
        items.append(
            KnowledgeContextItem(
                item_id=str(value.get("item_id") or item.metadata.get("item_id") or item.item_id),
                source_id=str(
                    value.get("source_id") or item.metadata.get("source_id") or "unknown"
                ),
                content=item.content,
                score=item.relevance,
                title=value.get("title"),
                uri=value.get("uri"),
                citation=citation_value,
                metadata=item.metadata,
            )
        )
    citations = [item.citation for item in items if item.citation]
    source_ids = list(dict.fromkeys(item.source_id for item in items))
    status = _context_status(outcome.status if outcome else None, default="disabled")
    if existing is not None:
        status = existing.status
    return KnowledgeContext(
        summary=(
            existing.summary
            if existing and existing.summary
            else _summary(item.content for item in items)
        ),
        items=items,
        citations=citations,
        source_ids=existing.source_ids if existing and existing.source_ids else source_ids,
        status=status,
        truncated=bool(existing and existing.truncated)
        or any(item.truncated for item in result.pack.items if item.source == "knowledge"),
        errors=(existing.errors if existing else _outcome_errors(outcome)),
        metadata=existing.metadata if existing else {},
    )


def _provider_outcome(result, *names):
    return next(
        (item for item in result.trace.provider_outcomes if item.provider in set(names)),
        None,
    )


def _context_status(status: str | None, *, default: str) -> str:
    if status == "skipped":
        return "disabled"
    if status in {"ok", "empty", "timeout", "error", "denied", "disabled"}:
        return status
    return default


def _outcome_errors(outcome) -> list[str]:
    return [outcome.error_code] if outcome and outcome.error_code else []


def _summary(values) -> str:
    return "\n".join(f"- {value}" for value in values)


def _fit_runtime_context(
    base_input: JsonDict,
    memory: MemoryContext,
    knowledge: KnowledgeContext,
    *,
    max_tokens: int,
    chars_per_token: float,
) -> tuple[MemoryContext, KnowledgeContext, int]:
    memory_items = list(memory.items)
    knowledge_items = list(knowledge.items)

    def estimate() -> int:
        payload = {
            **base_input,
            "memory_context": memory.model_copy(update={"items": memory_items}).model_dump(
                mode="json"
            ),
            "knowledge_context": knowledge.model_copy(
                update={
                    "items": knowledge_items,
                    "citations": [item.citation for item in knowledge_items if item.citation],
                    "source_ids": list(dict.fromkeys(item.source_id for item in knowledge_items)),
                }
            ).model_dump(mode="json"),
        }
        return _token_estimate(_stable_json(payload), chars_per_token)

    truncated_memory = memory.truncated
    truncated_knowledge = knowledge.truncated
    while estimate() > max_tokens and (knowledge_items or memory_items):
        if knowledge_items:
            knowledge_items.pop()
            truncated_knowledge = True
        elif memory_items:
            memory_items.pop()
            truncated_memory = True
    memory = memory.model_copy(update={"items": memory_items, "truncated": truncated_memory})
    knowledge = knowledge.model_copy(
        update={
            "items": knowledge_items,
            "citations": [item.citation for item in knowledge_items if item.citation],
            "source_ids": list(dict.fromkeys(item.source_id for item in knowledge_items)),
            "truncated": truncated_knowledge,
        }
    )
    return memory, knowledge, estimate()


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _token_estimate(value: str, chars_per_token: float) -> int:
    return max(1, ceil(len(value) / max(chars_per_token, 0.1))) if value else 0
