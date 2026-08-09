import json
import subprocess
import sys
from pathlib import Path

from app.core.config import Settings
from app.db.session import create_all_tables


async def test_state_rehearsal_writes_only_isolated_database(tmp_path) -> None:
    primary_url = f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}"
    rehearsal_url = f"sqlite+aiosqlite:///{tmp_path / 'rehearsal.db'}"
    await create_all_tables(Settings(database_url=primary_url, storage_backend="database"))
    env_file = tmp_path / ".env"
    report_path = tmp_path / "report.json"
    env_file.write_text(
        f"DATABASE_URL={primary_url}\nOAC_HOST_STATE_REHEARSAL_DATABASE_URL={rehearsal_url}\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            "scripts/run_oac_state_rehearsal.py",
            "--env-file",
            str(env_file),
            "--report",
            str(report_path),
        ],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["passed"] is True
    assert report["primary_side_effect_count"] == 0
    assert report["checks"]["all_rehearsal_domains_written"] is True
