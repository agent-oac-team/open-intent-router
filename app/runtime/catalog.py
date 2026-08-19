from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from inspect import isawaitable, iscoroutinefunction
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, TypeVar

from jsonschema import Draft202012Validator, SchemaError

from app.core.config import Settings
from app.core.errors import RuntimeCatalogUnavailableError
from app.invokers.http import HttpAgentInvoker
from app.invokers.local_function import LocalFunctionInvoker, LocalFunctionRegistry
from app.invokers.mock import MockAgentInvoker
from app.invokers.ui_handoff import UiHandoffInvoker

logger = logging.getLogger(__name__)

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_VERSION_LENGTH = 128

AdapterFactory = Callable[["RuntimeAdapterContext"], object | Awaitable[object]]
AdapterHealthCheck = Callable[[object], bool | Awaitable[bool]]
AdapterLifecycleHook = Callable[[object], Awaitable[None]]


class RuntimeCatalogError(RuntimeError):
    """Base error for internal Runtime Catalog construction failures."""


class RuntimeCatalogValidationError(RuntimeCatalogError):
    """A descriptor is not safe to activate."""


class RuntimeCatalogActivationError(RuntimeCatalogError):
    """A validated descriptor could not be activated into a healthy catalog."""


class RuntimeCatalogKeyError(RuntimeCatalogError):
    """A caller requested a key absent from the frozen catalog."""


@dataclass(frozen=True, slots=True)
class RuntimeAdapterCapability:
    """Stable capabilities declared by a trusted deployment adapter.

    ``v2_invocation`` is deliberately separate from generic invocation support:
    legacy adapters may still be installed for the old Definition contract while
    being unable to consume an ``oir-agent-v2`` Invocation Binding.
    """

    invocation: bool
    cancellation: bool = False
    v2_invocation: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeAdapterLifecycle:
    """Async lifecycle hooks owned by the process-level catalog, never a request."""

    activate: AdapterLifecycleHook
    dispose: AdapterLifecycleHook


@dataclass(frozen=True, slots=True)
class RuntimeAdapterDescriptor:
    """Trusted, deployment-registered Runtime Adapter contract."""

    key: str
    contract_version: str
    implementation_version: str
    config_schema: Mapping[str, Any]
    capability: RuntimeAdapterCapability
    factory: AdapterFactory
    health_check: AdapterHealthCheck
    lifecycle: RuntimeAdapterLifecycle

    def __post_init__(self) -> None:
        if isinstance(self.config_schema, Mapping):
            try:
                object.__setattr__(
                    self, "config_schema", MappingProxyType(dict(self.config_schema))
                )
            except Exception:
                # Validation reports malformed trusted deployment descriptors as
                # a safe readiness failure during application startup.
                return


@dataclass(frozen=True, slots=True)
class RuntimeAdapterContext:
    """Process-scoped dependencies available to descriptor factories."""

    settings: Settings
    local_functions: LocalFunctionRegistry | None = None


@dataclass(frozen=True, slots=True)
class RuntimeCatalogStatus:
    status: Literal["not_started", "starting", "ready", "error", "stopped"]
    reason_code: str | None = None
    adapter_count: int = 0


@dataclass(frozen=True, slots=True)
class _ActivatedAdapter:
    descriptor: RuntimeAdapterDescriptor
    adapter: object


class RuntimeCatalog:
    """A frozen, process-scoped mapping of activated Runtime Adapters."""

    def __init__(
        self, activated: Sequence[_ActivatedAdapter], *, shutdown_timeout_seconds: float
    ) -> None:
        self._activated = tuple(activated)
        self._adapters: Mapping[str, object] = MappingProxyType(
            {item.descriptor.key: item.adapter for item in self._activated}
        )
        self._descriptors: Mapping[str, RuntimeAdapterDescriptor] = MappingProxyType(
            {
                item.descriptor.key: _copy_descriptor_metadata(item.descriptor)
                for item in self._activated
            }
        )
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._closed = False

    @classmethod
    async def activate(
        cls,
        descriptors: Sequence[RuntimeAdapterDescriptor],
        context: RuntimeAdapterContext,
        *,
        shutdown_timeout_seconds: float,
    ) -> RuntimeCatalog:
        activated: list[_ActivatedAdapter] = []
        try:
            _validate_shutdown_timeout(shutdown_timeout_seconds)
            validated = _validate_descriptors(descriptors)
            for descriptor in validated:
                adapter = await _resolve(descriptor.factory(context))
                if adapter is None:
                    raise RuntimeCatalogActivationError(
                        f"Runtime Adapter factory returned no adapter: {descriptor.key}"
                    )
                entry = _ActivatedAdapter(descriptor=descriptor, adapter=adapter)
                activated.append(entry)
                await _run_lifecycle_hook(descriptor.lifecycle.activate, adapter)
                healthy = await _resolve(descriptor.health_check(adapter))
                if healthy is not True:
                    raise RuntimeCatalogActivationError(
                        f"Runtime Adapter is unhealthy after activation: {descriptor.key}"
                    )
        except RuntimeCatalogError:
            await _dispose_reverse(activated, timeout_seconds=shutdown_timeout_seconds)
            raise
        except Exception as exc:
            await _dispose_reverse(activated, timeout_seconds=shutdown_timeout_seconds)
            raise RuntimeCatalogActivationError("Runtime Catalog activation failed") from exc
        return cls(activated, shutdown_timeout_seconds=shutdown_timeout_seconds)

    @property
    def adapters(self) -> Mapping[str, object]:
        """Read-only adapter mapping committed after the entire activation succeeds."""
        return self._adapters

    def keys(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def has(self, key: str) -> bool:
        return key in self._adapters

    def get(self, key: str) -> object:
        adapter = self._adapters.get(key)
        if adapter is None:
            raise RuntimeCatalogKeyError(f"No Runtime Adapter registered for key: {key}")
        return adapter

    def descriptor(self, key: str) -> RuntimeAdapterDescriptor:
        """Return a defensive metadata copy without exposing a live adapter instance."""

        descriptor = self._descriptors.get(key)
        if descriptor is None:
            raise RuntimeCatalogKeyError(f"No Runtime Adapter registered for key: {key}")
        return _copy_descriptor_metadata(descriptor)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _dispose_reverse(
            self._activated,
            timeout_seconds=self._shutdown_timeout_seconds,
        )


class RuntimeCatalogRuntime:
    """Owns one Runtime Catalog for an application lifespan and its readiness state."""

    def __init__(
        self,
        descriptors: Sequence[RuntimeAdapterDescriptor],
        context: RuntimeAdapterContext,
        *,
        shutdown_timeout_seconds: float,
    ) -> None:
        self._descriptors = descriptors
        self._context = context
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._catalog: RuntimeCatalog | None = None
        self._status = RuntimeCatalogStatus(status="not_started")
        self._lock = asyncio.Lock()

    @property
    def catalog(self) -> RuntimeCatalog | None:
        return self._catalog

    @property
    def status(self) -> RuntimeCatalogStatus:
        return self._status

    async def start(self) -> None:
        async with self._lock:
            if self._catalog is not None:
                return
            if self._status.status == "error":
                return
            self._status = RuntimeCatalogStatus(status="starting")
            try:
                catalog = await RuntimeCatalog.activate(
                    self._descriptors,
                    self._context,
                    shutdown_timeout_seconds=self._shutdown_timeout_seconds,
                )
            except Exception:
                logger.exception("runtime_catalog_activation_failed")
                self._catalog = None
                self._status = RuntimeCatalogStatus(
                    status="error",
                    reason_code="runtime_catalog_activation_failed",
                )
                return
            self._catalog = catalog
            self._status = RuntimeCatalogStatus(
                status="ready",
                adapter_count=len(catalog.keys()),
            )

    async def get_catalog(self) -> RuntimeCatalog:
        catalog = self._catalog
        if catalog is None:
            raise RuntimeCatalogUnavailableError("Runtime Catalog is unavailable")
        return catalog

    async def stop(self) -> None:
        async with self._lock:
            catalog = self._catalog
            self._catalog = None
            if catalog is not None:
                await catalog.aclose()
            self._status = RuntimeCatalogStatus(status="stopped")


def build_default_runtime_descriptors() -> tuple[RuntimeAdapterDescriptor, ...]:
    """Explicit trusted registration for the built-in adapters shipped by OIR."""
    no_op_lifecycle = RuntimeAdapterLifecycle(activate=_noop, dispose=_noop)
    invocation_capability = RuntimeAdapterCapability(invocation=True)
    return (
        RuntimeAdapterDescriptor(
            key="mock",
            contract_version="oir-runtime-adapter-v1",
            implementation_version="oir-0.1.0",
            config_schema={"type": "object"},
            capability=invocation_capability,
            factory=lambda _context: MockAgentInvoker(),
            health_check=_healthy,
            lifecycle=no_op_lifecycle,
        ),
        RuntimeAdapterDescriptor(
            key="http",
            contract_version="oir-runtime-adapter-v1",
            implementation_version="oir-0.1.0",
            config_schema={"type": "object"},
            capability=invocation_capability,
            factory=lambda context: HttpAgentInvoker(context.settings),
            health_check=_http_health,
            lifecycle=RuntimeAdapterLifecycle(
                activate=_activate_http,
                dispose=_dispose_http,
            ),
        ),
        RuntimeAdapterDescriptor(
            key="local_function",
            contract_version="oir-runtime-adapter-v1",
            implementation_version="oir-0.1.0",
            config_schema={"type": "object"},
            capability=invocation_capability,
            factory=lambda context: LocalFunctionInvoker(context.local_functions),
            health_check=_healthy,
            lifecycle=no_op_lifecycle,
        ),
        # This is a temporary compatibility adapter for the legacy definition
        # model. Ticket #43 moves UI Handoff out of the Invocation runtime path.
        RuntimeAdapterDescriptor(
            key="ui_handoff",
            contract_version="oir-runtime-adapter-v1",
            implementation_version="oir-0.1.0",
            config_schema={"type": "object"},
            capability=invocation_capability,
            factory=lambda _context: UiHandoffInvoker(),
            health_check=_healthy,
            lifecycle=no_op_lifecycle,
        ),
    )


def _validate_descriptors(
    descriptors: Sequence[RuntimeAdapterDescriptor],
) -> tuple[RuntimeAdapterDescriptor, ...]:
    try:
        validated = tuple(descriptors)
    except TypeError as exc:
        raise RuntimeCatalogValidationError("Runtime Catalog descriptors are invalid") from exc
    seen_keys: set[str] = set()
    for descriptor in validated:
        if not isinstance(descriptor, RuntimeAdapterDescriptor):
            raise RuntimeCatalogValidationError("Runtime Catalog descriptor is invalid")
        if not isinstance(descriptor.key, str) or not _KEY_PATTERN.fullmatch(descriptor.key):
            raise RuntimeCatalogValidationError("Runtime Adapter key must be a stable identifier")
        if descriptor.key in seen_keys:
            raise RuntimeCatalogValidationError(f"Duplicate Runtime Adapter key: {descriptor.key}")
        seen_keys.add(descriptor.key)
        for version in (descriptor.contract_version, descriptor.implementation_version):
            if (
                not isinstance(version, str)
                or not version.strip()
                or len(version) > _MAX_VERSION_LENGTH
            ):
                raise RuntimeCatalogValidationError("Runtime Adapter version is invalid")
        if not isinstance(descriptor.config_schema, Mapping):
            raise RuntimeCatalogValidationError(
                f"Runtime Adapter config schema is invalid: {descriptor.key}"
            )
        if not isinstance(descriptor.capability, RuntimeAdapterCapability):
            raise RuntimeCatalogValidationError("Runtime Adapter capability is invalid")
        if not isinstance(descriptor.capability.invocation, bool):
            raise RuntimeCatalogValidationError("Runtime Adapter invocation capability is invalid")
        if not isinstance(descriptor.capability.cancellation, bool):
            raise RuntimeCatalogValidationError(
                "Runtime Adapter cancellation capability is invalid"
            )
        if not isinstance(descriptor.capability.v2_invocation, bool):
            raise RuntimeCatalogValidationError(
                "Runtime Adapter v2 invocation capability is invalid"
            )
        if not callable(descriptor.factory):
            raise RuntimeCatalogValidationError("Runtime Adapter factory is required")
        if not callable(descriptor.health_check):
            raise RuntimeCatalogValidationError("Runtime Adapter health check is required")
        if not isinstance(descriptor.lifecycle, RuntimeAdapterLifecycle):
            raise RuntimeCatalogValidationError("Runtime Adapter lifecycle is required")
        if not _is_async_lifecycle_hook(
            descriptor.lifecycle.activate
        ) or not _is_async_lifecycle_hook(descriptor.lifecycle.dispose):
            raise RuntimeCatalogValidationError("Runtime Adapter lifecycle hooks must be async")
        try:
            Draft202012Validator.check_schema(dict(descriptor.config_schema))
        except (SchemaError, TypeError, ValueError) as exc:
            raise RuntimeCatalogValidationError(
                f"Runtime Adapter config schema is invalid: {descriptor.key}"
            ) from exc
    return validated


def _validate_shutdown_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise RuntimeCatalogValidationError("Runtime Catalog shutdown timeout must be positive")


def _is_async_lifecycle_hook(hook: object) -> bool:
    return callable(hook) and (
        iscoroutinefunction(hook) or iscoroutinefunction(type(hook).__call__)
    )


def _copy_descriptor_metadata(
    descriptor: RuntimeAdapterDescriptor,
) -> RuntimeAdapterDescriptor:
    """Detach Catalog metadata from deployment-owned mutable schema structures."""

    return RuntimeAdapterDescriptor(
        key=descriptor.key,
        contract_version=descriptor.contract_version,
        implementation_version=descriptor.implementation_version,
        config_schema=_deep_copy_schema(descriptor.config_schema),
        capability=descriptor.capability,
        factory=descriptor.factory,
        health_check=descriptor.health_check,
        lifecycle=descriptor.lifecycle,
    )


def _deep_copy_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_copy_schema(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy_schema(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_deep_copy_schema(item) for item in value)
    return deepcopy(value)


Value = TypeVar("Value")


async def _resolve(value: Value | Awaitable[Value]) -> Value:
    if isawaitable(value):
        return await value
    return value


async def _run_lifecycle_hook(hook: AdapterLifecycleHook, adapter: object) -> None:
    result = hook(adapter)
    if not isawaitable(result):
        raise RuntimeCatalogActivationError("Runtime Adapter lifecycle hook must be async")
    await result


async def _dispose_reverse(
    activated: Sequence[_ActivatedAdapter],
    *,
    timeout_seconds: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    for entry in reversed(activated):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            logger.warning("runtime_catalog_dispose_timeout key=%s", entry.descriptor.key)
            return
        try:
            await asyncio.wait_for(
                _run_lifecycle_hook(entry.descriptor.lifecycle.dispose, entry.adapter),
                timeout=remaining,
            )
        except TimeoutError:
            logger.warning("runtime_catalog_dispose_timeout key=%s", entry.descriptor.key)
        except Exception:
            logger.warning("runtime_catalog_dispose_failed key=%s", entry.descriptor.key)


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


async def _activate_http(adapter: object) -> None:
    if not isinstance(adapter, HttpAgentInvoker):
        raise RuntimeCatalogActivationError("HTTP Runtime Adapter type is invalid")
    await adapter.start()


async def _http_health(adapter: object) -> bool:
    if not isinstance(adapter, HttpAgentInvoker):
        return False
    return await adapter.health()


async def _dispose_http(adapter: object) -> None:
    if isinstance(adapter, HttpAgentInvoker):
        await adapter.aclose()
