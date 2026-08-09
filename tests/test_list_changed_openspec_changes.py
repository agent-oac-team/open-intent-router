from pathlib import Path

from scripts.list_changed_openspec_changes import (
    active_change_names,
    all_active_change_names,
)


def _changes_root(tmp_path: Path) -> Path:
    changes_root = tmp_path / "openspec" / "changes"
    for name in ("archive", "active-one", "active-two"):
        (changes_root / name).mkdir(parents=True)
    return changes_root


def test_active_change_names_excludes_archived_and_deleted_changes(tmp_path: Path) -> None:
    changes_root = _changes_root(tmp_path)

    assert active_change_names(
        [
            "openspec/changes/active-two/tasks.md",
            "openspec/changes/archive/2026-07-09-retired/tasks.md",
            "openspec/changes/deleted-change/design.md",
            "openspec/changes/active-one/specs/router/spec.md",
            "openspec/changes/active-two/proposal.md",
            "README.md",
        ],
        changes_root,
    ) == ["active-one", "active-two"]


def test_all_active_change_names_is_sorted_and_excludes_archive(tmp_path: Path) -> None:
    changes_root = _changes_root(tmp_path)

    assert all_active_change_names(changes_root) == ["active-one", "active-two"]
