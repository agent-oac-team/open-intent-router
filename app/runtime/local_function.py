"""Trusted in-process Local Function Runtime Adapter.

The Registry and Adapter are deployment-owned objects.  They are activated once
by the Runtime Catalog, not assembled from an Agent Definition during a
request.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from inspect import isawaitable, iscoroutinefunction
from types import MappingProxyType
from typing import Any

from app.application import ResolvedConnector
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
)
from app.runtime.invocation import (
    AgentCallEnvelope,
    RawInvocationFailure,
    RawInvocationOutcome,
    RuntimeAdapterBinding,
)

_FUNCTION_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

LocalRuntimeFunctionResult = RawInvocationOutcome | Mapping[str, object]
LocalRuntimeFunction = Callable[
    [AgentCallEnvelope],
    LocalRuntimeFunctionResult | Awaitable[LocalRuntimeFunctionResult],
]


class LocalFunctionRuntimeRegistry:
    """Process-scoped trusted function registration, frozen at activation."""

    def __init__(self) -> None:
        self._functions: dict[str, LocalRuntimeFunction] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, name: str, function: LocalRuntimeFunction) -> None:
        if self._frozen:
            raise RuntimeError("Local Function Runtime Registry is already active")
        if not isinstance(name, str) or not _FUNCTION_NAME_PATTERN.fullmatch(name):
            raise ValueError("Local Function name must be a stable identifier")
        if not callable(function):
            raise TypeError("Local Function must be callable")
        self._functions[name] = function

    def get(self, name: str) -> LocalRuntimeFunction | None:
        return self._functions.get(name)

    def freeze(self) -> Mapping[str, LocalRuntimeFunction]:
        """Seal registrations and return a detached immutable function map.

        The returned map is intentionally copied for each Catalog factory
        invocation.  Lifecycle restarts must get a fresh Adapter and fresh
        in-flight/cache state while retaining the same trusted registrations.
        """

        self._frozen = True
        return MappingProxyType(dict(self._functions))


class LocalFunctionRuntimeAdapter:
    """One lifespan-owned Adapter with bounded non-authoritative reuse state.

    The cache is only keyed by a canonical Plan execution claim projected by
    Core.  It is an in-process optimisation for repeated delivery within one
    lifespan; Plan/Run persistence remains the source of truth across restart.
    """

    def __init__(
        self,
        functions: Mapping[str, LocalRuntimeFunction],
        *,
        completed_cache_limit: int = 1_000,
    ) -> None:
        if completed_cache_limit < 1:
            raise ValueError("Local Function completed cache limit must be positive")
        self._functions = MappingProxyType(dict(functions))
        self._completed_cache_limit = completed_cache_limit
        self._inflight: dict[str, asyncio.Task[RawInvocationOutcome]] = {}
        self._completed: dict[str, RawInvocationOutcome] = {}
        self._active = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def completed_count(self) -> int:
        return len(self._completed)

    async def activate(self) -> None:
        if self._closed:
            raise RuntimeError("Local Function Runtime Adapter cannot be restarted")
        self._active = True

    async def health_check(self) -> bool:
        return self._active and not self._closed

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._active = False
        tasks = tuple(self._inflight.values())
        self._inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._completed.clear()

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        _connector: ResolvedConnector | None,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        if not self._active or self._closed:
            return _unavailable_outcome()
        function_name = binding.config.get("function")
        if not isinstance(function_name, str) or not _FUNCTION_NAME_PATTERN.fullmatch(
            function_name
        ):
            return _unavailable_outcome()
        function = self._functions.get(function_name)
        if function is None:
            return _unavailable_outcome()
        if envelope.idempotency_key is None:
            return await self._call(function, envelope)
        return await self._invoke_idempotent(
            envelope.idempotency_key,
            function,
            envelope,
        )

    async def _invoke_idempotent(
        self,
        execution_key: str,
        function: LocalRuntimeFunction,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        completed = self._completed.get(execution_key)
        if completed is not None:
            return completed.model_copy(deep=True)
        task = self._inflight.get(execution_key)
        if task is None:
            task = asyncio.create_task(self._call(function, envelope))
            self._inflight[execution_key] = task
        try:
            outcome = await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight.pop(execution_key, None)
        self._completed[execution_key] = outcome.model_copy(deep=True)
        while len(self._completed) > self._completed_cache_limit:
            self._completed.pop(next(iter(self._completed)))
        return outcome.model_copy(deep=True)

    async def _call(
        self,
        function: LocalRuntimeFunction,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        if iscoroutinefunction(function):
            result = function(envelope)
        else:
            result = await asyncio.to_thread(function, envelope)
        if isawaitable(result):
            result = await result
        return _normalize_outcome(result)


def local_function_runtime_descriptor(
    registry: LocalFunctionRuntimeRegistry,
    *,
    key: str = "local_function",
    contract_version: str = "oir-local-function-runtime-v1",
    implementation_version: str = "oir-local-function-runtime-v1",
    completed_cache_limit: int = 1_000,
) -> RuntimeAdapterDescriptor:
    """Publish a fresh Local Function Adapter for every Catalog lifespan."""

    if completed_cache_limit < 1:
        raise ValueError("Local Function completed cache limit must be positive")

    def factory(_context: object) -> LocalFunctionRuntimeAdapter:
        return LocalFunctionRuntimeAdapter(
            registry.freeze(),
            completed_cache_limit=completed_cache_limit,
        )

    async def activate(candidate: object) -> None:
        if not isinstance(candidate, LocalFunctionRuntimeAdapter):
            raise TypeError("Local Function Runtime Adapter factory returned an invalid Adapter")
        await candidate.activate()

    async def health(candidate: object) -> bool:
        if not isinstance(candidate, LocalFunctionRuntimeAdapter):
            return False
        return await candidate.health_check()

    async def dispose(candidate: object) -> None:
        if isinstance(candidate, LocalFunctionRuntimeAdapter):
            await candidate.aclose()

    return RuntimeAdapterDescriptor(
        key=key,
        contract_version=contract_version,
        implementation_version=implementation_version,
        config_schema={
            "type": "object",
            "required": ["function"],
            "properties": {"function": {"type": "string"}},
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


def _normalize_outcome(value: Any) -> RawInvocationOutcome:
    if isinstance(value, RawInvocationOutcome):
        return value
    if isinstance(value, Mapping):
        return RawInvocationOutcome(output=deepcopy(dict(value)))
    return RawInvocationOutcome(
        failure=RawInvocationFailure.for_category("invalid_response", retryable=False)
    )


def _unavailable_outcome() -> RawInvocationOutcome:
    return RawInvocationOutcome(
        failure=RawInvocationFailure.for_category("unavailable", retryable=True)
    )
