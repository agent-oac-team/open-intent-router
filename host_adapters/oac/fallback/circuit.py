from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitSnapshot:
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    opened_at: datetime | None = None
    probe_in_flight: bool = False


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int, recovery_seconds: float) -> None:
        if failure_threshold < 1 or recovery_seconds <= 0:
            raise ValueError("Circuit thresholds must be positive")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.snapshot = CircuitSnapshot()

    def allow_request(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        if self.snapshot.state == CircuitState.CLOSED:
            return True
        if self.snapshot.state == CircuitState.OPEN:
            if self.snapshot.opened_at is None or current < self.snapshot.opened_at + timedelta(
                seconds=self.recovery_seconds
            ):
                return False
            self.snapshot.state = CircuitState.HALF_OPEN
        if self.snapshot.probe_in_flight:
            return False
        self.snapshot.probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.snapshot = CircuitSnapshot()

    def record_failure(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        if self.snapshot.state == CircuitState.HALF_OPEN:
            self.snapshot.state = CircuitState.OPEN
            self.snapshot.opened_at = current
            self.snapshot.probe_in_flight = False
            return
        self.snapshot.failure_count += 1
        if self.snapshot.failure_count >= self.failure_threshold:
            self.snapshot.state = CircuitState.OPEN
            self.snapshot.opened_at = current
        self.snapshot.probe_in_flight = False
