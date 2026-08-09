import asyncio
import json
from pathlib import Path


class MemoryCutoverAuditRepository:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def append(self, record: dict) -> None:
        self.records.append(dict(record))


class FileCutoverAuditRepository:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def append(self, record: dict) -> None:
        rendered = json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, rendered)

    def _append(self, rendered: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(rendered)
