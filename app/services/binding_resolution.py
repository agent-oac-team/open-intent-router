"""Resolve one trusted Registry Snapshot selection into an Invocation binding."""

from __future__ import annotations

import asyncio
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
    InvocationDeadlineExceededError,
    InvocationError,
)
from app.runtime.catalog import RuntimeCatalog, RuntimeCatalogKeyError
from app.runtime.invocation import (
    InvocationDeadline,
    RuntimeAdapterBinding,
    RuntimeAdapterCanceller,
    RuntimeAdapterConnectorPreparer,
    RuntimeAdapterConnectorValidator,
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
    _release_before_terminal: Callable[[], Awaitable[None]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def with_connector(
        self,
        connector: ResolvedConnector,
        *,
        late_task_cleanup: Callable[[], Awaitable[None]] | None = None,
        release_before_terminal: Callable[[], Awaitable[None]] | None = None,
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
            _release_before_terminal=release_before_terminal,
        )

    async def release_before_terminal(self) -> None:
        """Release this request's Connector before a Run becomes terminal.

        The connector scope is normally managed by ``open_connector``.  For
        an accepted Invocation, however, release remains part of the one
        absolute deadline: exposing this narrow callback lets the Service
        project a release timeout into the same durable terminal outcome
        instead of discovering it only after a success was committed.
        """

        release = self._release_before_terminal
        if release is not None:
            await release()

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

    @property
    def release_is_deferred(self) -> bool:
        return self._late_task_claimed

    @property
    def released(self) -> bool:
        return self._released

    def defer_release(self) -> Awaitable[None]:
        """Transfer a release to the Runtime's observed cleanup ownership."""

        self._late_task_claimed = True
        return self.release()

    def defer_release_until_late_task_finishes(self) -> Awaitable[None]:
        """Transfer cleanup to Runtime after it retains a late Adapter task."""

        return self.defer_release()

    def defer_release_task(self, task: asyncio.Future[object]) -> Awaitable[None]:
        """Let Runtime drain an already-started, deadline-late release.

        The request path awaits a shield around ``task``, so its deadline
        cancellation cannot cancel the actual deployment release. Return that
        original task directly to Runtime instead of a shielded wrapper: the
        lifecycle stop path must be able to cancel and drain the real release
        task before Catalog disposal.
        """

        self._late_task_claimed = True
        return cast(Awaitable[None], task)

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
        deadline: InvocationDeadline | None = None,
    ) -> AsyncIterator[ResolvedInvocationBinding]:
        """Resolve then always release one Connector around an accepted call.

        A Connector-bearing legacy Binding has no safe argument through which
        to pass the private capability.  It therefore fails closed before
        acceptance rather than silently bypassing Connector resolution.
        """

        if binding.requirement.connector_ref is None:
            if (
                binding.runtime_execution is not None
                and binding.runtime_execution.requires_connector
            ):
                raise InvocationBindingUnavailableError(
                    "Invocation Binding is unavailable",
                    details={"reason_code": "connector_unavailable"},
                )
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

        if deadline is not None:
            deadline.require_remaining()
        request = ConnectorResolutionRequest(
            tenant_id=tenant_id,
            principal=principal.model_copy(deep=True),
            adapter_key=binding.adapter_key,
            connector_ref=binding.requirement.connector_ref,
            deadline_at=deadline.deadline_at if deadline is not None else None,
        )
        try:
            candidate = resolver.resolve(request)
            if not isawaitable(candidate):
                raise TypeError("Connector Resolver must resolve asynchronously")
            if deadline is None:
                connector = await candidate
            else:
                connector = await deadline.wait_for(
                    candidate,
                    on_late_task=lambda task: _release_late_resolved_connector(resolver, task),
                )
        except InvocationDeadlineExceededError:
            raise
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

        # The request-scoped lease begins as soon as a valid Connector exists,
        # before any async Adapter preparation.  DNS and other preparers may
        # be cancelled while awaiting, and must not strand Connector secrets
        # or short-lived clients in that window.
        lease = _ConnectorLease(resolver=resolver, connector=connector)
        try:
            if deadline is not None:
                deadline.require_remaining()
            self._validate_resolved_connector(
                connector,
                tenant_id=tenant_id,
                adapter_key=binding.adapter_key,
                connector_ref=binding.requirement.connector_ref,
            )
            self._validate_connector_for_adapter(binding, connector)
            prepared_connector = await self._prepare_connector_for_adapter(
                binding,
                connector,
                deadline=deadline,
                late_task_cleanup=(
                    (lambda _task: lease.defer_release_until_late_task_finishes())
                    if deadline is not None
                    else None
                ),
            )
            if deadline is not None:
                deadline.require_remaining()
            self._validate_resolved_connector(
                prepared_connector,
                tenant_id=tenant_id,
                adapter_key=binding.adapter_key,
                connector_ref=binding.requirement.connector_ref,
            )
            resolved = binding.with_connector(
                prepared_connector,
                late_task_cleanup=lease.defer_release_until_late_task_finishes,
                release_before_terminal=lambda: self._close_lease_before_terminal(
                    lease,
                    deadline=deadline,
                ),
            )
            yield resolved
        finally:
            # Connector release is also part of the request's absolute
            # budget.  Checking only before entering ``close`` is racy: a
            # deployment release can begin with 1ms remaining and then block
            # indefinitely.  Shield the actual release and hand it to the
            # Runtime if the remaining budget elapses, so the response never
            # waits while lifecycle still owns the request-scoped resource.
            if not lease.released and not lease.release_is_deferred:
                if deadline is None:
                    await lease.close()
                else:
                    try:
                        await self._close_lease_before_terminal(lease, deadline=deadline)
                    except InvocationDeadlineExceededError:
                        # A body that has not used the accepted-call callback
                        # is already raising its own deadline error. The
                        # Runtime's late-cleanup observer owns the actual
                        # release; do not hide that failure with cleanup
                        # latency.
                        if not lease.release_is_deferred:
                            raise

    @staticmethod
    async def _close_lease_before_terminal(
        lease: _ConnectorLease,
        *,
        deadline: InvocationDeadline | None,
    ) -> None:
        """Release under the shared budget and retain a late deployment task."""

        if lease.released or lease.release_is_deferred:
            return
        if deadline is None:
            await lease.close()
            return
        release_task = asyncio.create_task(lease.close())
        try:
            await deadline.wait_for(
                asyncio.shield(release_task),
                on_late_task=lambda _task: lease.defer_release_task(release_task),
            )
        except InvocationDeadlineExceededError:
            # ``wait_for`` invokes the cleanup hook even in the immediate
            # expiry case. Keep this defensive branch for non-standard
            # Deadline implementations used by narrow Host tests.
            if not lease.release_is_deferred:
                deadline.observe_late_cleanup(lease.defer_release_task(release_task))
            raise

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
    def _validate_connector_for_adapter(
        binding: ResolvedInvocationBinding,
        connector: ResolvedConnector,
    ) -> None:
        """Run an Adapter's local Connector policy before Run acceptance."""

        execution = binding.runtime_execution
        validator = execution.connector_validator if execution is not None else None
        if validator is None:
            return
        try:
            accepted = validator(execution.binding, connector)
        except Exception:
            accepted = False
        if isawaitable(accepted):
            closer = getattr(accepted, "close", None)
            if callable(closer):
                closer()
            accepted = False
        if accepted is True:
            return
        raise InvocationBindingUnavailableError(
            "Invocation Binding is unavailable",
            details={"reason_code": "connector_unavailable"},
        )

    @staticmethod
    async def _prepare_connector_for_adapter(
        binding: ResolvedInvocationBinding,
        connector: ResolvedConnector,
        *,
        deadline: InvocationDeadline | None = None,
        late_task_cleanup: Callable[[asyncio.Future[object]], Awaitable[None] | None] | None = None,
    ) -> ResolvedConnector:
        """Let an Adapter attach request-only verified transport facts."""

        execution = binding.runtime_execution
        preparer = execution.connector_preparer if execution is not None else None
        if preparer is None:
            return connector
        try:
            candidate = preparer(execution.binding, connector)
            if not isawaitable(candidate):
                raise TypeError("Connector preparer must be async")
            if deadline is None:
                prepared = await candidate
            else:
                prepared = await deadline.wait_for(
                    candidate,
                    on_late_task=late_task_cleanup,
                )
        except InvocationDeadlineExceededError:
            raise
        except Exception:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "connector_unavailable"},
            ) from None
        if isinstance(prepared, ResolvedConnector):
            return prepared
        raise InvocationBindingUnavailableError(
            "Invocation Binding is unavailable",
            details={"reason_code": "connector_unavailable"},
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
            connector_validator = _runtime_connector_validator(adapter)
            connector_preparer = _runtime_connector_preparer(adapter)
            requires_connector = _runtime_adapter_requires_connector(adapter)
            canceller = _runtime_adapter_canceller(
                adapter,
                capability_declared=descriptor.capability.cancellation,
            )
            if descriptor.capability.cancellation and canceller is None:
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
                    cancel=canceller,
                    connector_validator=connector_validator,
                    connector_preparer=connector_preparer,
                    requires_connector=requires_connector,
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


def _runtime_connector_validator(adapter: object) -> RuntimeAdapterConnectorValidator | None:
    """Capture an optional local-only Connector validator once with the Adapter."""

    try:
        candidate = getattr(adapter, "validate_connector", None)
    except Exception:
        return None
    if not callable(candidate) or iscoroutinefunction(candidate):
        return None
    try:
        signature(candidate).bind(object(), object())
    except Exception:
        return None
    return cast(RuntimeAdapterConnectorValidator, candidate)


def _runtime_connector_preparer(adapter: object) -> RuntimeAdapterConnectorPreparer | None:
    """Capture an optional async Connector preparer once with the Adapter."""

    try:
        candidate = getattr(adapter, "prepare_connector", None)
    except Exception:
        return None
    if not callable(candidate) or not iscoroutinefunction(candidate):
        return None
    try:
        signature(candidate).bind(object(), object())
    except Exception:
        return None
    return cast(RuntimeAdapterConnectorPreparer, candidate)


def _runtime_adapter_canceller(
    adapter: object,
    *,
    capability_declared: bool,
) -> RuntimeAdapterCanceller | None:
    """Capture control only after the trusted descriptor explicitly opts in.

    In particular, do not even read ``adapter.cancel`` for an Adapter whose
    deployment Descriptor lacks cancellation capability. This prevents a
    legacy/foreign control method from becoming an accidental Runtime surface.
    """

    if not capability_declared:
        return None
    try:
        candidate = getattr(adapter, "cancel", None)
    except Exception:
        return None
    if not callable(candidate) or not iscoroutinefunction(candidate):
        return None
    try:
        signature(candidate).bind(object(), object())
    except Exception:
        return None
    return cast(RuntimeAdapterCanceller, candidate)


def _runtime_adapter_requires_connector(adapter: object) -> bool:
    """Read an explicit Adapter declaration without trusting a truthy foreign value."""

    try:
        return getattr(adapter, "requires_connector", False) is True
    except Exception:
        return False


def _supports_v2_invocation_protocol(method: object) -> bool:
    """Keep protocol mismatches on the pre-acceptance side of Direct Invoke."""

    if not callable(method) or not iscoroutinefunction(method):
        return False
    try:
        signature(method).bind(object(), object(), object())
    except Exception:
        return False
    return True


def _release_late_resolved_connector(
    resolver: ConnectorResolverApplicationPort,
    task: asyncio.Future[object],
) -> Awaitable[None]:
    """Release a Connector returned after its pre-acceptance budget expired."""

    async def release() -> None:
        result = getattr(task, "result", None)
        if not callable(result):
            return
        try:
            connector = result()
        except (asyncio.CancelledError, Exception):
            return
        if isinstance(connector, ResolvedConnector):
            await BindingResolver._release_connector(resolver, connector)

    return release()
