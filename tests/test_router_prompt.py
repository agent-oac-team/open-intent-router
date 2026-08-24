from datetime import UTC, datetime

from app.prompts.router_prompt import (
    DEFAULT_SYSTEM_PROMPT,
    RouterPromptTemplate,
    route_response_schema_hint,
)
from app.schemas.agents import CandidateAgentV2
from app.schemas.common import UserContext
from app.schemas.routing import LLMRouteInput, RouteContext, RouteRequest
from app.services.context_service import ContextService


def test_router_prompt_uses_default_when_file_missing(registry_service) -> None:
    template = RouterPromptTemplate.from_file("./missing-router-prompt.yaml")

    assert template.system_prompt == DEFAULT_SYSTEM_PROMPT


async def test_router_prompt_loads_yaml_template(tmp_path, registry_service) -> None:
    prompt_file = tmp_path / "router.zh.yaml"
    prompt_file.write_text(
        """
system_prompt: 自定义系统提示词
user_template: |
  自定义用户模板
  {payload_json}
""",
        encoding="utf-8",
    )
    candidates = await registry_service.candidates_for_user(
        UserContext(id="u1", roles=["operator"], attributes={"tenant_id": "t1"})
    )
    payload = LLMRouteInput(
        request=RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize this text"},
            }
        ),
        candidates=candidates,
        context=RouteContext(candidate_agent_ids=[agent.agent_id for agent in candidates]),
    )

    messages = RouterPromptTemplate.from_file(prompt_file).messages(payload)

    assert messages[0] == {"role": "system", "content": "自定义系统提示词"}
    assert "自定义用户模板" in messages[1]["content"]
    assert '"candidate_agents"' in messages[1]["content"]
    assert '"response_schema_hint"' in messages[1]["content"]
    assert '"routing_rules"' in messages[1]["content"]


def test_router_prompt_serializes_datetime_in_request() -> None:
    payload = LLMRouteInput(
        request=RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {
                    "text": "summarize this text",
                    "attachments": [{"created_at": datetime(2026, 1, 1, tzinfo=UTC)}],
                },
            }
        ),
        candidates=[
            CandidateAgentV2(agent_id="summarizer", name="Summarizer", description="Summarize text")
        ],
        context=RouteContext(candidate_agent_ids=["summarizer"]),
    )

    messages = RouterPromptTemplate().messages(payload)

    assert "2026-01-01T00:00:00Z" in messages[1]["content"]


def test_router_prompt_includes_context_pack_debug_data() -> None:
    request = RouteRequest.model_validate(
        {
            "session_id": "s1",
            "user": {"id": "u1", "roles": ["operator"]},
            "input": {"text": "summarize this text"},
        }
    )
    context = ContextService(settings=_settings()).build_route_context(
        request,
        request_id="req_1",
        candidate_agent_ids=["summarizer"],
        evidence=[{"id": "ev1", "content": "routing evidence"}],
    )
    payload = LLMRouteInput(
        request=request,
        candidates=[
            CandidateAgentV2(
                agent_id="summarizer",
                name="Summarizer",
                description="Summarize text",
            )
        ],
        context=context,
    )

    messages = RouterPromptTemplate().messages(payload)

    assert '"context_pack"' in messages[1]["content"]
    assert '"current_input"' in messages[1]["content"]
    assert '"routing evidence"' in messages[1]["content"]


def test_response_schema_hint_excludes_prompt_only_rules() -> None:
    schema_hint = route_response_schema_hint(["summarizer"])

    assert "rules" not in schema_hint
    assert "assistant_message" in schema_hint


def _settings():
    from app.core.config import Settings

    return Settings(storage_backend="memory", registry_backend="database")
