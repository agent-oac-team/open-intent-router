from pathlib import Path

from host_adapters.oac.security.redaction import redact_sensitive


def test_ticket_token_and_claim_secrets_are_redacted_recursively() -> None:
    payload = {
        "execution_ticket": "opaque-value",
        "nested": {
            "lease_token": "lease-value",
            "message": "execution_ticket=opaque-value request_id=request-1",
        },
        "items": [{"authorization": "Bearer secret"}],
        "run_id": "run-1",
    }

    redacted = redact_sensitive(payload)

    assert redacted["execution_ticket"] == "[REDACTED]"
    assert redacted["nested"]["lease_token"] == "[REDACTED]"
    assert "opaque-value" not in redacted["nested"]["message"]
    assert redacted["items"][0]["authorization"] == "[REDACTED]"
    assert redacted["run_id"] == "run-1"


def test_ticket_service_does_not_log_or_serialize_raw_ticket() -> None:
    source = Path("app/services/execution_ticket_service.py").read_text(encoding="utf-8")
    repository = Path("app/repositories/execution_tickets.py").read_text(encoding="utf-8")

    assert "logger." not in source
    assert "print(" not in source
    assert "ticket_hash=ticket" not in repository
    assert "ticket TEXT" not in repository
