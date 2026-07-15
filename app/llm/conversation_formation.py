import json
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Protocol

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator

from app.core.config import Settings
from app.core.errors import LLMError
from app.prompts.memory_formation_prompt import MemoryFormationPrompt
from app.schemas.common import StrictBaseModel
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryFormationCandidate,
    MemoryFormationTurn,
    MemoryItem,
)

_PLACEHOLDER_API_KEYS = {
    "replace-with-real-key",
    "your-api-key",
    "your-deepseek-api-key",
}
_MAX_FORMATION_RESPONSE_BYTES = 256 * 1024


class ConversationFormationCandidate(MemoryFormationCandidate):
    semantic: MemoryCandidateSemantics


class ConversationFormationResponse(StrictBaseModel):
    candidates: list[ConversationFormationCandidate] = Field(default_factory=list, max_length=20)

    @field_validator("candidates", mode="before")
    @classmethod
    def normalize_candidate_models(cls, value):
        if not isinstance(value, list):
            return value
        return [
            item.model_dump(mode="python") if isinstance(item, MemoryFormationCandidate) else item
            for item in value
        ]

    @model_validator(mode="after")
    def validate_response_size(self) -> "ConversationFormationResponse":
        if len(self.model_dump_json().encode()) > _MAX_FORMATION_RESPONSE_BYTES:
            raise ValueError("formation response exceeds 256 KiB")
        return self


class ConversationFormationModel(Protocol):
    async def form(
        self,
        *,
        turns: Sequence[MemoryFormationTurn],
        existing_memories: Sequence[MemoryItem] = (),
    ) -> list[MemoryFormationCandidate]: ...


class ConversationFormationModelError(LLMError):
    error_code = "formation_model_error"


class FormationModelInvalidResponse(ConversationFormationModelError):
    error_code = "formation_model_invalid_response"


class FormationModelTimeout(ConversationFormationModelError):
    error_code = "formation_model_timeout"


class FormationModelProviderError(ConversationFormationModelError):
    error_code = "formation_model_provider_error"


def validate_conversation_candidates(candidates) -> list[MemoryFormationCandidate]:
    try:
        validated = ConversationFormationResponse.model_validate({"candidates": candidates})
    except ValidationError as exc:
        raise FormationModelInvalidResponse(
            "Conversation formation candidates do not match the strict response schema"
        ) from exc
    return [
        MemoryFormationCandidate.model_validate(candidate.model_dump(mode="python"))
        for candidate in validated.candidates
    ]


class FakeConversationFormationModel:
    def __init__(
        self,
        response: ConversationFormationResponse | dict | None = None,
        *,
        error: Exception | None = None,
        usage: dict[str, int | float] | None = None,
    ) -> None:
        self.response = response if response is not None else ConversationFormationResponse()
        self.error = error
        self.last_usage = _numeric_usage(usage)
        self.calls: list[dict] = []

    async def form(
        self,
        *,
        turns: Sequence[MemoryFormationTurn],
        existing_memories: Sequence[MemoryItem] = (),
    ) -> list[MemoryFormationCandidate]:
        self.calls.append(
            {
                "turns": [turn.model_copy(deep=True) for turn in turns],
                "existing_memories": [item.model_copy(deep=True) for item in existing_memories],
            }
        )
        if self.error is not None:
            raise self.error
        try:
            validated = ConversationFormationResponse.model_validate(self.response)
        except ValidationError as exc:
            raise FormationModelInvalidResponse(
                "Fake formation response does not match the strict schema"
            ) from exc
        return [
            MemoryFormationCandidate.model_validate(candidate.model_dump(mode="python"))
            for candidate in validated.candidates
        ]


class OpenAICompatibleConversationFormationModel:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.prompt = MemoryFormationPrompt(
            version=settings.memory_formation_prompt_version,
            max_chars=settings.memory_formation_prompt_max_chars,
        )
        self._last_usage: ContextVar[dict[str, int | float] | None] = ContextVar(
            "formation_model_usage", default=None
        )

    @property
    def last_usage(self) -> dict[str, int | float]:
        return dict(self._last_usage.get() or {})

    async def form(
        self,
        *,
        turns: Sequence[MemoryFormationTurn],
        existing_memories: Sequence[MemoryItem] = (),
    ) -> list[MemoryFormationCandidate]:
        base_url, api_key = self._provider_config()
        messages = self.prompt.messages(
            turns=turns,
            existing_memories=existing_memories,
            response_schema=ConversationFormationResponse.model_json_schema(),
        )
        body = {
            "model": self.settings.memory_formation_model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.memory_formation_model_timeout_seconds,
                transport=self.transport,
            ) as client:
                response_bytes = bytearray()
                async with client.stream(
                    "POST",
                    base_url.rstrip("/") + "/v1/chat/completions",
                    headers=headers,
                    json=body,
                ) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        if len(response_bytes) + len(chunk) > _MAX_FORMATION_RESPONSE_BYTES:
                            raise FormationModelInvalidResponse(
                                "Formation provider response exceeds 256 KiB"
                            )
                        response_bytes.extend(chunk)
        except httpx.TimeoutException as exc:
            raise FormationModelTimeout("Formation model request timed out") from exc
        except httpx.HTTPError as exc:
            raise FormationModelProviderError("Formation model request failed") from exc

        try:
            envelope = json.loads(response_bytes)
            self._last_usage.set(_numeric_usage(envelope.get("usage")))
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content must be a string")
            parsed = json.loads(content)
            validated = ConversationFormationResponse.model_validate(parsed)
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            raise FormationModelInvalidResponse(
                "Formation model returned invalid strict JSON"
            ) from exc
        return [
            MemoryFormationCandidate.model_validate(candidate.model_dump(mode="python"))
            for candidate in validated.candidates
        ]

    def _provider_config(self) -> tuple[str, str]:
        if not self.settings.router_llm_base_url:
            raise FormationModelProviderError(
                "ROUTER_LLM_BASE_URL is required for the formation model"
            )
        api_key = self.settings.router_llm_api_key
        if not api_key:
            raise FormationModelProviderError(
                "ROUTER_LLM_API_KEY is required for the formation model"
            )
        if api_key.strip() in _PLACEHOLDER_API_KEYS:
            raise FormationModelProviderError("ROUTER_LLM_API_KEY is still a placeholder")
        return self.settings.router_llm_base_url, api_key


def _numeric_usage(value) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:64]: item
        for key, item in list(value.items())[:50]
        if isinstance(item, int | float) and not isinstance(item, bool) and item >= 0
    }


__all__ = [
    "ConversationFormationModel",
    "ConversationFormationModelError",
    "ConversationFormationCandidate",
    "ConversationFormationResponse",
    "FakeConversationFormationModel",
    "FormationModelInvalidResponse",
    "FormationModelProviderError",
    "FormationModelTimeout",
    "OpenAICompatibleConversationFormationModel",
    "validate_conversation_candidates",
]
