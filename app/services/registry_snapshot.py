"""Compile trusted v2 Agent Definitions into an immutable Registry Snapshot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal, TypeAlias

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.application.ports import (
    ExternalExecutorApplicationPort,
    RegistrySnapshotQuarantineInput,
)
from app.core.errors import RegistryError
from app.runtime.catalog import RuntimeCatalog
from app.schemas.agents import (
    AgentDefinitionV2,
    AgentHandlingKind,
    AgentPublicV2,
    CandidateAgentV2,
    ExternalExecutionHandling,
    InvocationHandling,
    UiHandoffHandling,
    is_safe_agent_identifier,
)
from app.schemas.common import UserContext

BindingStatus = Literal["ready", "isolated", "disabled"]
RegistrySnapshotRuntimeState = Literal["not_loaded", "ready", "degraded", "error"]
_SAFE_ISOLATION_REASON_CODES = frozenset(
    {
        "invocation_adapter_missing",
        "invocation_adapter_unsupported",
        "invocation_adapter_incompatible",
        "invocation_adapter_unhealthy",
        "invocation_config_invalid",
        "external_executor_unsupported",
    }
)
_SAFE_QUARANTINE_REASON_CODES = frozenset(
    {
        "definition_schema_invalid",
        "definition_configuration_invalid",
        "duplicate_agent_id",
        "legacy_definition_unmappable",
    }
)


class RegistryDefinitionValidationError(RegistryError):
    """A v2 Definition cannot safely enter a Registry Snapshot."""

    code = "registry_definition_invalid"

    def __init__(self, reason_code: str) -> None:
        super().__init__(
            "Agent Definition is invalid for this deployment",
            details={"reason_code": reason_code},
        )
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class InvocationBindingRequirement:
    """Stable Invocation constraints; no activated Adapter object is retained."""

    adapter_key: str
    connector_ref: str | None
    _config_json: str = field(repr=False)
    kind: Literal["invocation"] = field(default="invocation", init=False)

    @property
    def config(self) -> Mapping[str, object]:
        return _frozen_configuration(self._config_json)

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "adapter_key": self.adapter_key,
            "connector_ref": self.connector_ref,
            "config": dict(self.config),
        }


@dataclass(frozen=True, slots=True)
class ExternalExecutionBindingRequirement:
    """Stable host-neutral External Execution constraints."""

    executor_ref: str
    _params_json: str = field(repr=False)
    kind: Literal["external_execution"] = field(default="external_execution", init=False)

    @property
    def params(self) -> Mapping[str, object]:
        return _frozen_configuration(self._params_json)

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "executor_ref": self.executor_ref,
            "params": dict(self.params),
        }


@dataclass(frozen=True, slots=True)
class UiHandoffBindingRequirement:
    """Stable Host collaboration constraints rather than an Invocation binding."""

    route: str
    _params_json: str = field(repr=False)
    kind: Literal["ui_handoff"] = field(default="ui_handoff", init=False)

    @property
    def params(self) -> Mapping[str, object]:
        return _frozen_configuration(self._params_json)

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "route": self.route,
            "params": dict(self.params),
        }


BindingRequirement: TypeAlias = (
    InvocationBindingRequirement | ExternalExecutionBindingRequirement | UiHandoffBindingRequirement
)


@dataclass(frozen=True, slots=True)
class RegistrySnapshotEntry:
    """One compiled Definition and its binding availability within a Snapshot."""

    agent_id: str
    revision: int
    priority: int
    handling_kind: AgentHandlingKind
    enabled: bool
    binding_requirement: BindingRequirement | None
    binding_status: BindingStatus
    isolation_reason_code: str | None
    _definition_json: str = field(repr=False)

    @property
    def definition(self) -> AgentDefinitionV2:
        """Return a fresh v2 Definition copy so callers cannot mutate this Snapshot."""

        return AgentDefinitionV2.model_validate_json(self._definition_json)

    @property
    def is_executable(self) -> bool:
        return self.enabled and self.binding_status == "ready"


@dataclass(frozen=True, slots=True)
class RegistryQuarantineEntry:
    """Safe reload diagnostic for a raw Definition excluded from a Snapshot."""

    source_index: int
    reason_code: str
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class RegistrySnapshotRuntimeStatus:
    """Safe aggregate state for readiness and repair-oriented inventory."""

    status: RegistrySnapshotRuntimeState
    reason_code: str | None = None
    isolated_definition_count: int = 0
    quarantined_definition_count: int = 0
    unhealthy_adapter_definition_count: int = 0


@dataclass(frozen=True, slots=True)
class RegistrySnapshotInventoryEntry:
    """Redacted Definition state suitable for an administrator-only inventory."""

    agent_id: str
    revision: int
    enabled: bool
    handling_kind: AgentHandlingKind
    handling: Mapping[str, object]
    binding_status: BindingStatus
    isolation_reason_code: str | None


@dataclass(frozen=True, slots=True)
class RegistrySnapshotSelection:
    """An exact Definition selected from one immutable, trusted Snapshot."""

    snapshot_id: str
    entry: RegistrySnapshotEntry

    @property
    def definition(self) -> AgentDefinitionV2:
        return self.entry.definition

    @property
    def binding_requirement(self) -> BindingRequirement:
        requirement = self.entry.binding_requirement
        if requirement is None:  # pragma: no cover - select_for_user excludes this state
            raise RuntimeError("Selected Registry Definition has no Binding Requirement")
        return requirement


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """Immutable, validated Definitions that callers can query without source reads."""

    snapshot_id: str
    source: str
    quarantined: tuple[RegistryQuarantineEntry, ...]
    _entries: Mapping[str, RegistrySnapshotEntry] = field(repr=False)

    @property
    def entries(self) -> Mapping[str, RegistrySnapshotEntry]:
        return self._entries

    def public_definitions(self, user: UserContext) -> tuple[AgentPublicV2, ...]:
        return tuple(entry.definition.to_public() for entry in self._visible_entries(user))

    def candidates_for_user(self, user: UserContext) -> tuple[CandidateAgentV2, ...]:
        return tuple(entry.definition.to_candidate() for entry in self._visible_entries(user))

    def selections_for_user(self, user: UserContext) -> tuple[RegistrySnapshotSelection, ...]:
        """Select all executable Definitions once for a trusted request."""

        return tuple(
            RegistrySnapshotSelection(snapshot_id=self.snapshot_id, entry=entry)
            for entry in self._visible_entries(user)
        )

    def select_for_user(
        self,
        agent_id: str,
        user: UserContext,
    ) -> RegistrySnapshotSelection | None:
        return next(
            (
                RegistrySnapshotSelection(snapshot_id=self.snapshot_id, entry=entry)
                for entry in self._visible_entries(user)
                if entry.agent_id == agent_id
            ),
            None,
        )

    def preflight_for_user(
        self,
        agent_id: str,
        user: UserContext,
    ) -> RegistrySnapshotSelection | None:
        """Return an authorized enabled target before its Handling is resolved.

        Candidate selection remains limited to executable entries.  Direct Invoke
        needs this narrower trusted lookup so it can distinguish an unavailable
        Binding (503) from an invisible or disabled Definition (404), without
        ever consulting the mutable Registry Source again.
        """

        entry = self._entries.get(agent_id)
        if entry is None or not entry.enabled:
            return None
        if not entry.definition.access_policy.allows(user):
            return None
        return RegistrySnapshotSelection(snapshot_id=self.snapshot_id, entry=entry)

    def entry_for(self, agent_id: str) -> RegistrySnapshotEntry | None:
        """Return a compiled entry for internal diagnostics without consulting a source."""

        return self._entries.get(agent_id)

    def admin_inventory(self) -> tuple[RegistrySnapshotInventoryEntry, ...]:
        """Project binding diagnostics without retaining Binding internals or secrets."""

        return tuple(
            RegistrySnapshotInventoryEntry(
                agent_id=entry.agent_id,
                revision=entry.revision,
                enabled=entry.enabled,
                handling_kind=entry.handling_kind,
                handling=MappingProxyType(dict(entry.definition.to_admin().handling)),
                binding_status=entry.binding_status,
                isolation_reason_code=_safe_isolation_reason_code(entry.isolation_reason_code),
            )
            for entry in sorted(self._entries.values(), key=lambda entry: entry.agent_id)
        )

    def admin_quarantine_inventory(self) -> tuple[RegistryQuarantineEntry, ...]:
        """Expose only safe locators and reason codes for rejected source rows."""

        return tuple(
            RegistryQuarantineEntry(
                source_index=entry.source_index,
                agent_id=_safe_quarantine_agent_id(entry.agent_id),
                reason_code=_safe_quarantine_reason_code(entry.reason_code),
            )
            for entry in self.quarantined
        )

    def _executable_entries(self) -> tuple[RegistrySnapshotEntry, ...]:
        return tuple(
            sorted(
                (entry for entry in self._entries.values() if entry.is_executable),
                key=lambda entry: (-entry.priority, entry.agent_id),
            )
        )

    def _visible_entries(self, user: UserContext) -> tuple[RegistrySnapshotEntry, ...]:
        return tuple(
            entry
            for entry in self._executable_entries()
            if entry.definition.access_policy.allows(user)
        )


class RegistrySnapshotRuntime:
    """Own the active immutable Snapshot and request-scoped selections for one process.

    Source implementations build a complete candidate before calling ``load``.  The
    reference swap is therefore atomic: a malformed source-level load keeps the
    previous Snapshot available, while individual bad Definition rows are already
    quarantined by ``RegistrySnapshotBuilder``.
    """

    def __init__(self, builder: RegistrySnapshotBuilder) -> None:
        self._builder = builder
        self._base_snapshot: RegistrySnapshot | None = None
        self._snapshot: RegistrySnapshot | None = None
        self._unhealthy_adapter_keys: frozenset[str] = frozenset()
        self._reload_failed = False
        self._status = RegistrySnapshotRuntimeStatus(status="not_loaded")

    @property
    def snapshot(self) -> RegistrySnapshot | None:
        return self._snapshot

    @property
    def status(self) -> RegistrySnapshotRuntimeStatus:
        return self._status

    def load(
        self,
        raw_definitions: Sequence[object],
        *,
        source: str,
    ) -> RegistrySnapshot:
        try:
            candidate = self._builder.build(raw_definitions, source=source)
        except Exception:
            self._reload_failed = True
            self._refresh_status()
            raise
        self._base_snapshot = candidate
        self._reload_failed = False
        self._snapshot = _apply_runtime_health(candidate, self._unhealthy_adapter_keys)
        self._refresh_status()
        return self._snapshot

    def apply_adapter_health(self, unhealthy_adapter_keys: Collection[str]) -> None:
        """Atomically overlay runtime Adapter health onto the active Snapshot.

        The base Snapshot remains unchanged so a later healthy probe restores the
        original validated binding without a source reread.
        """

        self._unhealthy_adapter_keys = frozenset(unhealthy_adapter_keys)
        if self._base_snapshot is not None:
            self._snapshot = _apply_runtime_health(
                self._base_snapshot,
                self._unhealthy_adapter_keys,
            )
        self._refresh_status()

    def mark_reload_failed(self) -> None:
        """Retain the current Snapshot after a source adapter fails before ``load``.

        A host-owned source mapper can reject malformed legacy data before this
        Runtime receives raw v2 Definitions.  It must get the same last-known-
        good semantics as a failed builder reload, without retaining its raw
        exception as state or inventory data.
        """

        self._reload_failed = True
        self._refresh_status()

    def validate_definition_for_write(self, raw_definition: object) -> AgentDefinitionV2:
        """Validate a v2 Definition before a source persists it."""

        return self._builder.validate_definition_for_write(raw_definition)

    def admin_inventory(self) -> tuple[RegistrySnapshotInventoryEntry, ...]:
        snapshot = self._snapshot
        return snapshot.admin_inventory() if snapshot is not None else ()

    def admin_quarantine_inventory(self) -> tuple[RegistryQuarantineEntry, ...]:
        snapshot = self._snapshot
        return snapshot.admin_quarantine_inventory() if snapshot is not None else ()

    def public_definitions(self, user: UserContext) -> tuple[AgentPublicV2, ...]:
        return self._require_snapshot().public_definitions(user)

    def candidates_for_user(self, user: UserContext) -> tuple[CandidateAgentV2, ...]:
        return self._require_snapshot().candidates_for_user(user)

    def selections_for_user(self, user: UserContext) -> tuple[RegistrySnapshotSelection, ...]:
        return self._require_snapshot().selections_for_user(user)

    def select_for_user(
        self,
        agent_id: str,
        user: UserContext,
    ) -> RegistrySnapshotSelection | None:
        """Select once; downstream callers retain the returned exact Snapshot entry."""

        return self._require_snapshot().select_for_user(agent_id, user)

    def preflight_for_user(
        self,
        agent_id: str,
        user: UserContext,
    ) -> RegistrySnapshotSelection | None:
        return self._require_snapshot().preflight_for_user(agent_id, user)

    def _require_snapshot(self) -> RegistrySnapshot:
        snapshot = self._snapshot
        if snapshot is None:
            raise RegistryError("Registry Snapshot is unavailable")
        return snapshot

    def _refresh_status(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            self._status = RegistrySnapshotRuntimeStatus(
                status="error" if self._reload_failed else "not_loaded",
                reason_code="registry_snapshot_reload_failed" if self._reload_failed else None,
            )
            return
        isolated_count = sum(
            entry.binding_status == "isolated" for entry in snapshot.entries.values()
        )
        unhealthy_adapter_count = sum(
            entry.isolation_reason_code == "invocation_adapter_unhealthy"
            for entry in snapshot.entries.values()
        )
        quarantined_count = len(snapshot.quarantined)
        if self._reload_failed:
            reason_code = "registry_snapshot_reload_failed"
        elif any(
            entry.isolation_reason_code == "invocation_adapter_unhealthy"
            for entry in snapshot.entries.values()
        ):
            reason_code = "runtime_adapter_unhealthy"
        elif isolated_count:
            reason_code = "registry_snapshot_definitions_isolated"
        elif quarantined_count:
            reason_code = "registry_snapshot_definitions_quarantined"
        else:
            reason_code = None
        self._status = RegistrySnapshotRuntimeStatus(
            status="degraded" if reason_code is not None else "ready",
            reason_code=reason_code,
            isolated_definition_count=isolated_count,
            quarantined_definition_count=quarantined_count,
            unhealthy_adapter_definition_count=unhealthy_adapter_count,
        )


class RegistrySnapshotBuilder:
    """Build a complete v2 Snapshot or safely isolate individual bad Definitions."""

    def __init__(
        self,
        runtime_catalog: RuntimeCatalog | None,
        *,
        supported_executor_refs: Collection[str] = (),
        external_executor: ExternalExecutorApplicationPort | None = None,
    ) -> None:
        self._runtime_catalog = runtime_catalog
        self._supported_executor_refs = frozenset(supported_executor_refs)
        self._external_executor = external_executor

    def validate_definition_for_write(self, raw_definition: object) -> AgentDefinitionV2:
        """Reject a Definition before persistence when schema or deployment binding is invalid."""

        definition = self._validate_definition(raw_definition)
        if not definition.enabled:
            return definition
        _requirement, reason_code = self._compile_binding_requirement(definition)
        if reason_code is not None:
            raise RegistryDefinitionValidationError(reason_code)
        return definition

    def build(
        self,
        raw_definitions: Sequence[object],
        *,
        source: str,
    ) -> RegistrySnapshot:
        """Compile one trusted source into a new immutable Snapshot.

        Schema-invalid rows are quarantined. Definitions with a valid v2 contract but
        no deployment binding remain visible to diagnostics as isolated entries and
        never enter the executable Candidate Set.
        """

        if not isinstance(source, str) or not source:
            raise ValueError("Registry Snapshot source must be a non-empty string")

        entries: dict[str, RegistrySnapshotEntry] = {}
        quarantined: list[RegistryQuarantineEntry] = []
        for source_index, raw_definition in enumerate(raw_definitions):
            if isinstance(raw_definition, RegistrySnapshotQuarantineInput):
                quarantined.append(
                    RegistryQuarantineEntry(
                        source_index=source_index,
                        agent_id=_safe_quarantine_agent_id(raw_definition.agent_id),
                        reason_code=_safe_quarantine_reason_code(raw_definition.reason_code),
                    )
                )
                continue
            try:
                definition = self._validate_definition(raw_definition)
            except RegistryDefinitionValidationError as exc:
                quarantined.append(
                    RegistryQuarantineEntry(
                        source_index=source_index,
                        agent_id=_safe_quarantine_agent_id(
                            _raw_definition_agent_id(raw_definition)
                        ),
                        reason_code=exc.reason_code,
                    )
                )
                continue

            if definition.agent_id in entries:
                quarantined.append(
                    RegistryQuarantineEntry(
                        source_index=source_index,
                        agent_id=definition.agent_id,
                        reason_code="duplicate_agent_id",
                    )
                )
                continue

            try:
                requirement, isolation_reason_code = self._compile_binding_requirement(definition)
            except ValueError:
                quarantined.append(
                    RegistryQuarantineEntry(
                        source_index=source_index,
                        agent_id=definition.agent_id,
                        reason_code="definition_configuration_invalid",
                    )
                )
                continue

            binding_status: BindingStatus
            if not definition.enabled:
                binding_status = "disabled"
                requirement = None
                isolation_reason_code = None
            elif isolation_reason_code is None:
                binding_status = "ready"
            else:
                binding_status = "isolated"
            entries[definition.agent_id] = RegistrySnapshotEntry(
                agent_id=definition.agent_id,
                revision=definition.revision,
                priority=definition.priority,
                handling_kind=definition.handling.kind,
                enabled=definition.enabled,
                binding_requirement=requirement,
                binding_status=binding_status,
                isolation_reason_code=isolation_reason_code,
                _definition_json=_canonical_json(definition.model_dump(mode="json")),
            )

        frozen_entries = MappingProxyType(dict(entries))
        frozen_quarantined = tuple(quarantined)
        return RegistrySnapshot(
            snapshot_id=_snapshot_identity(source, frozen_entries, frozen_quarantined),
            source=source,
            quarantined=frozen_quarantined,
            _entries=frozen_entries,
        )

    @staticmethod
    def _validate_definition(raw_definition: object) -> AgentDefinitionV2:
        try:
            payload = (
                raw_definition.model_dump(mode="json", warnings=False)
                if isinstance(raw_definition, AgentDefinitionV2)
                else raw_definition
            )
            definition = AgentDefinitionV2.model_validate(payload)
            if _safe_quarantine_agent_id(definition.agent_id) is None:
                raise RegistryDefinitionValidationError("definition_schema_invalid")
            return definition
        except RegistryDefinitionValidationError:
            raise
        except (TypeError, ValidationError, ValueError) as exc:
            raise RegistryDefinitionValidationError("definition_schema_invalid") from exc

    def _compile_binding_requirement(
        self,
        definition: AgentDefinitionV2,
    ) -> tuple[BindingRequirement, str | None]:
        handling = definition.handling
        if isinstance(handling, InvocationHandling):
            requirement = InvocationBindingRequirement(
                adapter_key=handling.adapter_key,
                connector_ref=handling.connector_ref,
                _config_json=_canonical_json(
                    handling.config.model_dump(mode="json", exclude_none=True)
                ),
            )
            return requirement, self._invocation_isolation_reason(requirement)
        if isinstance(handling, ExternalExecutionHandling):
            requirement = ExternalExecutionBindingRequirement(
                executor_ref=handling.executor_ref,
                _params_json=_canonical_json(
                    handling.params.model_dump(mode="json", exclude_none=True)
                ),
            )
            reason_code = (
                None
                if self._supports_external_executor(requirement.executor_ref)
                else "external_executor_unsupported"
            )
            return requirement, reason_code
        if isinstance(handling, UiHandoffHandling):
            return (
                UiHandoffBindingRequirement(
                    route=handling.route,
                    _params_json=_canonical_json(
                        handling.params.model_dump(mode="json", exclude_none=True)
                    ),
                ),
                None,
            )
        raise ValueError("Unknown Agent Handling")

    def _supports_external_executor(self, executor_ref: str) -> bool:
        external_executor = self._external_executor
        if external_executor is None:
            return executor_ref in self._supported_executor_refs
        try:
            return bool(external_executor.supports(executor_ref))
        except Exception:
            return False

    def _invocation_isolation_reason(
        self,
        requirement: InvocationBindingRequirement,
    ) -> str | None:
        catalog = self._runtime_catalog
        if catalog is None or not catalog.has(requirement.adapter_key):
            return "invocation_adapter_missing"
        descriptor = catalog.descriptor(requirement.adapter_key)
        if not descriptor.capability.invocation:
            return "invocation_adapter_unsupported"
        if not descriptor.capability.v2_invocation:
            return "invocation_adapter_incompatible"
        validator = Draft202012Validator(dict(descriptor.config_schema))
        if next(validator.iter_errors(dict(requirement.config)), None) is not None:
            return "invocation_config_invalid"
        return None


def _frozen_configuration(encoded: str) -> Mapping[str, object]:
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - encoded only by _canonical_json
        raise ValueError("Binding configuration must be an object")
    return MappingProxyType(decoded)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _safe_isolation_reason_code(reason_code: object) -> str | None:
    if reason_code is None:
        return None
    if isinstance(reason_code, str) and reason_code in _SAFE_ISOLATION_REASON_CODES:
        return reason_code
    return "binding_unavailable"


def _safe_quarantine_agent_id(agent_id: object) -> str | None:
    return agent_id if is_safe_agent_identifier(agent_id) else None


def _raw_definition_agent_id(raw_definition: object) -> object:
    if isinstance(raw_definition, AgentDefinitionV2):
        return raw_definition.agent_id
    if isinstance(raw_definition, Mapping):
        return raw_definition.get("agent_id")
    return None


def _safe_quarantine_reason_code(reason_code: object) -> str:
    if isinstance(reason_code, str) and reason_code in _SAFE_QUARANTINE_REASON_CODES:
        return reason_code
    return "definition_schema_invalid"


def _apply_runtime_health(
    snapshot: RegistrySnapshot,
    unhealthy_adapter_keys: Collection[str],
) -> RegistrySnapshot:
    """Create an immutable health overlay without mutating the last good source view."""

    unhealthy = frozenset(unhealthy_adapter_keys)
    entries: dict[str, RegistrySnapshotEntry] = {}
    changed = False
    for entry in snapshot.entries.values():
        requirement = entry.binding_requirement
        if (
            entry.binding_status == "ready"
            and isinstance(requirement, InvocationBindingRequirement)
            and requirement.adapter_key in unhealthy
        ):
            entries[entry.agent_id] = replace(
                entry,
                binding_status="isolated",
                isolation_reason_code="invocation_adapter_unhealthy",
            )
            changed = True
        else:
            entries[entry.agent_id] = entry
    if not changed:
        return snapshot
    frozen_entries = MappingProxyType(entries)
    return RegistrySnapshot(
        snapshot_id=_snapshot_identity(snapshot.source, frozen_entries, snapshot.quarantined),
        source=snapshot.source,
        quarantined=snapshot.quarantined,
        _entries=frozen_entries,
    )


def _snapshot_identity(
    source: str,
    entries: Mapping[str, RegistrySnapshotEntry],
    quarantined: tuple[RegistryQuarantineEntry, ...],
) -> str:
    payload = {
        "source": source,
        "entries": [
            {
                "agent_id": entry.agent_id,
                "revision": entry.revision,
                "priority": entry.priority,
                "handling_kind": entry.handling_kind,
                "enabled": entry.enabled,
                "binding_status": entry.binding_status,
                "isolation_reason_code": entry.isolation_reason_code,
                "definition": json.loads(entry._definition_json),
                "binding_requirement": (
                    entry.binding_requirement.to_payload()
                    if entry.binding_requirement is not None
                    else None
                ),
            }
            for entry in sorted(entries.values(), key=lambda entry: entry.agent_id)
        ],
        "quarantined": [
            {
                "source_index": entry.source_index,
                "agent_id": entry.agent_id,
                "reason_code": entry.reason_code,
            }
            for entry in quarantined
        ],
    }
    digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
    return f"registry-snapshot-v1:{digest}"
