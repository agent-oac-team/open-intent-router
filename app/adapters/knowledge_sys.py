from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError

from app.schemas.agent_context import KnowledgeCitation, KnowledgeContextItem
from app.schemas.knowledge_provider import (
    KnowledgeProviderRequest,
    KnowledgeProviderResult,
)

_SEARCH_PATH = "/api/v1/knowledge/search"
_READ_SCOPE = "knowledge:read"


class KnowledgeSysHttpProvider:
    """Map the provider-neutral Knowledge port to knowledge_sys HTTP search."""

    def __init__(
        self,
        *,
        base_url: str,
        signing_private_key: str,
        signing_key_id: str,
        issuer: str = "oir",
        audience: str = "knowledge_sys",
        deadline_seconds: float = 12.0,
        token_ttl_seconds: int = 60,
        circuit_window_seconds: float = 30.0,
        circuit_failure_threshold: int = 5,
        circuit_open_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        parsed_url = urlparse(normalized_url)
        if parsed_url.scheme != "https" and not (
            parsed_url.scheme == "http" and parsed_url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Knowledge Provider base URL must use HTTPS or loopback HTTP")
        if not signing_private_key.strip():
            raise ValueError("Knowledge Provider signing private key is required")
        if not signing_key_id.strip():
            raise ValueError("Knowledge Provider signing key ID is required")
        if not issuer.strip() or not audience.strip():
            raise ValueError("Knowledge Provider issuer and audience are required")
        if deadline_seconds <= 0:
            raise ValueError("Knowledge Provider deadline must be positive")
        if not 1 <= token_ttl_seconds <= 300:
            raise ValueError("Knowledge Provider token TTL must be between 1 and 300 seconds")
        if circuit_window_seconds <= 0 or circuit_open_seconds <= 0:
            raise ValueError("Knowledge Provider circuit durations must be positive")
        if circuit_failure_threshold <= 0:
            raise ValueError("Knowledge Provider circuit threshold must be positive")

        self._base_url = normalized_url
        self._signing_private_key = signing_private_key
        self._signing_key_id = signing_key_id
        self._issuer = issuer
        self._audience = audience
        self._deadline_seconds = deadline_seconds
        self._token_ttl_seconds = token_ttl_seconds
        self._circuit_window_seconds = circuit_window_seconds
        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_open_seconds = circuit_open_seconds
        self._transport = transport
        self._clock = clock
        self._wall_clock = wall_clock

        self._circuit_lock = asyncio.Lock()
        self._failure_times: deque[float] = deque()
        self._open_until: float | None = None
        self._half_open_probe_in_flight = False

    async def retrieve(self, request: KnowledgeProviderRequest) -> KnowledgeProviderResult:
        is_probe = await self._acquire_circuit_permission()
        if is_probe is None:
            return KnowledgeProviderResult(
                status="error",
                error_code="knowledge_provider_circuit_open",
            )

        identity_error = self._identity_error(request)
        if identity_error is not None:
            await self._record_non_failure(is_probe=is_probe)
            return identity_error

        request_id = _bounded_string(
            request.trace_context.get("request_id")
            or request.trace_context.get("trace_id")
            or f"oir-knowledge-{uuid4().hex}",
            fallback=f"oir-knowledge-{uuid4().hex}",
        )
        timeout_seconds = self._effective_deadline(request)
        try:
            async with asyncio.timeout(timeout_seconds):
                token = self._issue_token(request)
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    transport=self._transport,
                    timeout=httpx.Timeout(timeout_seconds),
                ) as client:
                    response = await client.post(
                        _SEARCH_PATH,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-Trace-ID": request_id,
                        },
                        json=self._search_payload(request, request_id=request_id),
                    )
        except (TimeoutError, httpx.TimeoutException):
            await self._record_counted_failure(is_probe=is_probe)
            return KnowledgeProviderResult(
                status="timeout",
                error_code="knowledge_provider_timeout",
            )
        except httpx.TransportError:
            await self._record_counted_failure(is_probe=is_probe)
            return KnowledgeProviderResult(
                status="error",
                error_code="knowledge_provider_unavailable",
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            await self._record_non_failure(is_probe=is_probe)
            return KnowledgeProviderResult(
                status="error",
                error_code="knowledge_provider_configuration_error",
            )
        except Exception:
            await self._record_counted_failure(is_probe=is_probe)
            return KnowledgeProviderResult(
                status="error",
                error_code="knowledge_provider_unavailable",
            )

        if response.status_code >= 500:
            await self._record_counted_failure(is_probe=is_probe)
            return KnowledgeProviderResult(
                status="error",
                error_code="knowledge_provider_unavailable",
            )
        if response.status_code in {401, 403}:
            await self._record_non_failure(is_probe=is_probe)
            return KnowledgeProviderResult(
                status="denied",
                error_code="knowledge_auth_denied",
            )
        if response.status_code >= 400:
            await self._record_non_failure(is_probe=is_probe)
            return KnowledgeProviderResult(
                status="error",
                error_code="knowledge_request_rejected",
            )

        try:
            result = self._map_response(response.json())
        except (TypeError, ValueError, ValidationError):
            await self._record_counted_failure(is_probe=is_probe)
            return KnowledgeProviderResult(
                status="error",
                error_code="knowledge_provider_unavailable",
            )
        await self._record_non_failure(is_probe=is_probe)
        return result

    def _identity_error(self, request: KnowledgeProviderRequest) -> KnowledgeProviderResult | None:
        if not request.principal.tenant_id:
            return KnowledgeProviderResult(
                status="denied",
                error_code="knowledge_identity_missing",
            )
        return None

    def _issue_token(self, request: KnowledgeProviderRequest) -> str:
        now = int(self._wall_clock())
        attributes = request.principal.attributes
        principal_type = _bounded_string(attributes.get("principal_type"), fallback="user")
        access_tags_value = attributes.get("knowledge_access_tags", [])
        access_tags = (
            [item for item in access_tags_value if isinstance(item, str) and item]
            if isinstance(access_tags_value, list)
            else []
        )
        return jwt.encode(
            {
                "iss": self._issuer,
                "sub": request.principal.id,
                "aud": self._audience,
                "iat": now,
                "exp": now + self._token_ttl_seconds,
                "jti": uuid4().hex,
                "tenant_id": request.principal.tenant_id,
                "principal_type": principal_type,
                "scopes": [_READ_SCOPE],
                "access_tags": access_tags,
            },
            self._signing_private_key,
            algorithm="RS256",
            headers={"kid": self._signing_key_id},
        )

    def _search_payload(
        self,
        request: KnowledgeProviderRequest,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        session_id = _bounded_string(
            request.trace_context.get("session_id"),
            fallback=request_id,
        )
        return {
            "request_id": request_id,
            "session_id": session_id,
            "user_id": request.principal.id,
            "tenant_id": request.principal.tenant_id,
            "principal_type": _bounded_string(
                request.principal.attributes.get("principal_type"),
                fallback="user",
            ),
            "user_tags": [],
            "consumer": _map_consumer(request.consumer),
            "consumer_id": _bounded_string(request.consumer, fallback="oir"),
            "purpose": _map_purpose(request.purpose),
            "query": request.query,
            "filters": {
                "source_ids": list(request.source_ids),
                "source_tags": list(request.source_tags),
            },
            "top_k": max(1, request.budget.max_items),
        }

    def _effective_deadline(self, request: KnowledgeProviderRequest) -> float:
        requested = request.budget.timeout_seconds
        return min(self._deadline_seconds, requested) if requested else self._deadline_seconds

    def _map_response(self, payload: Any) -> KnowledgeProviderResult:
        if not isinstance(payload, dict):
            raise ValueError("Knowledge Provider response must be an object")
        evidence = payload.get("evidence", [])
        warnings = payload.get("warnings", [])
        if not isinstance(evidence, list) or not isinstance(warnings, list):
            raise ValueError("Knowledge Provider response collections are invalid")

        warning_codes = [
            item["code"]
            for item in warnings
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        ]
        items = [self._map_item(item) for item in evidence]
        matched = payload.get("matched")
        if matched is True and not items:
            raise ValueError("matched response requires evidence")
        denied = not items and any(
            code in {"permission_filtered", "secret_filtered"} for code in warning_codes
        )
        status = "ok" if items else ("denied" if denied else "empty")
        trace_id = payload.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("Knowledge Provider response trace_id is required")
        truncated = any(
            code in {"result_truncated", "top_k_capped", "max_chars_capped"}
            for code in warning_codes
        )
        return KnowledgeProviderResult(
            status=status,
            items=items,
            citations=[item.citation for item in items if item.citation is not None],
            trace_id=trace_id,
            warnings=warning_codes,
            error_code="knowledge_access_denied" if denied else None,
            truncated=truncated,
        )

    @staticmethod
    def _map_item(payload: Any) -> KnowledgeContextItem:
        if not isinstance(payload, dict):
            raise ValueError("Knowledge Provider evidence item must be an object")
        citation_value = payload.get("citation")
        if not isinstance(citation_value, dict):
            raise ValueError("Knowledge Provider evidence citation is required")
        source_id = citation_value.get("source_id")
        source_ref = payload.get("source_ref")
        uri = source_ref.get("uri") if isinstance(source_ref, dict) else None
        return KnowledgeContextItem(
            item_id=payload.get("item_id"),
            source_id=source_id,
            content=payload.get("content", ""),
            score=payload.get("confidence", 0.0),
            title=payload.get("title") or None,
            uri=uri if isinstance(uri, str) and uri else None,
            updated_at=payload.get("updated_at"),
            citation=KnowledgeCitation(source_id=source_id),
            metadata={
                key: value
                for key, value in {
                    "asset_id": payload.get("asset_id"),
                    "chunk_id": payload.get("chunk_id"),
                }.items()
                if isinstance(value, str) and value
            },
        )

    async def _acquire_circuit_permission(self) -> bool | None:
        now = self._clock()
        async with self._circuit_lock:
            if self._open_until is None:
                return False
            if now < self._open_until or self._half_open_probe_in_flight:
                return None
            self._half_open_probe_in_flight = True
            return True

    async def _record_counted_failure(self, *, is_probe: bool) -> None:
        now = self._clock()
        async with self._circuit_lock:
            if is_probe:
                self._half_open_probe_in_flight = False
                self._open_until = now + self._circuit_open_seconds
                return
            cutoff = now - self._circuit_window_seconds
            while self._failure_times and self._failure_times[0] < cutoff:
                self._failure_times.popleft()
            self._failure_times.append(now)
            if len(self._failure_times) >= self._circuit_failure_threshold:
                self._open_until = now + self._circuit_open_seconds
                self._failure_times.clear()

    async def _record_non_failure(self, *, is_probe: bool) -> None:
        async with self._circuit_lock:
            self._failure_times.clear()
            if is_probe:
                self._open_until = None
                self._half_open_probe_in_flight = False


def _map_consumer(value: str) -> str:
    if value.startswith("agent"):
        return "agent"
    if value == "admin":
        return "admin"
    if value == "coze_workflow":
        return "coze_workflow"
    return "central"


def _map_purpose(value: str) -> str:
    values = {
        "answer",
        "route",
        "plan",
        "execute",
        "validate",
        "explain",
        "workflow",
    }
    if value in values:
        return value
    if "route" in value:
        return "route"
    if "plan" in value:
        return "plan"
    if "execute" in value or value == "agent_execution":
        return "execute"
    return "answer"


def _bounded_string(value: Any, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    return normalized[:128] if normalized else fallback


def load_signing_private_key(*, pem: str | None, file_path: str | None) -> str:
    if bool(pem) == bool(file_path):
        raise ValueError("Configure exactly one Knowledge Provider private key PEM or file")
    if pem:
        return pem.replace("\\n", "\n")
    try:
        return Path(str(file_path)).read_text()
    except OSError as exc:
        raise ValueError("Knowledge Provider private key file is unavailable") from exc


def public_jwks(*, signing_private_key: str, signing_key_id: str) -> dict[str, Any]:
    try:
        private_key = serialization.load_pem_private_key(
            signing_private_key.encode(),
            password=None,
        )
        public_key = private_key.public_key()
        raw_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(public_key)
        parsed = json.loads(raw_jwk)
    except (AttributeError, TypeError, ValueError, jwt.PyJWTError) as exc:
        raise ValueError("Knowledge Provider signing private key is invalid") from exc
    return {
        "keys": [
            {
                "kid": signing_key_id,
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "n": parsed["n"],
                "e": parsed["e"],
            }
        ]
    }
