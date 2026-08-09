from dataclasses import FrozenInstanceError

import pytest

from app.core.config import Settings
from app.core.memory_runtime import (
    MEMORY_RUNTIME_POLICY_VERSION,
    RETIRED_MEMORY_BEHAVIOR_VARIABLES,
    build_memory_runtime_policy,
    reject_retired_memory_behavior_variables,
)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (
            "off",
            {
                "recall": False,
                "formation": "off",
                "formation_workers": False,
                "maintenance": True,
                "context": False,
                "scopes": (),
            },
        ),
        (
            "observe",
            {
                "recall": False,
                "formation": "observe",
                "formation_workers": True,
                "maintenance": True,
                "context": False,
                "scopes": (),
            },
        ),
        (
            "on",
            {
                "recall": True,
                "formation": "enforced",
                "formation_workers": True,
                "maintenance": True,
                "context": True,
                "scopes": ("user_preference", "stable_fact"),
            },
        ),
    ],
)
def test_memory_mode_has_one_deterministic_policy(mode, expected) -> None:
    policy = build_memory_runtime_policy(mode)

    assert policy.version == MEMORY_RUNTIME_POLICY_VERSION
    assert policy.config_source == "MEMORY_MODE"
    assert policy.recall_enabled is expected["recall"]
    assert policy.formation_mode == expected["formation"]
    assert policy.turn_outbox_consumer_enabled is True
    assert policy.formation_worker_enabled is expected["formation_workers"]
    assert policy.formation_sweeper_enabled is expected["formation_workers"]
    assert policy.index_worker_enabled is expected["maintenance"]
    assert policy.ttl_sweeper_enabled is expected["maintenance"]
    assert policy.consolidation_enabled is False
    assert policy.governed_context_memory_enabled is expected["context"]
    assert policy.route_memory_scopes == expected["scopes"]


def test_memory_runtime_policy_is_immutable() -> None:
    policy = build_memory_runtime_policy("on")
    with pytest.raises(FrozenInstanceError):
        policy.recall_enabled = False  # type: ignore[misc]


def test_decision_shadow_disables_all_business_and_provider_side_effects() -> None:
    policy = build_memory_runtime_policy("on", execution_plane="decision_shadow")

    assert policy.memory_enabled is False
    assert policy.effective_recall_enabled is False
    assert policy.effective_formation_mode == "off"
    assert policy.effective_formation_worker_enabled is False
    assert policy.effective_formation_sweeper_enabled is False
    assert policy.effective_governed_context_memory_enabled is False
    assert policy.effective_index_worker_enabled is False
    assert policy.effective_ttl_sweeper_enabled is False


def test_invalid_memory_mode_fails_without_echoing_value() -> None:
    secret_value = "invalid-secret-mode"
    with pytest.raises(ValueError, match="off, observe, on") as error:
        build_memory_runtime_policy(secret_value)
    assert secret_value not in str(error.value)


@pytest.mark.parametrize("name", sorted(RETIRED_MEMORY_BEHAVIOR_VARIABLES))
@pytest.mark.parametrize("value", ["", "secret-value-that-must-not-leak"])
def test_retired_memory_environment_variables_fail_fast_without_value(name, value) -> None:
    with pytest.raises(ValueError) as error:
        reject_retired_memory_behavior_variables({name: value, "DATABASE_URL": "secret-db"})

    message = str(error.value)
    assert name in message
    assert "MEMORY_MODE" in message
    assert value not in message if value else True
    assert "secret-db" not in message


def test_settings_accepts_only_memory_mode_as_behavior_field(monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_MODE", "observe")
    settings = Settings(_env_file=None)
    assert settings.memory_mode == "observe"
    for field in RETIRED_MEMORY_BEHAVIOR_VARIABLES:
        assert field.lower() not in type(settings).model_fields


def test_settings_rejects_retired_variable_from_dotenv(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MEMORY_MODE=on\nMEMORY_RECALL_ENABLED=do-not-log-this\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        Settings(_env_file=env_file)

    assert "MEMORY_RECALL_ENABLED" in str(error.value)
    assert "MEMORY_MODE" in str(error.value)
    assert "do-not-log-this" not in str(error.value)


@pytest.mark.parametrize("field", ["memory_recall_enabled", "MEMORY_RECALL_ENABLED"])
def test_settings_rejects_direct_low_level_behavior_override(field) -> None:
    with pytest.raises(ValueError, match="MEMORY_RECALL_ENABLED"):
        Settings(_env_file=None, **{field: False})
