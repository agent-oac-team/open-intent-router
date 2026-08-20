"""Shared canonical persistence projection for Plan Steps."""

from app.db.models import PlanStepModel
from app.repositories.json_utils import dumps
from app.schemas.plans import PlanStep


def plan_step_model(*, plan_id: str, step: PlanStep) -> PlanStepModel:
    """Project a validated Step into its canonical database row.

    The frozen v2 Binding fields are deliberately written with the rest of the
    Step so every Plan persistence path preserves the same complete fact.
    """

    return PlanStepModel(
        step_id=step.step_id,
        plan_id=plan_id,
        agent_id=step.agent_id,
        status=step.status,
        description=step.description,
        depends_on_text=dumps(step.depends_on),
        artifact_refs_text=dumps(
            [reference.model_dump(mode="json") for reference in step.artifact_refs]
        ),
        agent_revision=step.agent_revision,
        binding_requirement_text=(
            dumps(step.binding_requirement.model_dump(mode="json", exclude_none=True))
            if step.binding_requirement is not None
            else None
        ),
    )
