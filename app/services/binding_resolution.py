"""Resolve one trusted Registry Snapshot selection into an Invocation binding."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable, iscoroutinefunction, signature
from typing import cast

from pydantic import ValidationError

from app.core.errors import (
    DirectInvocationUnsupportedError,
    InvocationBindingUnavailableError,
    InvocationError,
)
from app.runtime.catalog import RuntimeCatalog, RuntimeCatalogKeyError
from app.runtime.invocation import (
    RuntimeAdapterBinding,
    RuntimeAdapterExecution,
    RuntimeAdapterExecutor,
)
from app.schemas.agents import AgentDefinitionV2, InvocationHandling
from app.schemas.invocation import AgentInvocation, AgentInvocationResult
from app.schemas.logs import InvocationBindingSnapshot, binding_version_fingerprint
from app.services.registry_snapshot import (
    InvocationBindingRequirement,
    RegistrySnapshotSelection,
)


@dataclass(frozen=True, slots=True)
class ResolvedInvocationBinding:
    """Ephemeral runtime binding for one already-authorized Snapshot selection."""

    selection: RegistrySnapshotSelection
    definition: AgentDefinitionV2
    requirement: InvocationBindingRequirement
    adapter_key: str
    persistence_snapshot: InvocationBindingSnapshot
    runtime_execution: RuntimeAdapterExecution | None = None
    _invoke_v2: (
        Callable[
            [AgentDefinitionV2, InvocationBindingRequirement, AgentInvocation],
            Awaitable[AgentInvocationResult],
        ]
        | None
    ) = field(default=None, repr=False)

    async def invoke(self, invocation: AgentInvocation) -> AgentInvocationResult:
        """Execute the retained pre-Runtime Adapter protocol during expansion.

        New Adapters resolve to ``runtime_execution`` and are always called by
        ``InvocationRuntime``.  The legacy branch is intentionally temporary:
        it keeps existing deployed test and Host entry points green while the
        remaining Adapter contracts migrate.
        """

        invoke_v2 = self._invoke_v2
        if invoke_v2 is None:
            raise InvocationError("Runtime Adapter must execute through Invocation Runtime")
        result = invoke_v2(self.definition, self.requirement, invocation)
        if not isawaitable(result):
            raise InvocationError("Runtime Adapter v2 Invocation must be async")
        result = await result
        if not isinstance(result, AgentInvocationResult):
            raise InvocationError("Runtime Adapter returned an invalid Invocation result")
        return result


class BindingResolver:
    """Resolve only the immutable selection supplied by Registry Snapshot Runtime."""

    def __init__(self, runtime_catalog: RuntimeCatalog) -> None:
        self._runtime_catalog = runtime_catalog

    def resolve_direct_invocation(
        self,
        selection: RegistrySnapshotSelection,
    ) -> ResolvedInvocationBinding:
        definition = selection.definition
        handling = definition.handling
        if not isinstance(handling, InvocationHandling):
            raise DirectInvocationUnsupportedError(
                "Direct Invoke only accepts Invocation Handling",
                details={"handling_kind": handling.kind},
            )

        entry = selection.entry
        if entry.binding_status != "ready":
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": entry.isolation_reason_code or "binding_unavailable"},
            )
        requirement = entry.binding_requirement
        if not isinstance(requirement, InvocationBindingRequirement):
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "binding_requirement_invalid"},
            )

        try:
            descriptor = self._runtime_catalog.descriptor(requirement.adapter_key)
            adapter = self._runtime_catalog.get(requirement.adapter_key)
        except RuntimeCatalogKeyError as exc:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "invocation_adapter_missing"},
            ) from exc
        if not descriptor.capability.invocation:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "invocation_adapter_unsupported"},
            )
        if not descriptor.capability.v2_invocation:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "invocation_adapter_incompatible"},
            )
        try:
            persistence_snapshot = InvocationBindingSnapshot(
                adapter_key=descriptor.key,
                adapter_contract_version=binding_version_fingerprint(descriptor.contract_version),
                adapter_implementation_version=binding_version_fingerprint(
                    descriptor.implementation_version
                ),
                connector_ref=requirement.connector_ref,
            )
        except ValidationError as exc:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "binding_snapshot_invalid"},
            ) from exc
        if requirement.execution_protocol == "runtime_adapter":
            try:
                runtime_execute = getattr(adapter, "execute", None)
            except Exception as exc:
                raise InvocationBindingUnavailableError(
                    "Invocation Binding is unavailable",
                    details={"reason_code": "invocation_adapter_incompatible"},
                ) from exc
            if not _supports_runtime_adapter_protocol(runtime_execute):
                raise InvocationBindingUnavailableError(
                    "Invocation Binding is unavailable",
                    details={"reason_code": "invocation_adapter_incompatible"},
                )
            return ResolvedInvocationBinding(
                selection=selection,
                definition=definition,
                requirement=requirement,
                adapter_key=descriptor.key,
                persistence_snapshot=persistence_snapshot,
                runtime_execution=RuntimeAdapterExecution(
                    binding=RuntimeAdapterBinding(
                        adapter_key=descriptor.key,
                        config=requirement.config,
                    ),
                    execute=cast(RuntimeAdapterExecutor, runtime_execute),
                ),
            )
        if requirement.execution_protocol != "legacy_v2":
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "invocation_adapter_incompatible"},
            )
        try:
            invoke_v2 = getattr(adapter, "invoke_v2", None)
        except Exception as exc:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "invocation_adapter_incompatible"},
            ) from exc
        if not _supports_v2_invocation_protocol(invoke_v2):
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "invocation_adapter_incompatible"},
            )
        return ResolvedInvocationBinding(
            selection=selection,
            definition=definition,
            requirement=requirement,
            adapter_key=descriptor.key,
            persistence_snapshot=persistence_snapshot,
            _invoke_v2=cast(
                Callable[
                    [AgentDefinitionV2, InvocationBindingRequirement, AgentInvocation],
                    Awaitable[AgentInvocationResult],
                ],
                invoke_v2,
            ),
        )


def _supports_runtime_adapter_protocol(method: object) -> bool:
    if not callable(method) or not iscoroutinefunction(method):
        return False
    try:
        signature(method).bind(object(), object())
    except Exception:
        return False
    return True


def _supports_v2_invocation_protocol(method: object) -> bool:
    """Keep protocol mismatches on the pre-acceptance side of Direct Invoke."""

    if not callable(method) or not iscoroutinefunction(method):
        return False
    try:
        signature(method).bind(object(), object(), object())
    except Exception:
        return False
    return True
