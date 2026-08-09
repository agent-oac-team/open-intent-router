#!/usr/bin/env python3
"""Inventory or explicitly terminate IRS runtime state without retaining message bodies."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

ACTIVE_STATUSES = ("pending", "running", "blocked")


async def inventory(connection: AsyncConnection) -> dict:
    tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    required = {"plans", "plan_steps", "session_states"}
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError(f"IRS database is missing required runtime tables: {missing}")
    active_plans = (
        (
            await connection.execute(
                text(
                    "SELECT plan_id, status FROM plans "
                    "WHERE status IN ('pending','running','blocked') ORDER BY plan_id"
                )
            )
        )
        .mappings()
        .all()
    )
    active_steps = (
        (
            await connection.execute(
                text(
                    "SELECT plan_id, step_id, agent_id, status FROM plan_steps "
                    "WHERE status IN ('pending','running','blocked') ORDER BY plan_id, step_id"
                )
            )
        )
        .mappings()
        .all()
    )
    session_count = int(
        (await connection.execute(text("SELECT COUNT(*) FROM session_states"))).scalar_one()
    )
    inflight = [row for row in active_steps if row["status"] in {"running", "blocked"}]
    return {
        "active_plan_count": len(active_plans),
        "inflight_agent_count": len(inflight),
        "pending_callback_count": len(inflight),
        "legacy_session_count": session_count,
        "active_plan_refs": [_ref(row["plan_id"]) for row in active_plans],
        "inflight_agent_refs": [
            {
                "plan": _ref(row["plan_id"]),
                "step": _ref(row["step_id"]),
                "agent": _ref(row["agent_id"]),
                "status": row["status"],
            }
            for row in inflight
        ],
    }


async def terminate(connection: AsyncConnection) -> int:
    steps = await connection.execute(
        text(
            "UPDATE plan_steps SET status = 'failed' "
            "WHERE status IN ('pending','running','blocked')"
        )
    )
    await connection.execute(
        text("UPDATE plans SET status = 'failed' WHERE status IN ('pending','running','blocked')")
    )
    return int(steps.rowcount or 0)


async def run(args: argparse.Namespace) -> dict:
    database_url = args.database_url or os.getenv("IRS_DATABASE_URL")
    if not database_url:
        raise RuntimeError("IRS_DATABASE_URL or --database-url is required")
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            before = await inventory(connection)
        terminated_steps = 0
        if args.mode == "terminate":
            if not args.confirm_terminate_irs_runtime:
                raise RuntimeError("Termination requires --confirm-terminate-irs-runtime")
            async with engine.begin() as connection:
                terminated_steps = await terminate(connection)
        async with engine.connect() as connection:
            after = await inventory(connection)
    finally:
        await engine.dispose()

    drained = all(
        after[key] == 0
        for key in ("active_plan_count", "inflight_agent_count", "pending_callback_count")
    )
    now = datetime.now(UTC)
    return {
        "contract": "irs-runtime-drain/v1",
        "generated_at": now.isoformat(),
        "mode": args.mode,
        "before": before,
        "after": after,
        "terminated_step_count": terminated_steps,
        "drained": drained,
        "cutover_watermark": now.isoformat() if drained else None,
        "historical_messages_retained": False,
        "runtime_state_migration_enabled": False,
    }


def _ref(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("inventory", "terminate"), default="inventory")
    parser.add_argument("--database-url")
    parser.add_argument("--confirm-terminate-irs-runtime", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["drained"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
