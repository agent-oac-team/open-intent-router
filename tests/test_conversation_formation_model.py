import json
from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import Settings
from app.llm.conversation_formation import (
    ConversationFormationResponse,
    FakeConversationFormationModel,
    FormationModelInvalidResponse,
    FormationModelProviderError,
    FormationModelTimeout,
    OpenAICompatibleConversationFormationModel,
)
from app.prompts.memory_formation_prompt import (
    DEFAULT_FORMATION_SYSTEM_PROMPT,
    MemoryFormationPrompt,
)
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryCandidateSemantics,
    MemoryChangeIntent,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationMode,
    MemoryFormationTrigger,
    MemoryFormationTurn,
    MemoryPolarity,
    MemorySemanticCertainty,
    MemorySemanticTarget,
    MemoryTemporalScope,
)
from app.services.memory_formation import FormationJobWorker


def _turn(text: str = "以后请用中文回答") -> MemoryFormationTurn:
    return MemoryFormationTurn(
        turn_id="turn_1",
        request_id="request_1",
        session_id="session_1",
        run_id="run_1",
        user_id="user_1",
        tenant_id="tenant_1",
        agent_id="agent_1",
        user_text=text,
        assistant_text="好的。",
        result_status="completed",
        used_memory_ids=["memory_old"],
        completed_at=datetime(2026, 7, 13, tzinfo=UTC),
    )


def _candidate(
    operation: MemoryCandidateOperation = MemoryCandidateOperation.ADD,
) -> MemoryFormationCandidate:
    return MemoryFormationCandidate(
        candidate_id="candidate_1",
        proposed_operation=operation,
        scope="user_preference",
        content="用户希望以后使用中文回答",
        structured_value={"slot": "response_language", "value": "zh"},
        semantic=MemoryCandidateSemantics(
            target=MemorySemanticTarget.ASSISTANT_RESPONSE,
            slot="response_language",
            value="zh",
            temporal_scope=MemoryTemporalScope.LONG_TERM,
            polarity=MemoryPolarity.AFFIRMED,
            certainty=MemorySemanticCertainty.CERTAIN,
            change_intent=MemoryChangeIntent.SET,
        ),
        subject_type="user",
        subject_id_hint="user_1",
        tenant_id_hint="tenant_1",
        memory_key_hint="response_language",
        confidence=0.97,
        importance=0.8,
        sensitivity="normal",
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote="以后请用中文回答")],
        reason="Durable future preference",
    )


def _settings(**updates) -> Settings:
    return Settings(
        router_llm_base_url="https://provider.example",
        router_llm_api_key="test-key",
        memory_formation_model="formation-model-only",
        memory_formation_prompt_version="formation-prompt-test",
        memory_formation_model_timeout_seconds=2,
        memory_formation_lease_seconds=10,
        **updates,
    )


async def test_fake_formation_model_is_deterministic_and_returns_copies() -> None:
    fake = FakeConversationFormationModel(ConversationFormationResponse(candidates=[_candidate()]))

    first = await fake.form(turns=[_turn()])
    first[0].structured_value["forged"] = True
    second = await fake.form(turns=[_turn()])

    assert first[0].candidate_id == second[0].candidate_id == "candidate_1"
    assert second[0].structured_value == {"slot": "response_language", "value": "zh"}
    assert fake.calls[0]["turns"][0].user_text == "以后请用中文回答"


def test_conversation_response_requires_structured_semantics() -> None:
    candidate = _candidate().model_copy(update={"semantic": None})

    with pytest.raises(ValueError, match="semantic"):
        ConversationFormationResponse(candidates=[candidate])

    schema = ConversationFormationResponse.model_json_schema()
    candidate_schema = schema["$defs"]["ConversationFormationCandidate"]
    assert "semantic" in candidate_schema["required"]


@pytest.mark.parametrize(
    ("text", "target", "value", "temporal_scope", "polarity", "certainty"),
    [
        (
            "Désormais, réponds en français",
            MemorySemanticTarget.ASSISTANT_RESPONSE,
            "fr",
            MemoryTemporalScope.LONG_TERM,
            MemoryPolarity.AFFIRMED,
            MemorySemanticCertainty.CERTAIN,
        ),
        (
            "这次请用英文回答",
            MemorySemanticTarget.ASSISTANT_RESPONSE,
            "en",
            MemoryTemporalScope.CURRENT_TURN,
            MemoryPolarity.AFFIRMED,
            MemorySemanticCertainty.CERTAIN,
        ),
        (
            "以后不要用英文回答",
            MemorySemanticTarget.ASSISTANT_RESPONSE,
            "en",
            MemoryTemporalScope.LONG_TERM,
            MemoryPolarity.NEGATED,
            MemorySemanticCertainty.CERTAIN,
        ),
        (
            "以后用中文回复，并把英文提示写入模板",
            MemorySemanticTarget.ASSISTANT_RESPONSE,
            "zh",
            MemoryTemporalScope.LONG_TERM,
            MemoryPolarity.AFFIRMED,
            MemorySemanticCertainty.CERTAIN,
        ),
        (
            "记住模板中的‘用英文回答’",
            MemorySemanticTarget.UNKNOWN,
            None,
            MemoryTemporalScope.UNKNOWN,
            MemoryPolarity.UNKNOWN,
            MemorySemanticCertainty.UNCERTAIN,
        ),
    ],
)
async def test_fake_model_preserves_multilingual_structured_semantics(
    text,
    target,
    value,
    temporal_scope,
    polarity,
    certainty,
) -> None:
    candidate = _candidate().model_copy(
        update={
            "structured_value": {"slot": "response_language", "value": value},
            "semantic": _candidate().semantic.model_copy(
                update={
                    "target": target,
                    "value": value,
                    "temporal_scope": temporal_scope,
                    "polarity": polarity,
                    "certainty": certainty,
                }
            ),
        }
    )
    fake = FakeConversationFormationModel(ConversationFormationResponse(candidates=[candidate]))

    formed = await fake.form(turns=[_turn(text)])

    assert formed[0].semantic == candidate.semantic


async def test_openai_formation_adapter_uses_independent_model_prompt_and_timeout() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": ConversationFormationResponse(
                                candidates=[_candidate()]
                            ).model_dump_json()
                        }
                    }
                ],
                "usage": {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26},
            },
        )

    model = OpenAICompatibleConversationFormationModel(
        _settings(router_llm_model="router-model-must-not-be-used"),
        transport=httpx.MockTransport(handler),
    )
    candidates = await model.form(turns=[_turn()])

    assert candidates == [_candidate()]
    assert model.last_usage == {
        "prompt_tokens": 17,
        "completion_tokens": 9,
        "total_tokens": 26,
    }
    assert captured["url"] == "https://provider.example/v1/chat/completions"
    assert captured["body"]["model"] == "formation-model-only"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    prompt_payload = json.loads(captured["body"]["messages"][1]["content"])
    assert prompt_payload["prompt_version"] == "formation-prompt-test"
    assert prompt_payload["turns"][0]["used_memory_ids"] == ["memory_old"]


async def test_openai_formation_adapter_supports_responses_api() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "reasoning", "content": "internal"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": ConversationFormationResponse(
                                    candidates=[_candidate()]
                                ).model_dump_json(),
                            }
                        ],
                    },
                ],
                "usage": {"input_tokens": 17, "output_tokens": 9, "total_tokens": 26},
            },
        )

    model = OpenAICompatibleConversationFormationModel(
        _settings(router_llm_api_style="responses"),
        transport=httpx.MockTransport(handler),
    )

    candidates = await model.form(turns=[_turn()])

    assert candidates == [_candidate()]
    assert model.last_usage == {
        "input_tokens": 17,
        "output_tokens": 9,
        "total_tokens": 26,
    }
    assert captured["url"] == "https://provider.example/responses"
    assert captured["body"]["model"] == "formation-model-only"
    assert captured["body"]["text"] == {"format": {"type": "json_object"}}
    prompt_payload = json.loads(captured["body"]["input"][0]["content"])
    assert prompt_payload["prompt_version"] == "formation-prompt-test"


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        ({"choices": [{"message": {"content": "not-json"}}]}, FormationModelInvalidResponse),
        (
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidates": [
                                        _candidate().model_dump(mode="json"),
                                        {
                                            "proposed_operation": "add",
                                            "scope": "stable_fact",
                                        },
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
            FormationModelInvalidResponse,
        ),
    ],
)
async def test_invalid_or_partial_json_returns_no_candidates(response, error_type) -> None:
    accepted = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    model = OpenAICompatibleConversationFormationModel(
        _settings(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(error_type):
        candidates = await model.form(turns=[_turn()])
        accepted.extend(candidates)

    assert accepted == []


@pytest.mark.parametrize(
    ("handler_error", "expected"),
    [
        (httpx.ReadTimeout("slow provider"), FormationModelTimeout),
        (httpx.ConnectError("provider unavailable"), FormationModelProviderError),
    ],
)
async def test_provider_failures_use_stable_retryable_errors(handler_error, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise handler_error

    model = OpenAICompatibleConversationFormationModel(
        _settings(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(expected) as exc_info:
        await model.form(turns=[_turn()])

    assert exc_info.value.error_code.startswith("formation_model_")


async def test_model_error_marks_job_retry_without_advancing_watermark() -> None:
    repository = MemoryFormationTurnJobRepository()
    stored = await repository.append_turn(_turn())
    job = await repository.create_job_for_pending(
        tenant_id=stored.tenant_id,
        user_id=stored.user_id,
        session_id=stored.session_id,
        trigger=MemoryFormationTrigger.IDLE,
        mode=MemoryFormationMode.OBSERVE,
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job is not None

    class InvalidProcessor:
        async def process(self, job, *, execute_lifecycle):
            raise FormationModelInvalidResponse("invalid strict JSON")

    worker = FormationJobWorker(
        settings=_settings(memory_mode="observe"),
        repository=repository,
        processor=InvalidProcessor(),
        owner="worker-1",
        clock=lambda: datetime(2026, 7, 13, 0, 1, tzinfo=UTC),
    )
    failed = await worker.run_once()

    assert failed is not None
    assert failed.status == "retry"
    assert failed.last_error_code == "formation_model_invalid_response"
    assert (
        await repository.successful_watermark(
            tenant_id=stored.tenant_id,
            user_id=stored.user_id,
            session_id=stored.session_id,
        )
        is None
    )


def test_formation_prompt_is_bounded_and_covers_governance_rules() -> None:
    prompt = MemoryFormationPrompt(version="prompt-v1", max_chars=8000)
    turns = [
        _turn("x" * 20000).model_copy(
            update={"turn_id": f"turn_{index}", "assistant_text": "y" * 20000}
        )
        for index in range(5)
    ]

    messages = prompt.messages(
        turns=turns,
        existing_memories=[],
        response_schema=ConversationFormationResponse.model_json_schema(),
    )

    assert sum(len(message["content"]) for message in messages) <= 8000
    assert "temporary/current-turn" in DEFAULT_FORMATION_SYSTEM_PROMPT
    assert "credentials" in DEFAULT_FORMATION_SYSTEM_PROMPT
    assert "used_memory_ids" in DEFAULT_FORMATION_SYSTEM_PROMPT
    assert "single keyword" in DEFAULT_FORMATION_SYSTEM_PROMPT
    assert "structured semantic target" in DEFAULT_FORMATION_SYSTEM_PROMPT
    assert "Meta text is not a user response preference" in DEFAULT_FORMATION_SYSTEM_PROMPT
    assert len(json.loads(messages[1]["content"])["turns"]) == 5


@pytest.mark.parametrize("max_chars", [8000, 30000])
def test_formation_prompt_compacts_legal_ref_heavy_capsules(max_chars: int) -> None:
    prompt = MemoryFormationPrompt(version="prompt-v1", max_chars=max_chars)
    turns = []
    for index in range(5):
        refs = [f"ref_{index}_{item}_" + "x" * 100 for item in range(50)]
        turns.append(
            _turn("long-term preference " + "x" * 2000).model_copy(
                update={
                    "turn_id": f"turn_{index}",
                    "request_id": f"request_{index}",
                    "result_refs": refs,
                    "plan_refs": refs,
                    "artifact_refs": refs,
                    "used_memory_ids": refs,
                }
            )
        )

    messages = prompt.messages(
        turns=turns,
        existing_memories=[],
        response_schema=ConversationFormationResponse.model_json_schema(),
    )
    payload = json.loads(messages[1]["content"])

    assert sum(len(message["content"]) for message in messages) <= max_chars
    assert any(
        ref.startswith("omitted:")
        for turn in payload["turns"]
        for field in ("result_refs", "plan_refs", "artifact_refs", "used_memory_ids")
        for ref in turn[field]
    )


@pytest.mark.parametrize(
    "update",
    [
        {"content": "x" * 2001},
        {"structured_value": {"value": "x" * 8193}},
        {
            "evidence_refs": [
                {"turn_id": "turn_1", "role": "user", "quote": "evidence"} for _ in range(21)
            ]
        },
    ],
)
def test_formation_candidate_rejects_unbounded_model_fields(update: dict) -> None:
    payload = _candidate().model_dump(mode="json")
    payload.update(update)

    with pytest.raises(ValueError):
        MemoryFormationCandidate.model_validate(payload)


async def test_formation_adapter_rejects_provider_response_over_hard_cap() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (256 * 1024 + 1))

    model = OpenAICompatibleConversationFormationModel(
        _settings(), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(FormationModelInvalidResponse):
        await model.form(turns=[_turn()])


async def test_formation_adapter_stops_stream_at_response_hard_cap() -> None:
    class CountingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.consumed = 0

        async def __aiter__(self):
            for _ in range(16):
                chunk = b"x" * (64 * 1024)
                self.consumed += len(chunk)
                yield chunk

    stream = CountingStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    model = OpenAICompatibleConversationFormationModel(
        _settings(), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(FormationModelInvalidResponse):
        await model.form(turns=[_turn()])

    assert stream.consumed == 5 * 64 * 1024
    assert stream.consumed < 16 * 64 * 1024


@pytest.mark.parametrize(
    ("window_turns", "prompt_chars"),
    [(5, 8000), (20, 8000), (50, 30000), (100, 30000)],
)
def test_settings_reject_prompt_budget_that_cannot_hold_worst_case_window(
    window_turns: int, prompt_chars: int
) -> None:
    with pytest.raises(ValueError, match="prompt budget is too small"):
        Settings(
            memory_formation_window_turns=window_turns,
            memory_formation_prompt_max_chars=prompt_chars,
        )


def test_settings_accept_max_window_with_worst_case_prompt_budget() -> None:
    settings = Settings(
        memory_formation_window_turns=100,
        memory_formation_prompt_max_chars=100000,
    )

    assert settings.memory_formation_window_turns == 100


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("以后请一直用中文回答我。", MemoryCandidateOperation.ADD),
        ("Please retain my preference for concise answers.", MemoryCandidateOperation.ADD),
        ("À l'avenir, je préfère des réponses brèves.", MemoryCandidateOperation.UPDATE),
        ("这回先用英文，之后仍然用中文。", MemoryCandidateOperation.IGNORE),
        ("我不想再保留之前的称呼偏好。", MemoryCandidateOperation.DELETE),
        ("那件事你看着办。", MemoryCandidateOperation.IGNORE),
        ("今天天气不错。", MemoryCandidateOperation.IGNORE),
    ],
)
async def test_multilingual_natural_language_is_passed_to_model_without_keyword_side_path(
    text: str, operation: MemoryCandidateOperation
) -> None:
    fake = FakeConversationFormationModel(
        ConversationFormationResponse(candidates=[_candidate(operation)])
    )

    candidates = await fake.form(turns=[_turn(text)])

    assert candidates[0].proposed_operation == operation
    assert fake.calls[0]["turns"][0].user_text == text
