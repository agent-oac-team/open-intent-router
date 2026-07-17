from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.repositories.json_utils import dumps, loads


class ShadowBase(DeclarativeBase):
    pass


class ShadowReplayDatasetModel(ShadowBase):
    __tablename__ = "oac_shadow_replay_datasets"

    dataset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ShadowReplayResultModel(ShadowBase):
    __tablename__ = "oac_shadow_replay_results"
    __table_args__ = (
        UniqueConstraint("dataset_id", "sample_id", name="uq_oac_shadow_dataset_sample"),
    )

    result_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sample_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    irs_result_text: Mapped[str] = mapped_column(Text, nullable=False)
    oir_result_text: Mapped[str] = mapped_column(Text, nullable=False)
    irs_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    oir_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ShadowDiffModel(ShadowBase):
    __tablename__ = "oac_shadow_diffs"

    diff_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details_text: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


async def create_shadow_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(ShadowBase.metadata.create_all)


class DatabaseShadowRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def save_dataset(self, dataset: dict) -> None:
        async with self.session_factory() as session, session.begin():
            row = await session.get(ShadowReplayDatasetModel, dataset["dataset_id"])
            values = {
                "dataset_id": dataset["dataset_id"],
                "version": dataset["version"],
                "sample_count": dataset["sample_count"],
                "metadata_text": dumps(dataset.get("metadata", {})),
            }
            if row is None:
                session.add(ShadowReplayDatasetModel(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    async def save_result(self, result: dict, diffs: list[dict]) -> None:
        async with self.session_factory() as session, session.begin():
            row = await session.scalar(
                select(ShadowReplayResultModel).where(
                    ShadowReplayResultModel.dataset_id == result["dataset_id"],
                    ShadowReplayResultModel.sample_id == result["sample_id"],
                )
            )
            values = {
                "result_id": result["result_id"],
                "dataset_id": result["dataset_id"],
                "sample_id": result["sample_id"],
                "operation": result["operation"],
                "irs_result_text": dumps(result["irs_result"]),
                "oir_result_text": dumps(result["oir_result"]),
                "irs_latency_ms": result.get("irs_latency_ms", 0),
                "oir_latency_ms": result.get("oir_latency_ms", 0),
            }
            if row is None:
                row = ShadowReplayResultModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.flush()
            for diff in diffs:
                existing = await session.get(ShadowDiffModel, diff["diff_id"])
                diff_values = {
                    "diff_id": diff["diff_id"],
                    "result_id": result["result_id"],
                    "fingerprint": diff["fingerprint"],
                    "severity": diff["severity"],
                    "blocking": diff["blocking"],
                    "approved": diff.get("approved", False),
                    "details_text": dumps(diff.get("details", {})),
                }
                if existing is None:
                    session.add(ShadowDiffModel(**diff_values))
                else:
                    for key, value in diff_values.items():
                        setattr(existing, key, value)

    async def snapshot(self, dataset_id: str) -> dict:
        async with self.session_factory() as session:
            results = (
                (
                    await session.execute(
                        select(ShadowReplayResultModel).where(
                            ShadowReplayResultModel.dataset_id == dataset_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            result_ids = [row.result_id for row in results]
            diffs = (
                (
                    await session.execute(
                        select(ShadowDiffModel).where(ShadowDiffModel.result_id.in_(result_ids))
                    )
                )
                .scalars()
                .all()
                if result_ids
                else []
            )
            return {
                "results": [
                    {
                        "sample_id": row.sample_id,
                        "operation": row.operation,
                        "irs_result": loads(row.irs_result_text, {}),
                        "oir_result": loads(row.oir_result_text, {}),
                    }
                    for row in results
                ],
                "diffs": [
                    {
                        "fingerprint": row.fingerprint,
                        "severity": row.severity,
                        "blocking": row.blocking,
                        "approved": row.approved,
                        "details": loads(row.details_text, {}),
                    }
                    for row in diffs
                ],
            }
