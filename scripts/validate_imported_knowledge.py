import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.session import create_session_factory  # noqa: E402
from app.repositories.knowledge_assets import DatabaseCanonicalKnowledgeRepository  # noqa: E402
from app.schemas.common import UserContext  # noqa: E402
from app.schemas.knowledge_assets import (  # noqa: E402
    CanonicalKnowledgeSearchRequest,
    ExactReadRequest,
    ExactReadTarget,
    GroupedKnowledgeSearchRequest,
    KnowledgeAccessPolicy,
    KnowledgeSourceRef,
)
from app.services.knowledge_asset_service import KnowledgeAssetService  # noqa: E402

DATASET = Path("tests/contract/oac_irs/golden/knowledge/v1/dataset.json")


async def validate(database_url: str | None = None) -> dict:
    settings = (
        Settings(storage_backend="database", database_url=database_url)
        if database_url
        else Settings(storage_backend="database")
    )
    repository = DatabaseCanonicalKnowledgeRepository(create_session_factory(settings))
    service = KnowledgeAssetService(repository)
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    user = UserContext(id="golden-user", groups=["运营版"], attributes={"tenant_id": "oac"})
    results = []
    chunks_by_asset = {
        asset_id: await repository.get_chunks(tenant_id="oac", asset_ids=[asset_id])
        for asset_id in [f"asset_content_production_0{index}" for index in range(1, 7)]
    }
    for case in dataset["cases"]:
        operation = case["operation"]
        passed = True
        details = {}
        if operation == "search":
            request = case["request"]
            overlay = case.get("fixture_policy_overlay")
            original = None
            if overlay:
                original = await repository.get_asset(overlay["asset_id"])
                await repository.save_asset(
                    original.model_copy(
                        update={
                            "sensitivity": overlay["sensitivity"],
                            "access_policy": KnowledgeAccessPolicy(
                                allow_groups=overlay["allowed_groups"]
                            ),
                        }
                    )
                )
            response = await service.search(
                CanonicalKnowledgeSearchRequest(
                    query=request["query"],
                    user=UserContext(
                        id="golden-user",
                        groups=request.get("groups", user.groups),
                        attributes={"tenant_id": "oac"},
                    ),
                    caller=request.get("caller", "host"),
                    purpose="golden",
                    tenant_id="oac",
                    asset_ids=request.get("scope", {}).get("asset_ids", []),
                    top_k=request.get("top_k", 5),
                )
            )
            if original:
                await repository.save_asset(original)
            text = "\n".join(item.content for item in response.evidence)
            expected = case["expected"]
            passed = response.matched == expected.get("matched", False)
            passed = passed and all(fact in text for fact in expected.get("required_facts", []))
            expected_warning_codes = set(expected.get("warning_codes", []))
            actual_warning_codes = {item.get("code") for item in response.warnings}
            passed = passed and expected_warning_codes <= actual_warning_codes
            details = {
                "matched": response.matched,
                "evidence_ids": [item.chunk_id for item in response.evidence],
                "warning_codes": sorted(code for code in actual_warning_codes if code),
            }
        elif operation == "grouped_search":
            request = case["request"]
            response = await service.grouped_search(
                GroupedKnowledgeSearchRequest(
                    query=request["query"],
                    user=user,
                    caller="host",
                    purpose="golden",
                    tenant_id="oac",
                    group_id=request["asset_group"],
                    stable_asset_keys=request.get("asset_keys", []),
                    include_empty_assets=request.get("include_empty_assets", False),
                    top_k=request.get("top_k_per_asset", 3),
                )
            )
            keys = [item.stable_key for item in response.assets]
            passed = keys == case["expected"]["asset_keys_in_order"]
            if "03" in keys:
                slot = next(item for item in response.assets if item.stable_key == "03")
                passed = passed and slot.asset_id == "asset_content_production_03"
                passed = passed and not slot.matched and not slot.evidence
            details = {"asset_keys": keys}
        elif operation == "read":
            target = case["request"]["target"]
            target = _resolve_target(target, chunks_by_asset)
            response = await service.exact_read(
                ExactReadRequest(
                    tenant_id="oac",
                    user=user,
                    target=ExactReadTarget.model_validate(target),
                )
            )
            text = "\n".join(chunk.content for chunk in response.chunks)
            expected = case["expected"]
            passed = all(fact in text for fact in expected.get("required_facts", []))
            if expected.get("asset_ids_in_order"):
                passed = (
                    passed
                    and [asset.asset_id for asset in response.assets]
                    == expected["asset_ids_in_order"]
                )
            if expected.get("minimum_chunks"):
                passed = passed and len(response.chunks) >= expected["minimum_chunks"]
            if expected.get("missing_ids"):
                passed = passed and response.missing == expected["missing_ids"]
            details = {
                "asset_ids": [asset.asset_id for asset in response.assets],
                "chunk_ids": [chunk.chunk_id for chunk in response.chunks],
                "missing": response.missing,
            }
        elif operation in {"assets_list", "asset_detail", "asset_chunks", "chunk_detail"}:
            passed, details = await _validate_access_case(
                case, repository, service, user, chunks_by_asset
            )
        results.append({"id": case["id"], "operation": operation, "passed": passed, **details})
    report = {
        "contract": "oir-imported-knowledge-validation/v1",
        "dataset_version": dataset["version"],
        "total": len(results),
        "passed_count": sum(1 for item in results if item["passed"]),
        "failed": [item for item in results if not item["passed"]],
        "results": results,
        "passed": all(item["passed"] for item in results),
    }
    if report["passed"]:
        assets = await repository.list_assets(tenant_id="oac", group_id="content_production")
        for asset in assets:
            if asset.stable_key == "03":
                continue
            manifest = await repository.find_manifest(
                tenant_id="oac",
                file_hash=asset.file_hash or "",
                pipeline_version=str(asset.metadata.get("pipeline_version", "")),
            )
            if manifest is None or manifest.migration_status != "indexed":
                raise RuntimeError(f"Asset is not indexed before validation: {asset.asset_id}")
            await repository.replace_manifest(
                manifest.model_copy(update={"validation_status": "validated"})
            )
    return report


async def _validate_access_case(case, repository, service, user, chunks_by_asset):
    operation = case["operation"]
    request = case["request"]
    if operation == "assets_list":
        assets = await repository.list_assets(tenant_id="oac", group_id="content_production")
        assets.sort(key=lambda asset: asset.stable_key or "")
        ids = [asset.asset_id for asset in assets]
        return ids == case["expected"]["asset_ids_in_order"], {"asset_ids": ids}
    if operation == "asset_detail":
        asset = await repository.get_asset(request["asset_id"])
        chunks = chunks_by_asset[request["asset_id"]]
        passed = asset is not None and asset.status.value == case["expected"]["status"]
        passed = passed and len(chunks) == case["expected"]["chunk_count"]
        return passed, {"status": asset.status.value, "chunk_count": len(chunks)}
    if operation == "asset_chunks":
        chunks = chunks_by_asset[request["asset_id"]]
        return len(chunks) == case["expected"]["pagination.total"], {"chunk_count": len(chunks)}
    chunk_id = chunks_by_asset["asset_content_production_05"][0].chunk_id
    response = await service.exact_read(
        ExactReadRequest(
            tenant_id="oac",
            user=user,
            target=ExactReadTarget(type="chunk", chunk_id=chunk_id),
        )
    )
    passed = bool(response.chunks)
    return passed, {"chunk_ids": [chunk.chunk_id for chunk in response.chunks]}


def _resolve_target(target: dict, chunks_by_asset: dict) -> dict:
    resolved = json.loads(json.dumps(target))
    if resolved.get("chunk_id") == "${chunk_04_first}":
        resolved["chunk_id"] = chunks_by_asset["asset_content_production_04"][0].chunk_id
    if resolved.get("chunk_ids"):
        replacements = {
            "${chunk_04_first}": chunks_by_asset["asset_content_production_04"][0].chunk_id,
            "${chunk_05_first}": chunks_by_asset["asset_content_production_05"][0].chunk_id,
        }
        resolved["chunk_ids"] = [replacements.get(item, item) for item in resolved["chunk_ids"]]
    if resolved.get("source_ref"):
        source = resolved["source_ref"]
        resolved["source_ref"] = KnowledgeSourceRef(
            sheet=source.get("sheet"), row_start=source.get("row")
        ).model_dump(mode="json")
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser()
    database = parser.add_mutually_exclusive_group()
    database.add_argument("--database-url")
    database.add_argument("--use-configured-database", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    database_url = (
        None
        if args.use_configured_database
        else args.database_url or "sqlite+aiosqlite:///./data/oir-migration.db"
    )
    report = asyncio.run(validate(database_url))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
