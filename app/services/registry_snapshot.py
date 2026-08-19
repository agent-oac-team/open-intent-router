"""Compile trusted v2 Agent Definitions into an immutable Registry Snapshot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypeAlias

from jsonschema import Draft202012Validator
from pydantic import ValidationError

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
)
from app.schemas.common import UserContext

BindingStatus = Literal["ready", "isolated", "disabled"]


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
        self._snapshot: RegistrySnapshot | None = None

    @property
    def snapshot(self) -> RegistrySnapshot | None:
        return self._snapshot

    def load(
        self,
        raw_definitions: Sequence[object],
        *,
        source: str,
    ) -> RegistrySnapshot:
        candidate = self._builder.build(raw_definitions, source=source)
        self._snapshot = candidate
        return candidate

    def validate_definition_for_write(self, raw_definition: object) -> AgentDefinitionV2:
        """Validate a v2 Definition before a source persists it."""

        return self._builder.validate_definition_for_write(raw_definition)

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


class RegistrySnapshotBuilder:
    """Build a complete v2 Snapshot or safely isolate individual bad Definitions."""

    def __init__(
        self,
        runtime_catalog: RuntimeCatalog | None,
        *,
        supported_executor_refs: Collection[str] = (),
    ) -> None:
        self._runtime_catalog = runtime_catalog
        self._supported_executor_refs = frozenset(supported_executor_refs)

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
            try:
                definition = self._validate_definition(raw_definition)
            except RegistryDefinitionValidationError as exc:
                quarantined.append(
                    RegistryQuarantineEntry(
                        source_index=source_index,
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
            return AgentDefinitionV2.model_validate(payload)
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
                if requirement.executor_ref in self._supported_executor_refs
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
