from collections.abc import Mapping, Sequence
from typing import Any

from app.core.config import LLMApiStyle


def structured_output_request(
    *,
    base_url: str,
    api_style: LLMApiStyle,
    model: str,
    messages: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    normalized_base_url = base_url.rstrip("/")
    if api_style == "chat_completions":
        suffix = (
            "/chat/completions" if normalized_base_url.endswith("/v1") else "/v1/chat/completions"
        )
        return normalized_base_url + suffix, {
            "model": model,
            "messages": [dict(message) for message in messages],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

    instructions: list[str] = []
    response_input: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            instructions.append(str(message["content"]))
        else:
            response_input.append(dict(message))

    body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "text": {"format": {"type": "json_object"}},
    }
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    if response_input:
        body["input"] = response_input
    return normalized_base_url + "/responses", body


def structured_output_text(envelope: object, *, api_style: LLMApiStyle) -> str:
    if not isinstance(envelope, Mapping):
        raise ValueError("invalid response envelope")
    if api_style == "chat_completions":
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("missing choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ValueError("invalid choice")
        message = first.get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
            raise ValueError("missing message content")
        return str(message["content"])

    # output_text is an SDK convenience field, while raw Responses payloads expose
    # message content in output items. Support both without assuming item order.
    if isinstance(envelope.get("output_text"), str):
        return str(envelope["output_text"])
    text_parts: list[str] = []
    output = envelope.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, Mapping)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    text_parts.append(str(part["text"]))
    if not text_parts:
        raise ValueError("missing response output text")
    return "\n".join(text_parts)
