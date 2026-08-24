"""Governed HTTP Runtime Adapter with request-scoped Connector capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from inspect import isawaitable
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
_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.azure.internal",
        "metadata.google.internal",
        "metadata.internal",
        "instance-data.ec2.internal",
    }
)
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

HttpTargetResolver = Callable[[str, int], Awaitable[Sequence[str]]]


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
    allowed_local_address_hosts: frozenset[str] = frozenset()
    allowed_local_address_ports: frozenset[int] = frozenset()

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
        local_hosts = frozenset(
            _normalize_allowed_host(value) for value in self.allowed_local_address_hosts
        )
        local_ports = frozenset(self.allowed_local_address_ports)
        if any(
            not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535
            for port in local_ports
        ):
            raise ValueError("HTTP egress policy local ports are invalid")
        if bool(local_hosts) != bool(local_ports):
            raise ValueError("HTTP egress policy local override is incomplete")
        if not local_hosts.issubset(hosts) or not local_ports.issubset(ports):
            raise ValueError("HTTP egress policy local override exceeds the allowlist")
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
        object.__setattr__(self, "allowed_local_address_hosts", local_hosts)
        object.__setattr__(self, "allowed_local_address_ports", local_ports)

    def allows_url(self, url: httpx.URL, *, adapter_verify_tls: bool) -> bool:
        """Check one initial or redirect target without leaking its value."""

        if url.scheme not in {"http", "https"}:
            return False
        if url.username or url.password or url.query or url.fragment:
            return False
        if url.scheme != "https" and not self.allow_plaintext_http:
            return False
        if self.verify_tls != adapter_verify_tls:
            return False
        host = _canonical_url_host(url)
        if host is None or host not in self.allowed_hosts or host in _METADATA_HOSTS:
            return False
        return _effective_port(url) in self.allowed_ports

    def allows_local_address(self, *, host: str, port: int) -> bool:
        """Local address access needs a second, narrow deployment opt-in."""

        return host in self.allowed_local_address_hosts and port in self.allowed_local_address_ports


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
class _ResolvedHttpTarget:
    """One public or explicitly local address pinned for a request-only Connector."""

    hostname: str = field(repr=False)
    port: int
    address: str = field(repr=False)
    wire_host: str


@dataclass(frozen=True, slots=True)
class _PreparedHttpRuntimeConnector:
    """Private Connector form with one pre-acceptance DNS decision attached."""

    source: HttpRuntimeConnector = field(repr=False)
    operation_targets: Mapping[str, _ResolvedHttpTarget] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_targets", MappingProxyType(dict(self.operation_targets))
        )


@dataclass(frozen=True, slots=True)
class _HttpRequestPlan:
    operation: str
    method: str
    url: httpx.URL
    policy: HttpEgressPolicy
    headers: Mapping[str, str]
    target: _ResolvedHttpTarget | None = None


def _require_pinned_target(
    request: httpx.Request,
    target_context: ContextVar[_ResolvedHttpTarget | None],
) -> _ResolvedHttpTarget:
    target = target_context.get()
    request_host = request.url.host.casefold().rstrip(".") if request.url.host else ""
    if target is None or request_host != target.wire_host:
        raise httpx.ConnectError("Governed HTTP target is unavailable", request=request)
    return target


class _PinnedNetworkBackend:
    """Connect httpcore only to the address prepared in this request Context."""

    def __init__(
        self,
        delegate: object,
        target_context: ContextVar[_ResolvedHttpTarget | None],
    ) -> None:
        self._delegate = delegate
        self._target_context = target_context

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object | None = None,
    ) -> object:
        target = self._target_context.get()
        if target is None or host.casefold().rstrip(".") != target.wire_host or port != target.port:
            raise RuntimeError("Governed HTTP target is unavailable")
        candidate = getattr(self._delegate, "connect_tcp", None)
        if not callable(candidate):
            raise RuntimeError("Governed HTTP transport is unavailable")
        result = candidate(
            target.address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        if not isawaitable(result):
            raise RuntimeError("Governed HTTP transport is unavailable")
        return await result

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object | None = None,
    ) -> object:
        candidate = getattr(self._delegate, "connect_unix_socket", None)
        if not callable(candidate):
            raise RuntimeError("Governed HTTP transport is unavailable")
        result = candidate(path, timeout=timeout, socket_options=socket_options)
        if not isawaitable(result):
            raise RuntimeError("Governed HTTP transport is unavailable")
        return await result

    async def sleep(self, seconds: float) -> None:
        candidate = getattr(self._delegate, "sleep", None)
        if not callable(candidate):
            raise RuntimeError("Governed HTTP transport is unavailable")
        result = candidate(seconds)
        if not isawaitable(result):
            raise RuntimeError("Governed HTTP transport is unavailable")
        await result


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport whose pool keeps TLS origin separate from the wire IP."""

    def __init__(
        self,
        *,
        verify_tls: bool,
        target_context: ContextVar[_ResolvedHttpTarget | None],
    ) -> None:
        super().__init__(verify=verify_tls, trust_env=False)
        pool = getattr(self, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:
            raise RuntimeError("Governed HTTP transport is unavailable")
        pool._network_backend = _PinnedNetworkBackend(backend, target_context)
        self._target_context = target_context

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _require_pinned_target(request, self._target_context)
        return await super().handle_async_request(request)


class _PinnedDelegatingTransport(httpx.AsyncBaseTransport):
    """Apply the same target fence to a deployment-provided test transport."""

    def __init__(
        self,
        delegate: httpx.AsyncBaseTransport,
        target_context: ContextVar[_ResolvedHttpTarget | None],
    ) -> None:
        self._delegate = delegate
        self._target_context = target_context

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _require_pinned_target(request, self._target_context)
        return await self._delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self._delegate.aclose()


class HttpRuntimeAdapter:
    """One Catalog-owned HTTP client that accepts only Connector-derived targets."""

    requires_connector = True

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        verify_tls: bool = True,
        target_resolver: HttpTargetResolver | None = None,
        allow_local_targets: bool = False,
    ) -> None:
        if not isinstance(verify_tls, bool):
            raise ValueError("HTTP Runtime Adapter TLS setting is invalid")
        if target_resolver is not None and not callable(target_resolver):
            raise ValueError("HTTP Runtime Adapter target resolver is invalid")
        if not isinstance(allow_local_targets, bool):
            raise ValueError("HTTP Runtime Adapter local target setting is invalid")
        self._transport = transport
        self._verify_tls = verify_tls
        self._target_resolver = target_resolver or _resolve_host_addresses
        self._allow_local_targets = allow_local_targets
        self._wire_host_key = secrets.token_bytes(32)
        self._target_context: ContextVar[_ResolvedHttpTarget | None] = ContextVar(
            "http_runtime_egress_target",
            default=None,
        )
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
            transport = self._transport
            if transport is None:
                transport = _PinnedAsyncHTTPTransport(
                    verify_tls=self._verify_tls,
                    target_context=self._target_context,
                )
            else:
                transport = _PinnedDelegatingTransport(transport, self._target_context)
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=None,
                transport=transport,
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

        # A Resolver supplies deployment-owned source capabilities, never a
        # request-specific target prepared by a previous call.  Accepting the
        # latter would make a stale or forged DNS decision an acceptance input.
        if not isinstance(connector.connection, HttpRuntimeConnector):
            return False
        return self._request_plan(binding, connector, idempotency_key=None) is not None

    async def prepare_connector(
        self,
        binding: RuntimeAdapterBinding,
        connector: ResolvedConnector,
    ) -> ResolvedConnector | None:
        """Resolve all network facts before the Invocation Runtime accepts a Run."""

        connection = connector.connection
        if isinstance(connection, _PreparedHttpRuntimeConnector):
            source = connection.source
        elif isinstance(connection, HttpRuntimeConnector):
            source = connection
        else:
            return None
        if not isinstance(source, HttpRuntimeConnector):
            return None
        # Resolve every acceptance from the source Connector.  This is also
        # defensive for direct Adapter callers, which must not reuse a target
        # prepared during an earlier DNS decision.
        source_connector = replace(connector, connection=source)
        plan = self._request_plan(binding, source_connector, idempotency_key=None)
        if plan is None:
            return None
        target = await self._resolve_target(plan.url, plan.policy)
        if target is None:
            return None
        return replace(
            connector,
            connection=_PreparedHttpRuntimeConnector(
                source=source,
                operation_targets={plan.operation: target},
            ),
        )

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
        if plan is None or plan.target is None:
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
        target: _ResolvedHttpTarget | None = None
        if isinstance(connection, _PreparedHttpRuntimeConnector):
            source = connection.source
        elif isinstance(connection, HttpRuntimeConnector):
            source = connection
        else:
            return None
        if not isinstance(source, HttpRuntimeConnector):
            return None
        operation_name = binding.config.get("operation")
        if not isinstance(operation_name, str) or not _OPERATION_PATTERN.fullmatch(operation_name):
            return None
        operation = source.operations.get(operation_name)
        if not isinstance(operation, HttpConnectorOperation):
            return None
        method = _try_normalize_method(operation.method)
        if method is None or method not in source.policy.allowed_methods:
            return None
        url = _try_parse_url(operation.url)
        if url is None or not source.policy.allows_url(
            url,
            adapter_verify_tls=self._verify_tls,
        ):
            return None
        if isinstance(connection, _PreparedHttpRuntimeConnector):
            target = connection.operation_targets.get(operation_name)
            if target is None or not self._prepared_target_is_valid(
                target,
                url=url,
                policy=source.policy,
            ):
                return None
        headers = _filtered_headers(
            source.headers,
            policy=source.policy,
            idempotency_key=idempotency_key,
            host_header=_host_header(url),
        )
        if headers is None:
            return None
        return _HttpRequestPlan(
            operation=operation_name,
            method=method,
            url=url,
            policy=source.policy,
            headers=headers,
            target=target,
        )

    def _prepared_target_is_valid(
        self,
        target: _ResolvedHttpTarget,
        *,
        url: httpx.URL,
        policy: HttpEgressPolicy,
    ) -> bool:
        """Accept only a target minted by this Adapter for this exact operation URL."""

        host = _canonical_url_host(url)
        if (
            not isinstance(target, _ResolvedHttpTarget)
            or host is None
            or not isinstance(target.hostname, str)
            or not isinstance(target.address, str)
            or not isinstance(target.wire_host, str)
            or not isinstance(target.port, int)
            or isinstance(target.port, bool)
            or target.hostname != host
            or target.port != _effective_port(url)
        ):
            return False
        try:
            expected_wire_host = _wire_host(
                target.hostname,
                target.port,
                target.address,
                key=self._wire_host_key,
            )
        except (TypeError, UnicodeEncodeError):
            return False
        if not hmac.compare_digest(target.wire_host, expected_wire_host):
            return False
        return bool(
            _validated_addresses(
                (target.address,),
                policy=policy,
                host=host,
                port=target.port,
                allow_local_targets=self._allow_local_targets,
            )
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
        target = plan.target
        if target is None:
            return _failure("unavailable", retryable=False)
        redirects = 0
        while True:
            remaining = (deadline_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return _failure("deadline_exceeded", retryable=False)
            token = self._target_context.set(target)
            try:
                async with client.stream(
                    plan.method,
                    _wire_url(url, target),
                    content=payload,
                    headers=_headers_for_url(plan.headers, url),
                    timeout=remaining,
                    follow_redirects=False,
                    extensions={"sni_hostname": target.hostname},
                ) as response:
                    if response.status_code in _REDIRECT_STATUS_CODES:
                        next_url = _redirect_target(
                            response,
                            request_url=url,
                            policy=plan.policy,
                            verify_tls=self._verify_tls,
                        )
                        if (
                            not plan.policy.allow_redirects
                            or redirects >= plan.policy.max_redirects
                            or next_url is None
                        ):
                            return _failure("remote_failure", retryable=False)
                        next_target = await self._resolve_target(next_url, plan.policy)
                        if next_target is None:
                            return _failure("remote_failure", retryable=False)
                        url = next_url
                        target = next_target
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
            except Exception:
                return _failure("remote_failure", retryable=False)
            finally:
                self._target_context.reset(token)
            if body is None:
                return _failure("invalid_response", retryable=False)
            return _parse_outcome(body)

    async def _resolve_target(
        self,
        url: httpx.URL,
        policy: HttpEgressPolicy,
    ) -> _ResolvedHttpTarget | None:
        host = _canonical_url_host(url)
        if host is None or host in _METADATA_HOSTS:
            return None
        port = _effective_port(url)
        try:
            candidate = self._target_resolver(host, port)
            if not isawaitable(candidate):
                return None
            values = await candidate
        except Exception:
            return None
        addresses = _validated_addresses(
            values,
            policy=policy,
            host=host,
            port=port,
            allow_local_targets=self._allow_local_targets,
        )
        if not addresses:
            return None
        address = addresses[0]
        return _ResolvedHttpTarget(
            hostname=host,
            port=port,
            address=address,
            wire_host=_wire_host(host, port, address, key=self._wire_host_key),
        )


def http_runtime_descriptor(
    *,
    key: str = "http",
    contract_version: str = "oir-http-runtime-v1",
    implementation_version: str = "oir-http-runtime-v1",
    transport: httpx.AsyncBaseTransport | None = None,
    verify_tls: bool = True,
    target_resolver: HttpTargetResolver | None = None,
) -> RuntimeAdapterDescriptor:
    """Publish one shared HTTP Adapter for each Runtime Catalog lifespan."""

    def factory(context: object) -> HttpRuntimeAdapter:
        settings = getattr(context, "settings", None)
        allow_local_targets = getattr(settings, "app_env", None) == "local"
        return HttpRuntimeAdapter(
            transport=transport,
            verify_tls=verify_tls,
            target_resolver=target_resolver,
            allow_local_targets=allow_local_targets,
        )

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


def _canonical_url_host(url: httpx.URL) -> str | None:
    try:
        raw_host = url.raw_host.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not raw_host:
        return None
    try:
        return _normalize_allowed_host(raw_host)
    except ValueError:
        return None


def _effective_port(url: httpx.URL) -> int:
    if url.port is not None:
        return url.port
    return 443 if url.scheme == "https" else 80


def _host_header(url: httpx.URL) -> str:
    host = _canonical_url_host(url)
    if host is None:
        raise ValueError("HTTP target host is invalid")
    default_port = 443 if url.scheme == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    port = _effective_port(url)
    if port == default_port:
        return rendered_host
    return f"{rendered_host}:{port}"


def _wire_host(host: str, port: int, address: str, *, key: bytes) -> str:
    material = f"{host}\x00{port}\x00{address}".encode("ascii")
    digest = hmac.new(key, material, hashlib.sha256).hexdigest()[:32]
    return f"oir-egress-{digest}.invalid"


def _wire_url(url: httpx.URL, target: _ResolvedHttpTarget) -> httpx.URL:
    return url.copy_with(host=target.wire_host)


async def _resolve_host_addresses(host: str, port: int) -> Sequence[str]:
    """Resolve one canonical target once; connection uses the returned IP, never DNS again."""

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return (str(literal),)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return ()
    return tuple(
        record[4][0]
        for record in records
        if isinstance(record, tuple)
        and len(record) >= 5
        and isinstance(record[4], tuple)
        and record[4]
        and isinstance(record[4][0], str)
    )


def _validated_addresses(
    values: object,
    *,
    policy: HttpEgressPolicy,
    host: str,
    port: int,
    allow_local_targets: bool,
) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence) or not values:
        return ()
    addresses: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            return ()
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            return ()
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            parsed = parsed.ipv4_mapped
        address = str(parsed)
        category = _address_category(parsed)
        if category == "public":
            pass
        elif (
            allow_local_targets
            and category in {"loopback", "private"}
            and policy.allows_local_address(
                host=host,
                port=port,
            )
        ):
            pass
        else:
            return ()
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return tuple(addresses)


def _address_category(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if address.is_unspecified:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast:
        return "multicast"
    if isinstance(address, ipaddress.IPv6Address) and address.is_site_local:
        return "site_local"
    if address.is_reserved:
        return "reserved"
    if address.is_private:
        return "private"
    if address.is_global:
        return "public"
    return "non_public"


def _filtered_headers(
    values: Mapping[str, str],
    *,
    policy: HttpEgressPolicy,
    idempotency_key: str | None,
    host_header: str,
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
    headers["host"] = host_header
    return MappingProxyType(headers)


def _headers_for_url(headers: Mapping[str, str], url: httpx.URL) -> Mapping[str, str]:
    updated = dict(headers)
    updated["host"] = _host_header(url)
    return MappingProxyType(updated)


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
    *,
    request_url: httpx.URL,
    policy: HttpEgressPolicy,
    verify_tls: bool,
) -> httpx.URL | None:
    location = response.headers.get("location")
    if not isinstance(location, str) or not location or len(location) > 2_048:
        return None
    try:
        target = request_url.join(location)
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
