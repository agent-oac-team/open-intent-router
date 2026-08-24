"""Freeze and revalidate the safe v2 Binding contract of delayed Plan Steps."""

from pydantic import TypeAdapter

from app.core.errors import AgentUnavailableError, PlanBindingUnavailableError
from app.schemas.agents import AgentHandling
from app.schemas.common import UserContext
from app.schemas.plans import PlanStep
from app.services.registry_snapshot import (
    RegistrySnapshot,
    RegistrySnapshotRuntime,
    RegistrySnapshotSelection,
)

_HANDLING_ADAPTER = TypeAdapter(AgentHandling)


def freeze_plan_step_binding(
    step: PlanStep,
    selection: RegistrySnapshotSelection,
) -> PlanStep:
    """Persist the exact selected revision and declarative requirement for a Step."""

    definition = selection.definition
    if definition.agent_id != step.agent_id:
        raise PlanBindingUnavailableError(
            "Plan Binding does not match its Agent",
            details={"reason_code": "plan_binding_agent_mismatch"},
        )
    requirement = _HANDLING_ADAPTER.validate_python(selection.binding_requirement.to_payload())
    return step.model_copy(
        update={
            "agent_revision": definition.revision,
            "binding_requirement": requirement,
        }
    )


def revalidate_plan_step_binding(
    step: PlanStep,
    *,
    snapshot_runtime: RegistrySnapshotRuntime,
    user: UserContext,
) -> RegistrySnapshotSelection:
    """Select a current Binding for a delayed Step and require frozen compatibility.

    Authorization and availability are deliberately evaluated from the current
    immutable Registry Snapshot.  The persisted Step contributes only its
    declaration, never a historical entitlement result or runtime object.
    """

    return revalidate_plan_step_binding_against_snapshot(
        step,
        snapshot=snapshot_runtime.snapshot,
        user=user,
    )


def revalidate_plan_step_binding_against_snapshot(
    step: PlanStep,
    *,
    snapshot: RegistrySnapshot | None,
    user: UserContext,
) -> RegistrySnapshotSelection:
    """Validate a Step against one captured immutable Snapshot reference."""

    if snapshot is None:
        raise PlanBindingUnavailableError(
            "Registry Snapshot is unavailable for Plan execution",
            details={"reason_code": "plan_snapshot_unavailable"},
        )
    selection = snapshot.select_for_user(step.agent_id, user)
    if selection is None:
        preflight = snapshot.preflight_for_user(step.agent_id, user)
        if preflight is not None:
            raise PlanBindingUnavailableError(
                "Plan Binding is no longer available",
                details={
                    "reason_code": preflight.entry.isolation_reason_code
                    or "plan_binding_unavailable"
                },
            )
        raise AgentUnavailableError(
            f"Agent is not available: {step.agent_id}",
            details={"agent_ids": [step.agent_id]},
        )
    validate_frozen_plan_step_binding(step, selection)
    return selection


def validate_frozen_plan_step_binding(
    step: PlanStep,
    selection: RegistrySnapshotSelection,
) -> None:
    """Reject a changed v2 Definition or Binding Requirement before execution."""

    if step.agent_revision is None and step.binding_requirement is None:
        raise PlanBindingUnavailableError(
            "Plan Binding is unavailable",
            details={"reason_code": "legacy_plan_step_unsupported"},
        )
    if step.agent_revision is None or step.binding_requirement is None:
        raise PlanBindingUnavailableError(
            "Plan Binding is incomplete",
            details={"reason_code": "plan_binding_incomplete"},
        )
    definition = selection.definition
    if definition.agent_id != step.agent_id:
        raise PlanBindingUnavailableError(
            "Plan Binding does not match its Agent",
            details={"reason_code": "plan_binding_agent_mismatch"},
        )
    if definition.revision != step.agent_revision:
        raise PlanBindingUnavailableError(
            "Plan Agent revision is no longer compatible",
            details={"reason_code": "plan_binding_revision_incompatible"},
        )
    current = _HANDLING_ADAPTER.validate_python(selection.binding_requirement.to_payload())
    if _handling_payload(step.binding_requirement) != _handling_payload(current):
        raise PlanBindingUnavailableError(
            "Plan Binding Requirement is no longer compatible",
            details={"reason_code": "plan_binding_requirement_incompatible"},
        )


def _handling_payload(requirement: AgentHandling) -> dict[str, object]:
    return requirement.model_dump(mode="json", exclude_none=True)
