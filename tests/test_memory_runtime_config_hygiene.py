import re
from pathlib import Path

from app.core.memory_runtime import RETIRED_MEMORY_BEHAVIOR_VARIABLES

ROOT = Path(__file__).resolve().parents[1]
CURRENT_CONFIG_FILES = (
    ROOT / ".env.example",
    ROOT / "config/oac-host.local.env.example",
    ROOT / "README.md",
    ROOT / "docs/App-Adr/develop/skills/runbooks/oac-host-migration.md",
    ROOT / "docs/App-Adr/develop/skills/runbooks/conversation-memory-formation-rollout.md",
)


def test_retired_memory_variables_are_not_assignable_in_current_config_or_runbooks() -> None:
    assignment = re.compile(
        rf"(?m)^\s*(?:export\s+)?({'|'.join(sorted(RETIRED_MEMORY_BEHAVIOR_VARIABLES))})\s*="
    )
    violations = []
    for path in CURRENT_CONFIG_FILES:
        for match in assignment.finditer(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(ROOT)}:{match.group(1)}")

    assert violations == []
