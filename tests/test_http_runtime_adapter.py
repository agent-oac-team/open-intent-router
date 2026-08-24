"""HTTP Runtime Adapter uses only governed Connector capabilities."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.application import ConnectorResolutionRequest, ResolvedConnector
from app.core.config import Settings
from app.core.errors import InvocationBindingUnavailableError
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.runtime.catalog import RuntimeAdapterContext, RuntimeCatalog
from app.runtime.http import (
    HttpConnectorOperation,
    HttpEgressPolicy,
    HttpRuntimeAdapter,
    HttpRuntimeConnector,
    http_runtime_descriptor,
)
from app.runtime.invocation import (
    AgentCallEnvelope,
    InvocationPrincipalProjection,
    RuntimeAdapterBinding,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.invocation import InvokeRequest
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime


@dataclass
class _ConnectorResolver:
    connector: ResolvedConnector | None
    requests: list[ConnectorResolutionRequest] = field(default_factory=list)
    released: list[ResolvedConnector] = field(default_factory=list)

    async def resolve(self, request: ConnectorResolutionRequest) -> ResolvedConnector | None:
        self.requests.append(request)
        return self.connector

    async def release(self, connector: ResolvedConnector) -> None:
        self.released.append(connector)


def _policy(
    *,
    hosts: frozenset[str] = frozenset({"api.example.test"}),
    ports: frozenset[int] = frozenset({443}),
    allowed_headers: frozenset[str] = frozenset({"authorization", "idempotency-key"}),
    max_request_bytes: int = 16_384,
    max_response_bytes: int = 16_384,
    allow_redirects: bool = False,
    max_redirects: int = 0,
    verify_tls: bool = True,
) -> HttpEgressPolicy:
    return HttpEgressPolicy(
        allowed_hosts=hosts,
        allowed_ports=ports,
        allowed_header_names=allowed_headers,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
        allow_redirects=allow_redirects,
        max_redirects=max_redirects,
        verify_tls=verify_tls,
    )


def _connector(
    *,
    url: str = "https://api.example.test/execute",
    method: str = "POST",
    policy: HttpEgressPolicy | None = None,
    headers: dict[str, str] | None = None,
) -> ResolvedConnector:
    return ResolvedConnector(
        tenant_id="tenant-1",
        adapter_key="http",
        connector_ref="tenant-http",
        revision="http-r7",
        connection=HttpRuntimeConnector(
            operations={"summarize": HttpConnectorOperation(url=url, method=method)},
            headers=headers or {"Authorization": "Bearer connector-test-secret"},
            policy=policy or _policy(),
        ),
    )


def _definition(*, connector_ref: str | None = "tenant-http") -> AgentDefinitionV2:
    handling: dict[str, object] = {
        "kind": "invocation",
        "adapter_key": "http",
        "config": {"operation": "summarize"},
    }
    if connector_ref is not None:
        handling["connector_ref"] = connector_ref
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "http-agent",
            "name": "HTTP Agent",
            "description": "Executes a governed HTTP operation.",
            "revision": 4,
            "access_policy": {"allow_roles": ["operator"]},
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
            "handling": handling,
        }
    )


def _request(*, text: str = "approved text") -> InvokeRequest:
    return InvokeRequest.model_validate(
        {
            "request_id": "http-runtime-request",
            "session_id": "http-runtime-session",
            "agent_id": "http-agent",
            "user": {
                "id": "operator-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1", "token": "must-not-send"},
            },
            "input": {"text": text},
        }
    )


def _envelope(*, execution_id: str) -> AgentCallEnvelope:
    return AgentCallEnvelope(
        execution_id=execution_id,
        session_id="http-runtime-session",
        principal=InvocationPrincipalProjection(subject="operator-1"),
        input={"text": "approved text"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=5),
    )


async def _service(
    *,
    resolver: _ConnectorResolver,
    transport: httpx.AsyncBaseTransport,
    verify_tls: bool = True,
    definition: AgentDefinitionV2 | None = None,
) -> tuple[
    RuntimeCatalog,
    HttpRuntimeAdapter,
    InvocationService,
    MemoryRunRepository,
    MemoryResultRepository,
]:
    catalog = await RuntimeCatalog.activate(
        [http_runtime_descriptor(transport=transport, verify_tls=verify_tls)],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    adapter = catalog.get("http")
    assert isinstance(adapter, HttpRuntimeAdapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([definition or _definition()], source="http-runtime-test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = InvocationService(
        registry=object(),
        run_repository=runs,
        result_repository=results,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog, connector_resolver=resolver),
    )
    return catalog, adapter, service, runs, results


async def test_http_runtime_uses_connector_separately_and_reuses_one_lifespan_client() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"output": {"summary": "completed"}})

    connector = _connector()
    resolver = _ConnectorResolver(connector)
    catalog, adapter, service, runs, results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        first_client = adapter.client
        assert first_client is not None and first_client.is_closed is False

        first = await service.invoke(_request())
        second = await service.invoke(_request(text="another approved text"))

        assert first.status == second.status == "completed"
        assert len(captured) == 2
        assert adapter.client is first_client
        assert captured[0].method == "POST"
        assert str(captured[0].url) == "https://api.example.test/execute"
        assert captured[0].headers["authorization"] == "Bearer connector-test-secret"
        payload = json.loads(captured[0].content)
        assert payload["input"] == {"text": "approved text"}
        assert payload["principal"] == {"subject": "operator-1", "tenant_id": "tenant-1"}
        sent = captured[0].content.decode()
        assert "connector-test-secret" not in sent
        assert "must-not-send" not in sent
        assert "definition" not in payload
        assert "memory_context" not in payload
        assert "knowledge_context" not in payload
        assert len(runs.runs) == len(results.results) == 2
        persisted = "\n".join(
            [
                next(iter(runs.runs.values())).model_dump_json(),
                results.results[0].model_dump_json(),
            ]
        )
        assert "connector-test-secret" not in persisted
        assert "api.example.test/execute" not in persisted
        assert resolver.released == [connector, connector]
    finally:
        await catalog.aclose()

    assert first_client.is_closed is True


async def test_http_runtime_does_not_retain_or_replay_remote_cookies_across_connectors() -> None:
    received_cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_cookies.append(request.headers.get("cookie"))
        if len(received_cookies) == 1:
            return httpx.Response(
                200,
                headers={"set-cookie": "tenant_a_cookie=secret-a; Path=/"},
                json={"output": {"summary": "first"}},
            )
        return httpx.Response(200, json={"output": {"summary": "second"}})

    catalog = await RuntimeCatalog.activate(
        [http_runtime_descriptor(transport=httpx.MockTransport(handler))],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    adapter = catalog.get("http")
    assert isinstance(adapter, HttpRuntimeAdapter)
    first_client = adapter.client
    assert first_client is not None
    binding = RuntimeAdapterBinding(adapter_key="http", config={"operation": "summarize"})
    connector_a = _connector(headers={"Authorization": "Bearer tenant-a"})
    connector_b = _connector(headers={"Authorization": "Bearer tenant-b"})
    try:
        first = await adapter.execute(binding, connector_a, _envelope(execution_id="tenant-a"))
        second = await adapter.execute(binding, connector_b, _envelope(execution_id="tenant-b"))

        assert first.failure is None
        assert second.failure is None
        assert received_cookies == [None, None]
        assert len(first_client.cookies) == 0
    finally:
        await catalog.aclose()


@pytest.mark.parametrize(
    "connector",
    [
        _connector(url="http://api.example.test/execute"),
        _connector(url="https://not-allowed.example.test/execute"),
        _connector(url="https://api.example.test:8443/execute"),
        _connector(method="GET"),
        _connector(policy=_policy(verify_tls=False)),
    ],
)
async def test_http_connector_policy_rejects_before_run_or_network_call(
    connector: ResolvedConnector,
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"output": {"summary": "unexpected"}})

    resolver = _ConnectorResolver(connector)
    catalog, _adapter, service, runs, results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(InvocationBindingUnavailableError) as exc_info:
            await service.invoke(_request())

        assert exc_info.value.status_code == 503
        assert exc_info.value.details == {"reason_code": "connector_unavailable"}
        assert requests == 0
        assert runs.runs == {}
        assert results.results == []
        assert resolver.released == [connector]
    finally:
        await catalog.aclose()


async def test_http_runtime_requires_a_connector_before_run_or_network_call() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"output": {"summary": "unexpected"}})

    connector = _connector()
    resolver = _ConnectorResolver(connector)
    catalog, _adapter, service, runs, results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
        definition=_definition(connector_ref=None),
    )
    try:
        with pytest.raises(InvocationBindingUnavailableError) as exc_info:
            await service.invoke(_request())

        assert exc_info.value.status_code == 503
        assert exc_info.value.details == {"reason_code": "connector_unavailable"}
        assert resolver.requests == []
        assert resolver.released == []
        assert requests == 0
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_http_runtime_only_sends_connector_headers_explicitly_allowed_by_policy() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"output": {"summary": "completed"}})

    connector = _connector(
        headers={
            "Authorization": "Bearer allowed-secret",
            "Host": "attacker.example.test",
            "Connection": "close",
            "X-Caller-Header": "must-not-send",
            "X-Unauthorized-Credential": "must-not-send",
        }
    )
    resolver = _ConnectorResolver(connector)
    catalog, _adapter, service, _runs, _results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await service.invoke(_request())

        assert result.status == "completed"
        headers = captured[0].headers
        assert headers["authorization"] == "Bearer allowed-secret"
        assert headers["host"] == "api.example.test"
        assert headers["connection"] == "keep-alive"
        assert "x-caller-header" not in headers
        assert "x-unauthorized-credential" not in headers
    finally:
        await catalog.aclose()


async def test_http_runtime_allows_an_explicit_deployment_tls_override_only_when_matched() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"summary": "completed"}})

    connector = _connector(policy=_policy(verify_tls=False))
    resolver = _ConnectorResolver(connector)
    catalog, adapter, service, _runs, _results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
        verify_tls=False,
    )
    try:
        result = await service.invoke(_request())

        assert result.status == "completed"
        assert adapter.verify_tls is False
    finally:
        await catalog.aclose()


async def test_http_runtime_rejects_oversized_request_without_dispatch() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"output": {"summary": "unexpected"}})

    connector = _connector(policy=_policy(max_request_bytes=32))
    resolver = _ConnectorResolver(connector)
    catalog, _adapter, service, runs, results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await service.invoke(_request(text="x" * 256))

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "invocation_rejected"
        assert requests == 0
        assert len(runs.runs) == len(results.results) == 1
    finally:
        await catalog.aclose()


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_http_runtime_stops_streaming_when_response_exceeds_hard_limit() -> None:
    stream = _CountingStream([b'{"output":{"summary":"', b"x" * 128, b'"}}'])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    connector = _connector(policy=_policy(max_response_bytes=64))
    resolver = _ConnectorResolver(connector)
    catalog, _adapter, service, runs, results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await service.invoke(_request())

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "invocation_invalid_response"
        assert stream.read_count == 2
        assert stream.closed is True
        assert len(runs.runs) == len(results.results) == 1
    finally:
        await catalog.aclose()


async def test_http_runtime_rejects_redirects_by_default_and_revalidates_allowed_hops() -> None:
    default_requests: list[httpx.Request] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        default_requests.append(request)
        return httpx.Response(307, headers={"location": "https://redirect.example.test/execute"})

    default_connector = _connector()
    default_catalog, _adapter, default_service, _runs, _results = await _service(
        resolver=_ConnectorResolver(default_connector),
        transport=httpx.MockTransport(default_handler),
    )
    try:
        rejected = await default_service.invoke(_request())
        assert rejected.status == "failed"
        assert rejected.error is not None
        assert rejected.error.code == "invocation_remote_failure"
        assert [str(request.url) for request in default_requests] == [
            "https://api.example.test/execute"
        ]
    finally:
        await default_catalog.aclose()

    redirected_requests: list[httpx.Request] = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        redirected_requests.append(request)
        if request.url.host == "api.example.test":
            return httpx.Response(
                307, headers={"location": "https://redirect.example.test/execute"}
            )
        return httpx.Response(200, json={"output": {"summary": "redirected"}})

    redirect_connector = _connector(
        policy=_policy(
            hosts=frozenset({"api.example.test", "redirect.example.test"}),
            allow_redirects=True,
            max_redirects=1,
        )
    )
    redirect_catalog, _adapter, redirect_service, _runs, _results = await _service(
        resolver=_ConnectorResolver(redirect_connector),
        transport=httpx.MockTransport(redirect_handler),
    )
    try:
        completed = await redirect_service.invoke(_request())
        assert completed.status == "completed"
        assert [str(request.url) for request in redirected_requests] == [
            "https://api.example.test/execute",
            "https://redirect.example.test/execute",
        ]
    finally:
        await redirect_catalog.aclose()

    blocked_redirect_requests: list[httpx.Request] = []

    def blocked_redirect_handler(request: httpx.Request) -> httpx.Response:
        blocked_redirect_requests.append(request)
        return httpx.Response(307, headers={"location": "https://not-allowed.example.test/execute"})

    blocked_connector = _connector(
        policy=_policy(allow_redirects=True, max_redirects=1),
    )
    blocked_catalog, _adapter, blocked_service, _runs, _results = await _service(
        resolver=_ConnectorResolver(blocked_connector),
        transport=httpx.MockTransport(blocked_redirect_handler),
    )
    try:
        blocked = await blocked_service.invoke(_request())
        assert blocked.status == "failed"
        assert blocked.error is not None
        assert blocked.error.code == "invocation_remote_failure"
        assert [str(request.url) for request in blocked_redirect_requests] == [
            "https://api.example.test/execute"
        ]
    finally:
        await blocked_catalog.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, content=b"remote-secret-body"),
        httpx.Response(200, content=b"not-json remote-secret-body"),
        httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content=b'{"output":{"summary":"remote-secret-body"}}',
        ),
        httpx.Response(200, json={"output": {"summary": 7}}),
    ],
)
async def test_http_runtime_projects_protocol_failures_without_remote_body(
    response: httpx.Response,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    connector = _connector()
    resolver = _ConnectorResolver(connector)
    catalog, _adapter, service, runs, results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await service.invoke(_request())

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code in {"invocation_remote_failure", "invocation_invalid_response"}
        persisted = "\n".join(
            [
                result.model_dump_json(),
                next(iter(runs.runs.values())).model_dump_json(),
                results.results[0].model_dump_json(),
            ]
        )
        assert "remote-secret-body" not in persisted
    finally:
        await catalog.aclose()


async def test_http_runtime_projects_transport_exceptions_without_exception_text() -> None:
    secret = "https://private.example.test/?authorization=connector-secret-marker"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(secret, request=request)

    connector = _connector()
    resolver = _ConnectorResolver(connector)
    catalog, _adapter, service, runs, results = await _service(
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await service.invoke(_request())

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "invocation_remote_failure"
        persisted = "\n".join(
            [
                result.model_dump_json(),
                next(iter(runs.runs.values())).model_dump_json(),
                results.results[0].model_dump_json(),
            ]
        )
        assert secret not in persisted
    finally:
        await catalog.aclose()


async def test_http_runtime_never_creates_a_temporary_client_when_not_lifecycle_active() -> None:
    adapter = HttpRuntimeAdapter()
    connector = _connector()
    outcome = await adapter.execute(
        binding=object(),  # type: ignore[arg-type]
        connector=connector,
        envelope=object(),  # type: ignore[arg-type]
    )

    assert outcome.failure is not None
    assert outcome.failure.category == "unavailable"
    assert adapter.client is None
