import time

import httpx

from app.core.config import Settings
from app.core.errors import InvocationError
from app.core.redaction import redact_value
from app.schemas.agents import AgentDefinition
from app.schemas.common import ErrorDetail
from app.schemas.invocation import AgentInvocation, AgentInvocationResult


class HttpAgentInvoker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def invoke(
        self,
        definition: AgentDefinition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        config = definition.invocation.config
        method = str(config.get("method", "POST")).upper()
        url = config.get("url")
        if not url:
            raise InvocationError("HTTP Agent requires invocation.config.url")
        headers = config.get("headers") or {}
        timeout = float(config.get("timeout_seconds") or self.settings.agent_http_timeout_seconds)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=invocation.model_dump(),
                )
                response.raise_for_status()
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
