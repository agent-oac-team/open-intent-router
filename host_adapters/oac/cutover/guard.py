import hashlib
from datetime import UTC, datetime, timedelta


class CutoverGuard:
    def __init__(self, *, watermark: datetime | None, repository) -> None:
        self.watermark = _as_utc(watermark) if watermark else None
        self.repository = repository

    async def quarantine_if_legacy(
        self,
        *,
        event_id: str,
        session_id: str,
        user_id: str,
        agent_id: str,
        occurred_at: datetime | None,
        ticket_expires_at: datetime | None = None,
        ticket_ttl_seconds: int | None = None,
    ) -> bool:
        if self.watermark is None:
            return False
        event_epoch = _as_utc(occurred_at) if occurred_at else None
        if event_epoch is None and ticket_expires_at and ticket_ttl_seconds:
            event_epoch = _as_utc(ticket_expires_at) - timedelta(seconds=ticket_ttl_seconds)
        if event_epoch is None or event_epoch >= self.watermark:
            return False
        await self.repository.append(
            {
                "contract": "oac-cutover-quarantine/v1",
                "reason": "pre_cutover_event",
                "event_id_hash": _fingerprint(event_id),
                "session_id_hash": _fingerprint(session_id),
                "user_id_hash": _fingerprint(user_id),
                "agent_id_hash": _fingerprint(agent_id),
                "event_epoch": event_epoch.isoformat(),
                "cutover_watermark": self.watermark.isoformat(),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )
        return True


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
