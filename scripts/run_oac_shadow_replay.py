#!/usr/bin/env python3
"""Run the versioned OAC shadow dataset through the replay engine."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from host_adapters.oac.fallback.policy import classify_operation
from host_adapters.oac.shadow.runner import ShadowReplayRunner


class ReportRepository:
    def __init__(self) -> None:
        self.dataset: dict = {}
        self.results: list[dict] = []
        self.diffs: list[dict] = []

    async def save_dataset(self, dataset: dict) -> None:
        self.dataset = dataset

    async def save_result(self, result: dict, diffs: list[dict]) -> None:
        self.results.append(result)
        self.diffs.extend(diffs)


async def run(dataset_path: Path) -> dict:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    repository = ReportRepository()
    runner = ShadowReplayRunner(repository=repository)
    report = await runner.run(
        dataset,
        operation_resolver=lambda sample: classify_operation(sample["method"], sample["path"]),
        oir_executor=lambda sample: _stored_shadow_result(sample),
    )
    return {
        **report,
        "execution_scope": dataset.get("metadata", {}).get("execution_scope"),
        "full_environment_replay_required": dataset.get("metadata", {}).get(
            "full_environment_replay_required", True
        ),
        "diff_count": len(repository.diffs),
    }


async def _stored_shadow_result(sample: dict) -> dict:
    """Dataset smoke proves runner behavior; environment replay supplies live executors."""
    return sample["oir_result"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/contract/oac_irs/shadow/v1/dataset.json"),
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args.dataset))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
