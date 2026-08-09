from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


def active_change_names(changed_paths: Iterable[str], changes_root: Path) -> list[str]:
    active_names = {
        path.name for path in changes_root.iterdir() if path.is_dir() and path.name != "archive"
    }
    changed_names: set[str] = set()
    prefix = "openspec/changes/"
    for path in changed_paths:
        if not path.startswith(prefix):
            continue
        change_name = path.removeprefix(prefix).split("/", 1)[0]
        if change_name in active_names:
            changed_names.add(change_name)
    return sorted(changed_names)


def all_active_change_names(changes_root: Path) -> list[str]:
    return sorted(
        path.name for path in changes_root.iterdir() if path.is_dir() and path.name != "archive"
    )


def git_changed_paths(repo_root: Path, base: str, head: str) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            "-z",
            base,
            head,
            "--",
            "openspec/changes",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return [os.fsdecode(path) for path in completed.stdout.split(b"\0") if path]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List active OpenSpec changes affected by a git diff."
    )
    parser.add_argument("--all-active", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args()
    if args.all_active:
        if args.base or args.head:
            parser.error("--all-active cannot be combined with --base or --head")
    elif not args.base or not args.head:
        parser.error("--base and --head are required unless --all-active is used")
    return args


def main() -> int:
    args = _parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    changes_root = repo_root / "openspec" / "changes"
    if args.all_active:
        names = all_active_change_names(changes_root)
    else:
        names = active_change_names(
            git_changed_paths(repo_root, args.base, args.head),
            changes_root,
        )
    sys.stdout.write("".join(f"{name}\n" for name in names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
