import json
from pathlib import Path

DATASET = Path("tests/contract/oac_irs/golden/route/v2/permission-dataset.json")
REQUIRED_BLOCKING = {
    "permission_expansion",
    "cross_edition_agent_exposure",
    "authorized_positive_route_failure",
}
REQUIRED_FAILURE_STATUSES = {401, 403, 422}
REQUIRED_FORBIDDEN_SURFACES = {
    "trigger",
    "evidence",
    "llm",
    "continue_agent",
    "plan_step",
}


def validate(path: Path = DATASET) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != "oac-agent-entitlement-replay/v1":
        raise ValueError("unexpected entitlement replay version")
    if set(payload.get("blocking_diffs", [])) != REQUIRED_BLOCKING:
        raise ValueError("blocking diff policy is incomplete")
    cases = payload.get("cases", [])
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)) or not all(ids):
        raise ValueError("case IDs must be unique and non-empty")
    roles = {case.get("role") for case in cases}
    if not {"admin", "operator", "user"} <= roles:
        raise ValueError("role matrix is incomplete")
    failure_statuses = {case.get("expected_status") for case in cases}
    if not REQUIRED_FAILURE_STATUSES <= failure_statuses:
        raise ValueError("failure status matrix is incomplete")
    surfaces = {surface for case in cases for surface in case.get("forbidden_surfaces", [])}
    if surfaces != REQUIRED_FORBIDDEN_SURFACES:
        raise ValueError("router authorization surfaces are incomplete")
    positive = [case for case in cases if case.get("expected_route")]
    if not positive or any(case.get("expected_status") != 200 for case in positive):
        raise ValueError("positive route gates are incomplete")
    return payload


if __name__ == "__main__":
    result = validate()
    print(f"validated {len(result['cases'])} OAC entitlement replay cases")
