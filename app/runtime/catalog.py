from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from inspect import isawaitable, iscoroutinefunction
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, TypeVar

from jsonschema import Draft202012Validator, SchemaError

from app.core.config import Settings
from app.core.errors import RuntimeCatalogUnavailableError
from app.schemas.agents import (
    INVOCATION_PRINCIPAL_CLAIMS,
    is_safe_invocation_principal_attribute_key,
)

logger = logging.getLogger(__name__)

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_VERSION_LENGTH = 128

AdapterFactory = Callable[["RuntimeAdapterContext"], object | Awaitable[object]]
AdapterHealthCheck = Callable[[object], Awaitable[bool]]
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
    an adapter must explicitly opt into consuming an ``oir-agent-v2`` Invocation
    Binding before it can be selected for a Native Definition.  During the
    Invocation Runtime expansion, ``invocation_runtime`` freezes which of the
    two supported execution protocols owns a Binding. ``cancellation`` is a
    separate opt-in for Runtime control: only a descriptor declaring it may
    expose the closed ``cancel(binding, control_envelope)`` protocol. It is a
    deployment declaration, not a request-time feature probe.
    """

    invocation: bool
    cancellation: bool = False
    v2_invocation: bool = False
    invocation_runtime: bool = False
    accepted_principal_claims: frozenset[str] = frozenset()
    accepted_principal_attribute_keys: frozenset[str] = frozenset()


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


@dataclass(frozen=True, slots=True)
class RuntimeCatalogStatus:
    status: Literal["not_started", "starting", "ready", "error", "stopped"]
    reason_code: str | None = None
    adapter_count: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeAdapterHealth:
    """One bounded health observation for an activated Runtime Adapter."""

    status: Literal["healthy", "unhealthy"]
    reason_code: Literal["runtime_adapter_unhealthy"] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeCatalogHealth:
    """Safe aggregate health used by readiness, never by request-time binding."""

    status: Literal["ready", "degraded", "error"]
    reason_code: str | None = None
    unhealthy_adapter_keys: frozenset[str] = frozenset()
    unhealthy_adapter_count: int = 0


@dataclass(frozen=True, slots=True)
class _ActivatedAdapter:
    descriptor: RuntimeAdapterDescriptor
    adapter: object


class RuntimeCatalog:
    """A frozen, process-scoped mapping of activated Runtime Adapters."""

    def __init__(
        self,
        activated: Sequence[_ActivatedAdapter],
        *,
        shutdown_timeout_seconds: float,
        initial_adapter_health: Mapping[str, RuntimeAdapterHealth],
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
        self._initial_adapter_health = MappingProxyType(dict(initial_adapter_health))
        self._closed = False

    @classmethod
    async def activate(
        cls,
        descriptors: Sequence[RuntimeAdapterDescriptor],
        context: RuntimeAdapterContext,
        *,
        shutdown_timeout_seconds: float,
        health_check_timeout_seconds: float = 5.0,
        allow_unhealthy: bool = False,
    ) -> RuntimeCatalog:
        activated: list[_ActivatedAdapter] = []
        initial_adapter_health: dict[str, RuntimeAdapterHealth] = {}
        try:
            _validate_shutdown_timeout(shutdown_timeout_seconds)
            _validate_health_check_timeout(health_check_timeout_seconds)
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
                health = await _check_adapter_health(
                    descriptor,
                    adapter,
                    timeout_seconds=health_check_timeout_seconds,
                )
                initial_adapter_health[descriptor.key] = health
                if health.status != "healthy" and not allow_unhealthy:
                    raise RuntimeCatalogActivationError(
                        f"Runtime Adapter is unhealthy after activation: {descriptor.key}"
                    )
        except RuntimeCatalogError:
            await _dispose_reverse(activated, timeout_seconds=shutdown_timeout_seconds)
            raise
        except Exception as exc:
            await _dispose_reverse(activated, timeout_seconds=shutdown_timeout_seconds)
            raise RuntimeCatalogActivationError("Runtime Catalog activation failed") from exc
        return cls(
            activated,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            initial_adapter_health=initial_adapter_health,
        )

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

    @property
    def initial_adapter_health(self) -> Mapping[str, RuntimeAdapterHealth]:
        """Health observed during atomic activation, detached from mutable adapters."""

        return self._initial_adapter_health

    async def check_health(
        self,
        *,
        timeout_seconds: float,
    ) -> Mapping[str, RuntimeAdapterHealth]:
        """Probe activated adapters without exposing exceptions or adapter objects."""

        _validate_health_check_timeout(timeout_seconds)
        observations: dict[str, RuntimeAdapterHealth] = {}
        for entry in self._activated:
            observations[entry.descriptor.key] = await _check_adapter_health(
                entry.descriptor,
                entry.adapter,
                timeout_seconds=timeout_seconds,
            )
        return MappingProxyType(observations)

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
        required_adapter_keys: Collection[str] = (),
        health_check_timeout_seconds: float = 5.0,
    ) -> None:
        self._descriptors = descriptors
        self._context = context
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._required_adapter_keys = frozenset(required_adapter_keys)
        self._health_check_timeout_seconds = health_check_timeout_seconds
        self._catalog: RuntimeCatalog | None = None
        self._status = RuntimeCatalogStatus(status="not_started")
        self._adapter_health: Mapping[str, RuntimeAdapterHealth] = MappingProxyType({})
        self._lock = asyncio.Lock()
        self._health_lock = asyncio.Lock()

    @property
    def catalog(self) -> RuntimeCatalog | None:
        return self._catalog

    @property
    def status(self) -> RuntimeCatalogStatus:
        return self._status

    @property
    def health(self) -> RuntimeCatalogHealth:
        """Return the most recent safe health state without starting a probe."""

        if self._status.status != "ready" or self._catalog is None:
            return RuntimeCatalogHealth(
                status="error",
                reason_code=self._status.reason_code or "runtime_catalog_not_ready",
            )
        keys = frozenset(self._catalog.keys())
        missing_required = self._required_adapter_keys - keys
        if missing_required:
            return RuntimeCatalogHealth(
                status="error",
                reason_code="runtime_required_adapter_missing",
            )
        unhealthy = frozenset(
            key
            for key, observation in self._adapter_health.items()
            if observation.status == "unhealthy"
        )
        if unhealthy & self._required_adapter_keys:
            return RuntimeCatalogHealth(
                status="error",
                reason_code="runtime_required_adapter_unhealthy",
                unhealthy_adapter_keys=unhealthy,
                unhealthy_adapter_count=len(unhealthy),
            )
        if unhealthy:
            return RuntimeCatalogHealth(
                status="degraded",
                reason_code="runtime_adapter_unhealthy",
                unhealthy_adapter_keys=unhealthy,
                unhealthy_adapter_count=len(unhealthy),
            )
        return RuntimeCatalogHealth(status="ready")

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
                    health_check_timeout_seconds=self._health_check_timeout_seconds,
                    allow_unhealthy=True,
                )
            except Exception:
                # Descriptor factories and lifecycle hooks are deployment code;
                # their exceptions can contain connection strings or credentials.
                # Readiness exposes the stable reason code below, and logs retain
                # only that same safe diagnostic.
                logger.warning("runtime_catalog_activation_failed")
                self._catalog = None
                self._adapter_health = MappingProxyType({})
                self._status = RuntimeCatalogStatus(
                    status="error",
                    reason_code="runtime_catalog_activation_failed",
                )
                return
            self._catalog = catalog
            self._adapter_health = catalog.initial_adapter_health
            self._status = RuntimeCatalogStatus(
                status="ready",
                adapter_count=len(catalog.keys()),
            )

    async def get_catalog(self) -> RuntimeCatalog:
        catalog = self._catalog
        if catalog is None:
            raise RuntimeCatalogUnavailableError("Runtime Catalog is unavailable")
        return catalog

    async def refresh_health(self) -> RuntimeCatalogHealth:
        """Refresh Adapter health for readiness without affecting the frozen Catalog."""

        async with self._health_lock:
            async with self._lock:
                catalog = self._catalog
                if catalog is None or self._status.status != "ready":
                    return self.health
            observations = await catalog.check_health(
                timeout_seconds=self._health_check_timeout_seconds,
            )
            async with self._lock:
                if self._catalog is catalog and self._status.status == "ready":
                    self._adapter_health = observations
                return self.health

    async def stop(self) -> None:
        # A probe uses an Adapter instance.  Do not start disposal after the
        # probe released the catalog lock but before it completes.  This gate is
        # deliberately shared with ``refresh_health`` rather than relying on a
        # best-effort closed flag in each Adapter.
        async with self._health_lock:
            async with self._lock:
                catalog = self._catalog
                self._catalog = None
                self._adapter_health = MappingProxyType({})
                if catalog is not None:
                    await catalog.aclose()
                self._status = RuntimeCatalogStatus(status="stopped")


def build_default_runtime_descriptors() -> tuple[RuntimeAdapterDescriptor, ...]:
    """Return no implicit Native Runtime Adapters.

    Native v2 Definitions bind only to trusted deployment descriptors supplied
    at application composition. Retired Invoker implementations are never
    activated as a fallback by the default Catalog.
    """

    return ()


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
        if not isinstance(descriptor.capability.invocation_runtime, bool):
            raise RuntimeCatalogValidationError(
                "Runtime Adapter Invocation Runtime capability is invalid"
            )
        if descriptor.capability.invocation_runtime and not descriptor.capability.v2_invocation:
            raise RuntimeCatalogValidationError(
                "Runtime Adapter Invocation Runtime capability requires v2 invocation"
            )
        if not isinstance(descriptor.capability.accepted_principal_claims, frozenset) or not all(
            isinstance(claim, str) and claim in INVOCATION_PRINCIPAL_CLAIMS
            for claim in descriptor.capability.accepted_principal_claims
        ):
            raise RuntimeCatalogValidationError(
                "Runtime Adapter accepted principal claims are invalid"
            )
        if not isinstance(
            descriptor.capability.accepted_principal_attribute_keys, frozenset
        ) or not all(
            is_safe_invocation_principal_attribute_key(key)
            for key in descriptor.capability.accepted_principal_attribute_keys
        ):
            raise RuntimeCatalogValidationError(
                "Runtime Adapter accepted principal attribute keys are invalid"
            )
        if not callable(descriptor.factory):
            raise RuntimeCatalogValidationError("Runtime Adapter factory is required")
        if not _is_async_callable(descriptor.health_check):
            raise RuntimeCatalogValidationError("Runtime Adapter health check must be async")
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


def _validate_health_check_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise RuntimeCatalogValidationError("Runtime Catalog health timeout must be positive")


def _is_async_lifecycle_hook(hook: object) -> bool:
    return _is_async_callable(hook)


def _is_async_callable(callback: object) -> bool:
    return callable(callback) and (
        iscoroutinefunction(callback) or iscoroutinefunction(type(callback).__call__)
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


async def _check_adapter_health(
    descriptor: RuntimeAdapterDescriptor,
    adapter: object,
    *,
    timeout_seconds: float,
) -> RuntimeAdapterHealth:
    """Normalize all adapter health outcomes to one non-leaking status."""

    try:
        healthy = await asyncio.wait_for(
            descriptor.health_check(adapter),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning("runtime_catalog_adapter_health_timeout key=%s", descriptor.key)
        return RuntimeAdapterHealth(
            status="unhealthy",
            reason_code="runtime_adapter_unhealthy",
        )
    except Exception:
        logger.warning("runtime_catalog_adapter_health_failed key=%s", descriptor.key)
        return RuntimeAdapterHealth(
            status="unhealthy",
            reason_code="runtime_adapter_unhealthy",
        )
    if healthy is True:
        return RuntimeAdapterHealth(status="healthy")
    return RuntimeAdapterHealth(
        status="unhealthy",
        reason_code="runtime_adapter_unhealthy",
    )


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
