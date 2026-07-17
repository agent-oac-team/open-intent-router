from app.core.config import Settings
from app.db.session import create_all_tables
from scripts.run_oac_state_rehearsal import run_rehearsal


async def test_state_rehearsal_writes_only_isolated_database(tmp_path) -> None:
    primary_url = f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}"
    rehearsal_url = f"sqlite+aiosqlite:///{tmp_path / 'rehearsal.db'}"
    await create_all_tables(Settings(database_url=primary_url, storage_backend="database"))
    await create_all_tables(Settings(database_url=rehearsal_url, storage_backend="database"))

    report = await run_rehearsal(
        primary_database_url=primary_url,
        rehearsal_database_url=rehearsal_url,
    )

    assert report["passed"] is True
    assert report["primary_side_effect_count"] == 0
    assert report["checks"]["all_rehearsal_domains_written"] is True
