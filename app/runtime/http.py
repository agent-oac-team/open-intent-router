"""Governed HTTP Runtime Adapter with request-scoped Connector capabilities."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from types import MappingProxyType

import httpx
from pydantic import ValidationError

from app.application import ResolvedConnector
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
)
from app.runtime.invocation import (
    AgentCallEnvelope,
    InvocationFailureCategory,
    RawInvocationFailure,
    RawInvocationOutcome,
    RuntimeAdapterBinding,
)

_OPERATION_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_FORBIDDEN_CONNECTOR_HEADERS = _HOP_BY_HOP_HEADERS | {"content-length", "host"}
_STATIC_HEADERS = MappingProxyType(
    {
        "accept": "application/json",
        "content-type": "application/json",
    }
)


class _NoCookiePolicy(DefaultCookiePolicy):
    """Keep the shared connection pool free of remote session state."""

    def set_ok(self, cookie: object, request: object) -> bool:
        return False

    def return_ok(self, cookie: object, request: object) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class HttpEgressPolicy:
    """Deployment-owned limits for one resolved HTTP Connector.

    The Definition cannot add a host, port, method, header, TLS override, or
    redirect behavior.  Each Connector carries the particular allowlist that
    its deployment is willing to grant to the selected HTTP Adapter.
    """

    allowed_hosts: frozenset[str]
    allowed_ports: frozenset[int] = frozenset({443})
    allowed_methods: frozenset[str] = frozenset({"POST"})
    allowed_header_names: frozenset[str] = frozenset()
    max_request_bytes: int = 32_768
    max_response_bytes: int = 32_768
    allow_plaintext_http: bool = False
    verify_tls: bool = True
    allow_redirects: bool = False
    max_redirects: int = 0

    def __post_init__(self) -> None:
        hosts = frozenset(_normalize_allowed_host(value) for value in self.allowed_hosts)
        if not hosts:
            raise ValueError("HTTP egress policy requires an allowed host")
        ports = frozenset(self.allowed_ports)
        if not ports or any(
            not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535
            for port in ports
        ):
            raise ValueError("HTTP egress policy ports are invalid")
        methods = frozenset(_normalize_method(value) for value in self.allowed_methods)
        if not methods:
            raise ValueError("HTTP egress policy requires an allowed method")
        headers = frozenset(_normalize_header_name(value) for value in self.allowed_header_names)
        if headers & _FORBIDDEN_CONNECTOR_HEADERS:
            raise ValueError("HTTP egress policy permits a forbidden header")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (self.max_request_bytes, self.max_response_bytes)
        ):
            raise ValueError("HTTP egress policy size limit is invalid")
        if not isinstance(self.allow_plaintext_http, bool) or not isinstance(self.verify_tls, bool):
            raise ValueError("HTTP egress policy transport setting is invalid")
        if not isinstance(self.allow_redirects, bool) or not isinstance(self.max_redirects, int):
            raise ValueError("HTTP egress policy redirect setting is invalid")
        if self.max_redirects < 0 or (self.allow_redirects and self.max_redirects < 1):
            raise ValueError("HTTP egress policy redirect limit is invalid")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_ports", ports)
        object.__setattr__(self, "allowed_methods", methods)
        object.__setattr__(self, "allowed_header_names", headers)

    def allows_url(self, url: httpx.URL, *, adapter_verify_tls: bool) -> bool:
        """Check one initial or redirect target without leaking its value."""

        if url.scheme not in {"http", "https"} or not url.host:
            return False
        if url.username or url.password or url.query or url.fragment:
            return False
        if url.scheme != "https" and not self.allow_plaintext_http:
            return False
        if self.verify_tls != adapter_verify_tls:
            return False
        host = url.host.casefold().rstrip(".")
        if host not in self.allowed_hosts:
            return False
        port = url.port or (443 if url.scheme == "https" else 80)
        return port in self.allowed_ports


@dataclass(frozen=True, slots=True)
class HttpConnectorOperation:
    """One deployment-selected endpoint and method, private to a Connector."""

    url: str = field(repr=False)
    method: str


@dataclass(frozen=True, slots=True)
class HttpRuntimeConnector:
    """Private HTTP capability carried in ``ResolvedConnector.connection``."""

    operations: Mapping[str, HttpConnectorOperation] = field(repr=False)
    policy: HttpEgressPolicy = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", MappingProxyType(dict(self.operations)))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class _HttpRequestPlan:
    method: str
    url: httpx.URL
    policy: HttpEgressPolicy
    headers: Mapping[str, str]


class HttpRuntimeAdapter:
    """One Catalog-owned HTTP client that accepts only Connector-derived targets."""

    requires_connector = True

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        verify_tls: bool = True,
    ) -> None:
        if not isinstance(verify_tls, bool):
            raise ValueError("HTTP Runtime Adapter TLS setting is invalid")
        self._transport = transport
        self._verify_tls = verify_tls
        self._client: httpx.AsyncClient | None = None
        self._active = False
        self._closed = False

    @property
    def client(self) -> httpx.AsyncClient | None:
        """Expose lifecycle state for controlled health and conformance checks."""

        return self._client

    @property
    def verify_tls(self) -> bool:
        return self._verify_tls

    async def activate(self) -> None:
        if self._closed:
            raise RuntimeError("HTTP Runtime Adapter cannot be restarted")
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=None,
                transport=self._transport,
                trust_env=False,
                verify=self._verify_tls,
                cookies=CookieJar(policy=_NoCookiePolicy()),
            )
        self._active = True

    async def health_check(self) -> bool:
        client = self._client
        return self._active and client is not None and not client.is_closed

    async def aclose(self) -> None:
        self._active = False
        self._closed = True
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def validate_connector(
        self,
        binding: RuntimeAdapterBinding,
        connector: ResolvedConnector,
    ) -> bool:
        """Prove local Connector policy before InvocationService accepts a Run."""

        return self._request_plan(binding, connector, idempotency_key=None) is not None

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        connector: ResolvedConnector | None,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        client = self._client
        if not self._active or client is None or client.is_closed:
            return _failure("unavailable", retryable=True)
        if connector is None:
            return _failure("unavailable", retryable=False)
        plan = self._request_plan(
            binding,
            connector,
            idempotency_key=envelope.idempotency_key,
        )
        if plan is None:
            return _failure("unavailable", retryable=False)
        try:
            payload = _serialize_envelope(envelope, limit=plan.policy.max_request_bytes)
        except ValueError:
            return _failure("rejected", retryable=False)
        return await self._send(
            client=client,
            plan=plan,
            payload=payload,
            deadline_at=envelope.deadline_at,
        )

    def _request_plan(
        self,
        binding: RuntimeAdapterBinding,
        connector: ResolvedConnector,
        *,
        idempotency_key: str | None,
    ) -> _HttpRequestPlan | None:
        connection = connector.connection
        if not isinstance(connection, HttpRuntimeConnector):
            return None
        operation_name = binding.config.get("operation")
        if not isinstance(operation_name, str) or not _OPERATION_PATTERN.fullmatch(operation_name):
            return None
        operation = connection.operations.get(operation_name)
        if not isinstance(operation, HttpConnectorOperation):
            return None
        method = _try_normalize_method(operation.method)
        if method is None or method not in connection.policy.allowed_methods:
            return None
        url = _try_parse_url(operation.url)
        if url is None or not connection.policy.allows_url(
            url,
            adapter_verify_tls=self._verify_tls,
        ):
            return None
        headers = _filtered_headers(
            connection.headers,
            policy=connection.policy,
            idempotency_key=idempotency_key,
        )
        if headers is None:
            return None
        return _HttpRequestPlan(
            method=method,
            url=url,
            policy=connection.policy,
            headers=headers,
        )

    async def _send(
        self,
        *,
        client: httpx.AsyncClient,
        plan: _HttpRequestPlan,
        payload: bytes,
        deadline_at: datetime,
    ) -> RawInvocationOutcome:
        url = plan.url
        redirects = 0
        while True:
            remaining = (deadline_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return _failure("deadline_exceeded", retryable=False)
            try:
                async with client.stream(
                    plan.method,
                    url,
                    content=payload,
                    headers=plan.headers,
                    timeout=remaining,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECT_STATUS_CODES:
                        next_url = _redirect_target(response, plan.policy, self._verify_tls)
                        if (
                            not plan.policy.allow_redirects
                            or redirects >= plan.policy.max_redirects
                            or next_url is None
                        ):
                            return _failure("remote_failure", retryable=False)
                        url = next_url
                        redirects += 1
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        return _failure("remote_failure", retryable=False)
                    if not _is_json_response(response):
                        return _failure("invalid_response", retryable=False)
                    body = await _read_response(response, limit=plan.policy.max_response_bytes)
            except (TimeoutError, httpx.TimeoutException):
                return _failure("deadline_exceeded", retryable=False)
            except httpx.HTTPError:
                return _failure("remote_failure", retryable=False)
            if body is None:
                return _failure("invalid_response", retryable=False)
            return _parse_outcome(body)


def http_runtime_descriptor(
    *,
    key: str = "http",
    contract_version: str = "oir-http-runtime-v1",
    implementation_version: str = "oir-http-runtime-v1",
    transport: httpx.AsyncBaseTransport | None = None,
    verify_tls: bool = True,
) -> RuntimeAdapterDescriptor:
    """Publish one shared HTTP Adapter for each Runtime Catalog lifespan."""

    def factory(_context: object) -> HttpRuntimeAdapter:
        return HttpRuntimeAdapter(transport=transport, verify_tls=verify_tls)

    async def activate(candidate: object) -> None:
        if not isinstance(candidate, HttpRuntimeAdapter):
            raise TypeError("HTTP Runtime Adapter factory returned an invalid Adapter")
        await candidate.activate()

    async def health(candidate: object) -> bool:
        return isinstance(candidate, HttpRuntimeAdapter) and await candidate.health_check()

    async def dispose(candidate: object) -> None:
        if isinstance(candidate, HttpRuntimeAdapter):
            await candidate.aclose()

    return RuntimeAdapterDescriptor(
        key=key,
        contract_version=contract_version,
        implementation_version=implementation_version,
        config_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "pattern": _OPERATION_PATTERN.pattern,
                }
            },
            "additionalProperties": False,
        },
        capability=RuntimeAdapterCapability(
            invocation=True,
            v2_invocation=True,
            invocation_runtime=True,
        ),
        factory=factory,
        health_check=health,
        lifecycle=RuntimeAdapterLifecycle(activate=activate, dispose=dispose),
    )


def _normalize_allowed_host(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("HTTP egress policy host is invalid")
    host = value.casefold().rstrip(".")
    if not host or not _HOST_PATTERN.fullmatch(host):
        raise ValueError("HTTP egress policy host is invalid")
    return host


def _normalize_method(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("HTTP egress policy method is invalid")
    method = value.upper()
    if not re.fullmatch(r"[A-Z]{1,16}", method):
        raise ValueError("HTTP egress policy method is invalid")
    return method


def _try_normalize_method(value: object) -> str | None:
    try:
        return _normalize_method(value)
    except ValueError:
        return None


def _normalize_header_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("HTTP egress policy header is invalid")
    name = value.casefold()
    if not _HEADER_NAME_PATTERN.fullmatch(name):
        raise ValueError("HTTP egress policy header is invalid")
    return name


def _try_parse_url(value: object) -> httpx.URL | None:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        return None
    try:
        return httpx.URL(value)
    except (TypeError, ValueError):
        return None


def _filtered_headers(
    values: Mapping[str, str],
    *,
    policy: HttpEgressPolicy,
    idempotency_key: str | None,
) -> Mapping[str, str] | None:
    headers = dict(_STATIC_HEADERS)
    for name, value in values.items():
        try:
            normalized_name = _normalize_header_name(name)
        except ValueError:
            return None
        if not isinstance(value, str) or len(value) > 4_096 or "\r" in value or "\n" in value:
            return None
        if (
            normalized_name in _FORBIDDEN_CONNECTOR_HEADERS
            or normalized_name not in policy.allowed_header_names
            or normalized_name in _STATIC_HEADERS
        ):
            continue
        headers[normalized_name] = value
    if (
        isinstance(idempotency_key, str)
        and 0 < len(idempotency_key) <= 256
        and "\r" not in idempotency_key
        and "\n" not in idempotency_key
        and "idempotency-key" in policy.allowed_header_names
    ):
        headers["idempotency-key"] = idempotency_key
    return MappingProxyType(headers)


def _serialize_envelope(envelope: AgentCallEnvelope, *, limit: int) -> bytes:
    try:
        payload = json.dumps(
            envelope.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError):
        raise ValueError("HTTP envelope cannot be serialized") from None
    if len(payload) > limit:
        raise ValueError("HTTP envelope exceeds egress limit")
    return payload


def _redirect_target(
    response: httpx.Response,
    policy: HttpEgressPolicy,
    verify_tls: bool,
) -> httpx.URL | None:
    location = response.headers.get("location")
    if not isinstance(location, str) or not location or len(location) > 2_048:
        return None
    try:
        target = response.url.join(location)
    except ValueError:
        return None
    if not policy.allows_url(target, adapter_verify_tls=verify_tls):
        return None
    return target


def _is_json_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type")
    if not isinstance(content_type, str):
        return False
    return content_type.split(";", maxsplit=1)[0].strip().casefold() == "application/json"


async def _read_response(response: httpx.Response, *, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes(chunk_size=min(limit + 1, 8_192)):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_outcome(body: bytes) -> RawInvocationOutcome:
    try:
        value = json.loads(body)
        return RawInvocationOutcome.model_validate(value)
    except (TypeError, ValueError, ValidationError):
        return _failure("invalid_response", retryable=False)


def _failure(category: InvocationFailureCategory, *, retryable: bool) -> RawInvocationOutcome:
    return RawInvocationOutcome(
        failure=RawInvocationFailure.for_category(category, retryable=retryable)
    )
