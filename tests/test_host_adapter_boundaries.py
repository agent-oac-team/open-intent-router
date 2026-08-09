from pathlib import Path

from scripts.validate_host_adapter_boundaries import find_boundary_violations


def test_oir_core_has_no_reverse_host_dependency_or_proprietary_identifier() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    assert find_boundary_violations(repository_root) == []


def test_boundary_validator_detects_reverse_import_and_host_identifier(tmp_path: Path) -> None:
    core_file = tmp_path / "app" / "api" / "leak.py"
    core_file.parent.mkdir(parents=True)
    core_file.write_text(
        "from host_adapters.oac import api\nLEGACY_FIELD = 'bot_id'\n",
        encoding="utf-8",
    )

    violations = find_boundary_violations(tmp_path)

    assert any("reverse host import" in violation for violation in violations)
    assert any("proprietary host identifier: oac" in violation for violation in violations)
    assert any("proprietary host identifier: bot_id" in violation for violation in violations)
