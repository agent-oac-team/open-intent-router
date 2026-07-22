#!/usr/bin/env python3
"""Exercise the canonical runtime against an isolated rehearsal database."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

RUNTIME_TABLES = (
    "canonical_turns",
    "agent_runs",
    "agent_results",
    "agent_events",
    "plans",
    "turn_outbox",
    "memory_items",
    "memory_events",
)


async def table_counts(database_url: str) -> dict[str, int]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return {
                table: int(
                    (await connection.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()
                )
                for table in RUNTIME_TABLES
            }
    finally:
        await engine.dispose()


async def run_rehearsal(*, primary_database_url: str, rehearsal_database_url: str) -> dict:
    primary_before = await table_counts(primary_database_url)

    os.environ.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": rehearsal_database_url,
            "STORAGE_BACKEND": "database",
            "REGISTRY_BACKEND": "database",
            "MEMORY_MODE": "on",
            "MEMORY_STRATEGY_PROVIDER": "memory",
            "KNOWLEDGE_VECTOR_BACKEND": "memory",
        }
    )

    from app.core.config import get_settings
    from app.db.session import create_all_tables
    from app.dependencies import (
        get_delegated_run_service,
        get_memory_service,
        get_plan_service,
        get_turn_repository,
        get_turn_service,
    )
    from app.schemas.common import UserContext
    from app.schemas.delegated_runs import (
        DelegatedRunCompleteCommand,
        DelegatedRunProgressCommand,
        DelegatedRunStartCommand,
    )
    from app.schemas.memory import MemoryRecallRequest, MemoryWriteCandidate
    from app.schemas.plans import Plan, PlanStep
    from app.schemas.turns import TurnUserInput

    get_settings.cache_clear()
    settings = get_settings()
    await create_all_tables(settings)
    rehearsal_before = await table_counts(rehearsal_database_url)

    suffix = uuid4().hex
    tenant_id = "oac"
    user_id = f"rehearsal-user-{suffix}"
    session_id = f"rehearsal-session-{suffix}"
    request_id = f"rehearsal-request-{suffix}"

    plan_service = get_plan_service()
    plan = await plan_service.save_plan(
        Plan(
            plan_id=f"rehearsal-plan-{suffix}",
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            status="pending",
            steps=[
                PlanStep(
                    step_id=f"rehearsal-step-{suffix}",
                    agent_id="rehearsal-agent",
                    description="Validate isolated state transitions",
                )
            ],
        ),
        publish=False,
    )
    await plan_service.confirm(plan.plan_id, tenant_id=tenant_id, user_id=user_id, publish=False)
    await plan_service.cancel(plan.plan_id, tenant_id=tenant_id, user_id=user_id, publish=False)

    turn_service = get_turn_service()
    started_turn = await turn_service.start_turn(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        request_id=request_id,
        source="state_rehearsal",
        user_input=TurnUserInput(text="complete the isolated rehearsal"),
    )
    runs = get_delegated_run_service()
    started_run = await runs.start(
        DelegatedRunStartCommand(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            turn_id=started_turn.turn.turn_id,
            agent_id="rehearsal-agent",
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    progressed = await runs.progress(
        DelegatedRunProgressCommand(
            event_id=f"rehearsal-progress-{suffix}",
            run_id=started_run.run.run_id,
            turn_id=started_run.run.turn_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=started_run.run.agent_id,
            expected_state_version=started_run.run.state_version,
            sequence=1,
            status="running",
            payload={"stage": "isolated"},
            occurred_at=datetime.now(UTC),
        )
    )
    completed = await runs.complete(
        DelegatedRunCompleteCommand(
            event_id=f"rehearsal-complete-{suffix}",
            run_id=started_run.run.run_id,
            turn_id=started_run.run.turn_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=started_run.run.agent_id,
            expected_state_version=progressed.run.state_version,
            result_id=f"rehearsal-result-{suffix}",
            response_text="isolated rehearsal completed",
            output={"status": "completed"},
            occurred_at=datetime.now(UTC),
        )
    )
    turn = await get_turn_repository().get(
        started_turn.turn.turn_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    memory_service = get_memory_service()
    memory_decisions = await memory_service.write_candidates(
        candidates=[
            MemoryWriteCandidate(
                scope="stable_fact",
                content=f"rehearsal preference {suffix}",
                source="state_rehearsal",
                subject_id=user_id,
                confidence=0.99,
            )
        ],
        user_id=user_id,
        tenant_id=tenant_id,
    )
    recalled = await memory_service.recall(
        MemoryRecallRequest(
            query=f"rehearsal preference {suffix}",
            user=UserContext(id=user_id, attributes={"tenant_id": tenant_id}),
            subject_id=user_id,
            scopes=["stable_fact"],
            max_items=5,
            metadata_filters={"consumer": "state_rehearsal"},
        )
    )

    primary_after = await table_counts(primary_database_url)
    rehearsal_after = await table_counts(rehearsal_database_url)
    primary_delta = {
        table: primary_after[table] - primary_before[table] for table in RUNTIME_TABLES
    }
    rehearsal_delta = {
        table: rehearsal_after[table] - rehearsal_before[table] for table in RUNTIME_TABLES
    }
    required_rehearsal_tables = {
        "canonical_turns",
        "agent_runs",
        "agent_results",
        "agent_events",
        "plans",
        "turn_outbox",
        "memory_items",
        "memory_events",
    }
    checks = {
        "turn_completed": turn is not None and turn.status.value == "completed",
        "run_completed": completed.run.status.value == "completed",
        "result_created": completed.result_id is not None,
        "plan_lifecycle_recorded": rehearsal_delta["plans"] >= 1,
        "event_progress_and_final_recorded": rehearsal_delta["agent_events"] >= 2,
        "outbox_created": rehearsal_delta["turn_outbox"] >= 1,
        "memory_write_accepted": bool(memory_decisions)
        and memory_decisions[0].status == "accepted",
        "memory_recalled": recalled.context.status == "ok" and bool(recalled.context.items),
        "all_rehearsal_domains_written": all(
            rehearsal_delta[table] >= 1 for table in required_rehearsal_tables
        ),
        "primary_side_effects_zero": all(delta == 0 for delta in primary_delta.values()),
    }
    return {
        "contract": "oir-state-rehearsal/v1",
        "passed": all(checks.values()),
        "tenant_id": tenant_id,
        "checks": checks,
        "primary_side_effect_count": sum(abs(delta) for delta in primary_delta.values()),
        "primary_delta": primary_delta,
        "rehearsal_delta": rehearsal_delta,
        "canonical_ids": {
            "turn_id": started_turn.turn.turn_id,
            "run_id": started_run.run.run_id,
            "result_id": completed.result_id,
            "plan_id": plan.plan_id,
        },
    }


def database_urls(env_file: Path) -> tuple[str, str]:
    values = dotenv_values(env_file)
    primary = values.get("DATABASE_URL")
    rehearsal = values.get("OAC_HOST_STATE_REHEARSAL_DATABASE_URL")
    if not primary or not rehearsal:
        raise ValueError("primary and rehearsal database URLs are required")
    if primary == rehearsal:
        raise ValueError("primary and rehearsal database URLs must differ")
    return primary, rehearsal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    primary, rehearsal = database_urls(args.env_file)
    report = asyncio.run(
        run_rehearsal(primary_database_url=primary, rehearsal_database_url=rehearsal)
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
