from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import AgentDefinitionModel
from app.main import create_app
from app.repositories.database import DatabaseAgentDefinitionRepository
from app.repositories.file_registry import FileRegistrySource
from app.runtime.catalog import build_default_runtime_descriptors
from app.schemas.common import UserContext
from app.schemas.registry_mutation import RegistryMutationCommand


def _native_definition(agent_id: str = "native-agent") -> dict[str, object]:
    return {
        "schema_version": "oir-agent-v2",
        "agent_id": agent_id,
        "name": "Native Agent",
        "description": "A Native v2 Agent Definition.",
        "access_policy": {"allow_roles": ["operator"], "allow_tenants": ["*"]},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
        "handling": {
            "kind": "invocation",
            "adapter_key": "test_v2_adapter",
            "config": {"function": "execute"},
        },
    }


def test_native_agent_endpoints_accept_only_v2_and_keep_public_handling_safe() -> None:
    settings = Settings(storage_backend="memory", registry_backend="database")
    with TestClient(create_app(settings=settings)) as client:
        legacy_response = client.post(
            "/api/v1/admin/agents",
            json={
                "agent_id": "legacy-agent",
                "name": "Legacy Agent",
                "description": "Must not enter Native Registry.",
                "type": "mock",
                "invocation": {"type": "mock", "config": {}},
            },
        )
        assert legacy_response.status_code == 422

        created = client.post("/api/v1/admin/agents", json=_native_definition())
        assert created.status_code == 200
        assert created.json()["schema_version"] == "oir-agent-v2"

        public = client.get("/api/v1/agents")
        assert public.status_code == 200
        public_agent = public.json()["agents"][0]
        assert public_agent["handling_kind"] == "invocation"
        assert "handling" not in public_agent
        assert not {"type", "invocation", "ui_handoff", "metadata"} & public_agent.keys()

        admin = client.get("/api/v1/admin/agents")
        assert admin.status_code == 200
        admin_agent = admin.json()["agents"][0]
        assert admin_agent["handling"] == {
            "kind": "invocation",
            "adapter_key": "***REDACTED***",
            "config": {"function": "***REDACTED***"},
        }


def test_default_catalog_does_not_activate_retired_native_invokers() -> None:
    assert build_default_runtime_descriptors() == ()


async def test_database_registry_writes_v2_and_clears_retired_columns(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'native-v2.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    repository = DatabaseAgentDefinitionRepository(factory)

    from app.schemas.agents import AgentDefinitionV2

    saved = await repository.upsert(AgentDefinitionV2.model_validate(_native_definition()))
    async with factory() as session:
        row = await session.scalar(
            select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == saved.agent_id)
        )
        assert row is not None
        assert row.schema_version == "oir-agent-v2"
        assert row.type == ""
        assert row.invocation_text == "{}"
        assert row.ui_handoff_text == "{}"
        assert row.metadata_text == "{}"
        assert '"kind":"invocation"' in row.handling_text


async def test_native_enabled_writes_clear_retired_database_columns(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'native-v2-enabled.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    repository = DatabaseAgentDefinitionRepository(factory)
    from app.schemas.agents import AgentDefinitionV2

    saved = await repository.upsert(AgentDefinitionV2.model_validate(_native_definition()))

    async def contaminate() -> None:
        async with factory() as session:
            row = await session.scalar(
                select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == saved.agent_id)
            )
            assert row is not None
            row.type = "http"
            row.invocation_text = '{"endpoint":"https://private.example/token"}'
            row.ui_handoff_text = '{"route":"/legacy"}'
            row.metadata_text = '{"secret":"must-be-cleared"}'
            await session.commit()

    async def assert_retired_columns_cleared() -> None:
        async with factory() as session:
            row = await session.scalar(
                select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == saved.agent_id)
            )
            assert row is not None
            assert (row.type, row.invocation_text, row.ui_handoff_text, row.metadata_text) == (
                "",
                "{}",
                "{}",
                "{}",
            )

    await contaminate()
    disabled = await repository.set_enabled(saved.agent_id, False)
    assert disabled is not None and disabled.enabled is False
    await assert_retired_columns_cleared()

    await contaminate()
    enabled = await repository.mutate(
        RegistryMutationCommand(
            operation="enable",
            agent_id=saved.agent_id,
            actor_id="test-admin",
            source="test",
            expected_revision=disabled.revision,
        )
    )
    assert enabled.after is not None and enabled.after.enabled is True
    await assert_retired_columns_cleared()


def test_default_native_app_builds_a_lifecycle_owned_v2_snapshot(tmp_path) -> None:
    path = tmp_path / "native-v2.yaml"
    path.write_text(
        """agents:
  - schema_version: oir-agent-v2
    agent_id: native-handoff
    name: Native handoff
    description: Lifecycle snapshot test definition.
    access_policy: {allow_roles: [operator]}
    handling: {kind: ui_handoff, route: /native/handoff}
""",
        encoding="utf-8",
    )
    settings = Settings(
        storage_backend="memory",
        registry_backend="file",
        registry_file_path=str(path),
        memory_mode="off",
    )
    app = create_app(settings=settings)

    with TestClient(app):
        snapshot_runtime = app.state.registry_snapshot_runtime
        assert snapshot_runtime is not None and snapshot_runtime.snapshot is not None
        selection = snapshot_runtime.snapshot.select_for_user(
            "native-handoff",
            UserContext(id="operator", roles=["operator"]),
        )

    assert selection is not None


def test_file_registry_rejects_legacy_definition(tmp_path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(
        '{"agents":[{"agent_id":"legacy-agent","name":"Legacy","description":"old",'
        '"type":"mock","invocation":{"type":"mock","config":{}}}]}',
        encoding="utf-8",
    )

    from app.core.errors import RegistryError

    try:
        FileRegistrySource(str(path)).load_sync()
    except RegistryError:
        return
    raise AssertionError("Legacy Definition unexpectedly entered the Native file Registry")
