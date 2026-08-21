"""Database-side enforcement for the short-lived Native migration window."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NativeDefinitionMigrationFrozenError
from app.db.models import NativeDefinitionMigrationPreparationModel

_FenceKind = Literal["native_write", "new_execution"]


async def require_native_definition_migration_fence_open(
    session: AsyncSession,
    *,
    kind: _FenceKind,
) -> None:
    """Reject a write that would race an active offline migration.

    The row lock is part of the fence protocol: a migration holds the same
    lock while it rechecks drain state and replaces Registry source.  Existing
    terminalization paths intentionally do not call this function, so the
    maintenance window can still drain work that began before ``prepare``.
    """

    column = (
        NativeDefinitionMigrationPreparationModel.native_writes_frozen
        if kind == "native_write"
        else NativeDefinitionMigrationPreparationModel.new_execution_frozen
    )
    rows = (
        await session.scalars(
            select(NativeDefinitionMigrationPreparationModel)
            .where(
                NativeDefinitionMigrationPreparationModel.active.is_(True),
                column.is_(True),
            )
            .with_for_update()
        )
    ).all()
    if rows:
        raise NativeDefinitionMigrationFrozenError(
            "Native Definition migration maintenance window is active"
        )
