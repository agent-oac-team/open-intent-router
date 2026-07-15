from collections.abc import Sequence
from typing import Protocol

from app.schemas.memory import (
    MemoryFormationCandidate,
    MemoryFormationTurn,
    MemorySemanticVerification,
)


class MemorySemanticVerifier(Protocol):
    async def verify(
        self,
        *,
        candidate: MemoryFormationCandidate,
        turns: Sequence[MemoryFormationTurn],
    ) -> MemorySemanticVerification: ...


class FakeMemorySemanticVerifier:
    def __init__(self, response: MemorySemanticVerification) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def verify(
        self,
        *,
        candidate: MemoryFormationCandidate,
        turns: Sequence[MemoryFormationTurn],
    ) -> MemorySemanticVerification:
        self.calls.append(
            {
                "candidate": candidate.model_copy(deep=True),
                "turns": [turn.model_copy(deep=True) for turn in turns],
            }
        )
        return self.response.model_copy(deep=True)


__all__ = ["FakeMemorySemanticVerifier", "MemorySemanticVerifier"]
