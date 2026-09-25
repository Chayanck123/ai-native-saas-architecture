"""Time abstraction so that retries, TTLs and analytics windows are testable."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(UTC)


class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall-clock time (timezone-aware, UTC)."""
        ...

    def time(self) -> float:
        """Current time as a POSIX timestamp in seconds."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()

    def time(self) -> float:
        return time.time()


class ManualClock:
    """A clock that only moves when told to - used by tests and simulations."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def time(self) -> float:
        return self._now.timestamp()

    def advance(self, seconds: float = 0.0, **kwargs: float) -> None:
        self._now += timedelta(seconds=seconds, **kwargs)

    def set(self, when: datetime) -> None:
        self._now = when
