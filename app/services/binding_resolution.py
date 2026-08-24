"""Resolve one trusted Registry Snapshot selection into an Invocation binding."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from inspect import isawaitable, iscoroutinefunction, signature
from typing import cast

from pydantic import ValidationError

from app.application import (
    ConnectorResolutionRequest,
    ConnectorResolverApplicationPort,
    ResolvedConnector,
)
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
from app.schemas.common import UserContext
from app.schemas.invocation import AgentInvocation, AgentInvocationResult
from app.schemas.logs import (
    InvocationBindingSnapshot,
    binding_version_fingerprint,
    is_safe_binding_identifier,
)
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

    def with_connector(
        self,
        connector: ResolvedConnector,
        *,
        late_task_cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> ResolvedInvocationBinding:
        """Attach one request-only Connector without changing selected Handling.

        The static Snapshot selected the Adapter and its configuration before
        Connector lookup. This method only carries the private Connector to
        that already selected Runtime Adapter and projects its safe revision
        into the binding snapshot used for persistence.
        """

        execution = self.runtime_execution
        if execution is None:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "connector_adapter_incompatible"},
            )
        return replace(
            self,
            persistence_snapshot=self.persistence_snapshot.model_copy(
                update={"connector_revision": connector.revision}
            ),
            runtime_execution=replace(
                execution,
                connector=connector,
                late_task_cleanup=late_task_cleanup,
            ),
        )

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


@dataclass(slots=True)
class _ConnectorLease:
    """One request-scoped Connector whose release may follow a late Adapter."""

    resolver: ConnectorResolverApplicationPort
    connector: ResolvedConnector
    _late_task_claimed: bool = False
    _released: bool = False

    def defer_release_until_late_task_finishes(self) -> Awaitable[None]:
        """Transfer cleanup to Runtime after it retains a late Adapter task."""

        self._late_task_claimed = True
        return self.release()

    async def close(self) -> None:
        """Release now unless Runtime owns cleanup for a late Adapter task."""

        if not self._late_task_claimed:
            await self.release()

    async def release(self) -> None:
        """Release once; diagnostics from deployment cleanup never cross Core."""

        if self._released:
            return
        self._released = True
        await BindingResolver._release_connector(self.resolver, self.connector)


class BindingResolver:
    """Resolve only the immutable selection supplied by Registry Snapshot Runtime."""

    def __init__(
        self,
        runtime_catalog: RuntimeCatalog,
        *,
        connector_resolver: ConnectorResolverApplicationPort | None = None,
    ) -> None:
        self._runtime_catalog = runtime_catalog
        self._connector_resolver = connector_resolver

    @asynccontextmanager
    async def open_connector(
        self,
        binding: ResolvedInvocationBinding,
        *,
        principal: UserContext,
    ) -> AsyncIterator[ResolvedInvocationBinding]:
        """Resolve then always release one Connector around an accepted call.

        A Connector-bearing legacy Binding has no safe argument through which
        to pass the private capability.  It therefore fails closed before
        acceptance rather than silently bypassing Connector resolution.
        """

        if binding.requirement.connector_ref is None:
            yield binding
            return
        if binding.runtime_execution is None:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "connector_adapter_incompatible"},
            )

        if not binding.definition.access_policy.allows(principal):
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "connector_unauthorized"},
            )
        if (
            binding.requirement.adapter_key != binding.adapter_key
            or binding.runtime_execution.binding.adapter_key != binding.adapter_key
        ):
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "connector_adapter_incompatible"},
            )

        tenant_id = principal.tenant_id
        resolver = self._connector_resolver
        if not tenant_id or resolver is None:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "connector_unavailable"},
            )

        request = ConnectorResolutionRequest(
            tenant_id=tenant_id,
            principal=principal.model_copy(deep=True),
            adapter_key=binding.adapter_key,
            connector_ref=binding.requirement.connector_ref,
        )
        try:
            candidate = resolver.resolve(request)
            if not isawaitable(candidate):
                raise TypeError("Connector Resolver must resolve asynchronously")
            connector = await candidate
        except Exception:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "connector_unavailable"},
            ) from None
        if not isinstance(connector, ResolvedConnector):
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "connector_unavailable"},
            )

        try:
            self._validate_resolved_connector(
                connector,
                tenant_id=tenant_id,
                adapter_key=binding.adapter_key,
                connector_ref=binding.requirement.connector_ref,
            )
        except InvocationBindingUnavailableError:
            await self._release_connector(resolver, connector)
            raise

        lease = _ConnectorLease(resolver=resolver, connector=connector)
        resolved = binding.with_connector(
            connector,
            late_task_cleanup=lease.defer_release_until_late_task_finishes,
        )

        try:
            yield resolved
        finally:
            await lease.close()

    @staticmethod
    def _validate_resolved_connector(
        connector: ResolvedConnector,
        *,
        tenant_id: str,
        adapter_key: str,
        connector_ref: str,
    ) -> None:
        """Fail closed if deployment output does not match the frozen binding."""

        if connector.tenant_id != tenant_id:
            reason_code = "connector_unauthorized"
        elif connector.adapter_key != adapter_key:
            reason_code = "connector_adapter_incompatible"
        elif connector.connector_ref != connector_ref:
            reason_code = "connector_reference_invalid"
        elif not is_safe_binding_identifier(connector.revision):
            reason_code = "connector_revision_invalid"
        else:
            return
        raise InvocationBindingUnavailableError(
            "Invocation Binding is unavailable",
            details={"reason_code": reason_code},
        )

    @staticmethod
    async def _release_connector(
        resolver: ConnectorResolverApplicationPort,
        connector: ResolvedConnector,
    ) -> None:
        """Best-effort cleanup must never disclose Connector diagnostics."""

        try:
            released = resolver.release(connector)
            if isawaitable(released):
                await released
        except Exception:
            return

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
                    limits=handling.limits,
                    requires_knowledge_context=(
                        definition.context.knowledge.requirement == "required"
                    ),
                    principal_claims=(
                        frozenset(handling.principal_projection.claims)
                        & descriptor.capability.accepted_principal_claims
                    ),
                    principal_attribute_keys=(
                        frozenset(handling.principal_projection.attribute_keys)
                        & descriptor.capability.accepted_principal_attribute_keys
                    ),
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
        signature(method).bind(object(), object(), object())
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
