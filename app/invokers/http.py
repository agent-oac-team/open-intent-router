import time

import httpx

from app.core.config import Settings
from app.core.errors import InvocationError
from app.core.redaction import redact_value
from app.schemas.agents import LegacyAgentDefinition
from app.schemas.common import ErrorDetail
from app.schemas.invocation import AgentInvocation, AgentInvocationResult


class HttpAgentInvoker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()

    async def health(self) -> bool:
        return self._client is not None and not self._client.is_closed

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def invoke(
        self,
        definition: LegacyAgentDefinition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        config = definition.invocation.config
        method = str(config.get("method", "POST")).upper()
        url = config.get("url")
        if not url:
            raise InvocationError("HTTP Agent requires invocation.config.url")
        headers = config.get("headers") or {}
        execution_key = invocation.context.get("plan_execution_idempotency_key")
        if isinstance(execution_key, str) and execution_key:
            headers = {**headers, "Idempotency-Key": execution_key}
        timeout = float(config.get("timeout_seconds") or self.settings.agent_http_timeout_seconds)
        started = time.perf_counter()
        try:
            response = await self._request(
                method=method,
                url=url,
                headers=headers,
                payload=invocation.model_dump(),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            return AgentInvocationResult(
                run_id=invocation.run_id,
                agent_id=definition.agent_id,
                status="failed",
                message="HTTP Agent invocation failed.",
                usage={"latency_ms": int((time.perf_counter() - started) * 1000)},
                error=ErrorDetail(
                    code="http_invocation_failed",
                    message=str(exc),
                    details={
                        "request": redact_value({"method": method, "url": url, "headers": headers})
                    },
                ),
            )

        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text}
        status = data.get("status", "completed") if isinstance(data, dict) else "completed"
        output = data.get("output", data) if isinstance(data, dict) else {"result": data}
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status=status,
            message=data.get("message", "") if isinstance(data, dict) else "",
            output=output,
            usage={"latency_ms": int((time.perf_counter() - started) * 1000)},
        )

    async def _request(
        self,
        *,
        method: str,
        url,
        headers,
        payload: dict,
        timeout: float,
    ) -> httpx.Response:
        client = self._client
        if client is not None:
            response = await client.request(
                method,
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return response
        async with httpx.AsyncClient(timeout=timeout) as temporary_client:
            response = await temporary_client.request(
                method,
                url,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response
