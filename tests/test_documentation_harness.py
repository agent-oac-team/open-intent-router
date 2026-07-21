import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = ROOT / "docs"
INDEX_FILES = (
    DOCS_ROOT / "README.md",
    DOCS_ROOT / "App-Adr" / "README.md",
    DOCS_ROOT / "App-Adr" / "develop" / "develop-standards" / "README.md",
    DOCS_ROOT / "App-Adr" / "develop" / "skills" / "README.md",
    DOCS_ROOT / "App-Adr" / "test" / "test-standards" / "README.md",
    DOCS_ROOT / "App-Adr" / "test" / "skills" / "README.md",
    DOCS_ROOT / "App-Desc" / "README.md",
    DOCS_ROOT / "App-Research" / "README.md",
)
CATEGORY_DIRS = {"App-Adr", "App-Desc", "App-Research"}
ROOT_FILES = {"AGENTS.md", "README.md"}
ENTRY_FILES = (
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "app" / "AGENTS.md",
    ROOT / "docs" / "AGENTS.md",
    ROOT / "host_adapters" / "oac" / "AGENTS.md",
    ROOT / "host_apps" / "oac" / "AGENTS.md",
    ROOT / "tests" / "AGENTS.md",
    ROOT / "web" / "AGENTS.md",
)
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
URI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _markdown_files() -> list[Path]:
    return [*ENTRY_FILES, *sorted(DOCS_ROOT.rglob("*.md"))]


def _local_link_targets(markdown_file: Path) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    content = markdown_file.read_text(encoding="utf-8")
    for match in MARKDOWN_LINK.finditer(content):
        raw_target = match.group(1).strip().removeprefix("<").removesuffix(">")
        path_text = unquote(raw_target.split("#", 1)[0])
        if not path_text or raw_target.startswith("#") or URI_SCHEME.match(path_text):
            continue
        target = Path(path_text)
        if not target.is_absolute():
            target = markdown_file.parent / target
        targets.append((raw_target, target.resolve()))
    return targets


def test_all_local_markdown_links_resolve() -> None:
    broken: list[str] = []
    for markdown_file in _markdown_files():
        for raw_target, target in _local_link_targets(markdown_file):
            if not target.exists():
                broken.append(f"{markdown_file.relative_to(ROOT)} -> {raw_target}")
    assert not broken, "Broken local Markdown links:\n" + "\n".join(broken)


def test_every_docs_markdown_file_is_indexed() -> None:
    indexed_targets = {
        target for index_file in INDEX_FILES for _, target in _local_link_targets(index_file)
    }
    index_paths = {path.resolve() for path in INDEX_FILES}
    unindexed = [
        path.relative_to(ROOT).as_posix()
        for path in sorted(DOCS_ROOT.rglob("*.md"))
        if path.resolve() not in index_paths and path.resolve() not in indexed_targets
    ]
    assert not unindexed, "Unindexed docs Markdown files:\n" + "\n".join(unindexed)


def test_docs_artifacts_are_physically_classified() -> None:
    unexpected_root_entries = sorted(
        path.name
        for path in DOCS_ROOT.iterdir()
        if (path.is_file() and path.name not in ROOT_FILES)
        or (path.is_dir() and path.name not in CATEGORY_DIRS)
    )
    assert not unexpected_root_entries, (
        "Unexpected docs root entries; classify them under App-Adr, App-Desc, "
        "or App-Research:\n" + "\n".join(unexpected_root_entries)
    )


def test_every_docs_json_evidence_file_is_indexed() -> None:
    indexed_targets = {
        target for index_file in INDEX_FILES for _, target in _local_link_targets(index_file)
    }
    unindexed = [
        path.relative_to(ROOT).as_posix()
        for path in sorted(DOCS_ROOT.rglob("*.json"))
        if path.resolve() not in indexed_targets
    ]
    assert not unindexed, "Unindexed docs JSON evidence files:\n" + "\n".join(unindexed)
