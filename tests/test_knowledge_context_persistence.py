from app.core.config import Settings
from app.repositories.memory import MemoryRunRepository
from app.schemas.events import AgentEvent
from app.schemas.logs import AgentResult, AgentRun, RouteLog
from app.services.memory_formation import TurnCapsuleBuilder


def _knowledge_context() -> dict:
    return {
        "summary": "CONFIDENTIAL KNOWLEDGE SUMMARY",
        "items": [
            {
                "item_id": "item-1",
                "source_id": "source-1",
                "content": "CONFIDENTIAL KNOWLEDGE BODY",
                "score": 0.93,
                "title": "Confidential title",
                "uri": "https://internal.example/secret",
                "citation": {
                    "source_id": "source-1",
                    "chunk_id": "chunk-1",
                    "title": "Confidential title",
                    "uri": "https://internal.example/secret",
                    "metadata": {"secret": "citation metadata"},
                },
                "metadata": {"secret": "item metadata"},
            }
        ],
        "citations": [
            {
                "source_id": "source-1",
                "chunk_id": "chunk-1",
                "title": "Confidential title",
                "uri": "https://internal.example/secret",
                "metadata": {"secret": "citation metadata"},
            }
        ],
        "source_ids": ["source-1"],
        "status": "ok",
        "truncated": False,
        "errors": [],
        "metadata": {
            "trace_id": "trace-1",
            "secret": "result metadata",
        },
    }


def _assert_reference_only(value: dict) -> None:
    assert value == {
        "status": "ok",
        "trace_id": "trace-1",
        "item_count": 1,
        "items": [{"item_id": "item-1", "source_id": "source-1"}],
        "citations": [{"source_id": "source-1"}],
        "source_ids": ["source-1"],
        "truncated": False,
        "errors": [],
    }


def test_agent_run_persists_only_knowledge_references() -> None:
    run = AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        status="running",
        invoker_type="local",
        input={
            "text": "ordinary user input",
            "knowledge_context": _knowledge_context(),
        },
    )

    assert run.input["text"] == "ordinary user input"
    _assert_reference_only(run.input["knowledge_context"])
    assert "CONFIDENTIAL" not in run.model_dump_json()
    assert "internal.example" not in run.model_dump_json()
    assert "secret" not in run.model_dump_json()


def test_persistence_projection_drops_unbounded_error_and_reference_text() -> None:
    context = _knowledge_context()
    context["errors"] = [
        "knowledge_unavailable",
        "CONFIDENTIAL provider response must not be persisted",
    ]
    context["source_ids"].append("CONFIDENTIAL source body with spaces")

    run = AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        status="running",
        invoker_type="local",
        input={"knowledge_context": context},
    )

    persisted = run.input["knowledge_context"]
    assert persisted["errors"] == ["knowledge_unavailable"]
    assert persisted["source_ids"] == ["source-1"]
    assert "CONFIDENTIAL" not in run.model_dump_json()


def test_route_log_recursively_persists_only_knowledge_references() -> None:
    route_log = RouteLog(
        request_id="request-1",
        session_id="session-1",
        model_name="router",
        evidence=[],
        raw_output={"knowledge_context": _knowledge_context()},
        parsed_output={
            "invocation": {
                "input": {
                    "text": "ordinary user input",
                    "knowledge_context": _knowledge_context(),
                }
            }
        },
    )

    assert route_log.raw_output is not None
    _assert_reference_only(route_log.raw_output["knowledge_context"])
    assert route_log.parsed_output is not None
    _assert_reference_only(route_log.parsed_output["invocation"]["input"]["knowledge_context"])
    assert "CONFIDENTIAL" not in route_log.model_dump_json()
    assert "internal.example" not in route_log.model_dump_json()
    assert "secret" not in route_log.model_dump_json()


async def test_repository_and_event_boundaries_revalidate_knowledge_references() -> None:
    safe_run = AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        status="running",
        invoker_type="local",
    )
    unvalidated_copy = safe_run.model_copy(
        update={"input": {"knowledge_context": _knowledge_context()}}
    )

    stored = await MemoryRunRepository().add_run(unvalidated_copy)
    _assert_reference_only(stored.input["knowledge_context"])

    event = AgentEvent(
        event_id="event-1",
        run_id=stored.run_id,
        session_id=stored.session_id,
        agent_id=stored.agent_id,
        event_type="agent_progress",
        payload={"nested": {"knowledge_context": _knowledge_context()}},
    )
    _assert_reference_only(event.payload["nested"]["knowledge_context"])
    assert "CONFIDENTIAL" not in event.model_dump_json()


def test_knowledge_context_is_not_memory_formation_evidence() -> None:
    run = AgentRun(
        run_id="run-1",
        request_id="request-1",
        session_id="session-1",
        agent_id="agent-1",
        user_id="user-1",
        tenant_id="tenant-1",
        status="completed",
        invoker_type="local",
        input={
            "text": "ordinary user input",
            "knowledge_context": _knowledge_context(),
        },
    )
    result = AgentResult(
        result_id="result-1",
        run_id=run.run_id,
        session_id=run.session_id,
        agent_id=run.agent_id,
        user_id=run.user_id,
        tenant_id=run.tenant_id,
        status="completed",
        message="ordinary assistant response",
    )

    turn = TurnCapsuleBuilder(Settings(storage_backend="memory")).build_from_records(
        run=run,
        result=result,
    )

    assert turn.user_text == "ordinary user input"
    assert "CONFIDENTIAL" not in turn.model_dump_json()
    assert "source-1" not in turn.model_dump_json()
