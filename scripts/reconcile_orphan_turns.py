import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.managed import ManagedDatabase  # noqa: E402
from app.services.orphan_turn_reconciler import DatabaseOrphanTurnReconciler  # noqa: E402


def _timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO-8601 timestamp") from exc


async def _run(args) -> dict:
    settings = Settings(
        storage_backend="database",
        **({} if args.use_configured_database else {"database_url": args.database_url}),
    )
    async with ManagedDatabase.from_settings(settings) as database:
        reconciler = DatabaseOrphanTurnReconciler(database.session_factory)
        report = await reconciler.scan(
            tenant_id=args.tenant_id,
            user_id=args.user_id,
            request_ids=args.request_id,
            updated_before=args.updated_before,
            limit=args.limit,
        )
        payload = report.model_dump(mode="json")
        if args.apply:
            payload["dry_run"] = False
            payload["repairs"] = [
                (
                    await reconciler.repair(
                        tenant_id=args.tenant_id,
                        user_id=args.user_id,
                        request_id=request_id,
                        idempotency_key=f"{args.idempotency_key}:{request_id}",
                    )
                ).model_dump(mode="json")
                for request_id in args.request_id
            ]
        return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run or explicitly repair ownership-scoped orphan Canonical Turns."
    )
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--database-url")
    database.add_argument("--use-configured-database", action="store_true")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--request-id", action="append", default=[])
    parser.add_argument("--updated-before", type=_timestamp)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 1000:
        parser.error("--limit must be between 1 and 1000")
    if not args.request_id and args.updated_before is None:
        parser.error("explicit scope requires --request-id or --updated-before")
    if args.apply and (not args.request_id or not args.idempotency_key):
        parser.error("--apply requires --request-id and --idempotency-key")

    payload = asyncio.run(_run(args))
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
