"""Fault tolerance: exponential backoff with full jitter, and circuit breakers.

*Backoff* spaces retries out (1s, 2s, 4s, ...) so a struggling dependency gets
room to recover. *Full jitter* randomises each wait between 0 and the cap so a
thousand clients that failed together do not retry together (the "thundering
herd"). A *circuit breaker* stops calling a dependency that keeps failing and
fails fast instead, then probes it again after a cool-down.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from platform_core.clock import Clock, SystemClock

T = TypeVar("T")


class RetryableError(Exception):
    """A transient failure worth retrying (timeouts, 429s, 5xx responses)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """A failure that will not fix itself (bad input, 4xx) - do not retry, dead-letter it."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 300.0
    multiplier: float = 2.0

    def backoff(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay before retry number ``attempt`` (1-based), using full jitter.

        ``sleep = random(0, min(max_delay, base_delay * multiplier ** (attempt - 1)))``
        """
        cap = min(self.max_delay, self.base_delay * self.multiplier ** max(0, attempt - 1))
        return (rng or random).uniform(0.0, cap)

    def should_retry(self, attempt: int, error: BaseException) -> bool:
        if isinstance(error, PermanentError):
            return False
        return attempt < self.max_attempts


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    retry_on: tuple[type[BaseException], ...] = (RetryableError,),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    """Run ``operation`` with in-process retries (for short, synchronous-path calls)."""
    attempt = 1
    while True:
        try:
            return await operation()
        except retry_on as error:
            if not policy.should_retry(attempt, error):
                raise
            delay = policy.backoff(attempt, rng)
            if isinstance(error, RetryableError) and error.retry_after is not None:
                delay = max(delay, error.retry_after)
            await sleep(delay)
            attempt += 1


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RetryableError):
    pass


class CircuitBreaker:
    """Classic three-state breaker.

    * **closed**: calls flow; consecutive failures are counted.
    * **open**: after ``failure_threshold`` failures, calls fail fast for ``reset_timeout``.
    * **half-open**: one trial call is let through; success closes, failure re-opens.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Clock | None = None,
    ) -> None:
        self.name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock or SystemClock()
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock.time() - self._opened_at >= self._reset_timeout:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def before_call(self) -> None:
        state = self.state
        if state is CircuitState.OPEN:
            raise CircuitOpenError(f"circuit '{self.name}' is open")
        if state is CircuitState.HALF_OPEN:
            if self._trial_in_flight:
                raise CircuitOpenError(f"circuit '{self.name}' is half-open (trial in flight)")
            self._trial_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self) -> None:
        self._trial_in_flight = False
        self._failures += 1
        if self._opened_at is not None or self._failures >= self._failure_threshold:
            self._opened_at = self._clock.time()

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        self.before_call()
        try:
            result = await operation()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result
