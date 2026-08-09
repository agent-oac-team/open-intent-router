import asyncio
from typing import Protocol


class NonceStore(Protocol):
    async def consume(self, *, key_id: str, nonce: str, now: int, ttl_seconds: int) -> bool: ...


class MemoryNonceStore:
    def __init__(self) -> None:
        self._expires_at: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    async def consume(self, *, key_id: str, nonce: str, now: int, ttl_seconds: int) -> bool:
        async with self._lock:
            self._expires_at = {
                key: expiry for key, expiry in self._expires_at.items() if expiry > now
            }
            key = (key_id, nonce)
            if key in self._expires_at:
                return False
            self._expires_at[key] = now + ttl_seconds
            return True
