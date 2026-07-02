from app.core.errors import ErrorPayload


def test_error_payload_details_are_json_safe() -> None:
    payload = ErrorPayload(
        code="llm_error",
        message="bad model output",
        details={"errors": [{"ctx": {"error": ValueError("plan is required")}}]},
    ).to_dict()

    assert payload["details"]["errors"][0]["ctx"]["error"] == "plan is required"
