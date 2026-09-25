from __future__ import annotations

import random

import pytest

from platform_core.clock import ManualClock
from platform_core.ratelimit import InMemoryRateLimiter, RateLimit
from platform_core.retry import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    PermanentError,
    RetryableError,
    RetryPolicy,
    retry_async,
)


def test_full_jitter_backoff_is_bounded_and_capped() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=10.0)
    rng = random.Random(7)
    for attempt, cap in [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 10.0), (12, 10.0)]:
        delays = [policy.backoff(attempt, rng) for _ in range(200)]
        assert all(0.0 <= d <= cap for d in delays)
        assert max(delays) > cap * 0.8  # jitter actually spreads across the window


def test_permanent_errors_are_never_retried() -> None:
    policy = RetryPolicy(max_attempts=5)
    assert policy.should_retry(1, RetryableError("x"))
    assert not policy.should_retry(5, RetryableError("x"))
    assert not policy.should_retry(1, PermanentError("bad input"))


async def test_retry_async_recovers_from_transient_failures() -> None:
    calls = 0
    sleeps: list[float] = []

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RetryableError("temporary", retry_after=0.5)
        return "ok"

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    result = await retry_async(flaky, RetryPolicy(max_attempts=5), sleep=fake_sleep)
    assert result == "ok"
    assert calls == 3
    assert all(s >= 0.5 for s in sleeps)  # Retry-After is honoured


async def test_retry_async_gives_up() -> None:
    async def always_fails() -> None:
        raise RetryableError("down")

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(RetryableError):
        await retry_async(always_fails, RetryPolicy(max_attempts=3), sleep=no_sleep)


async def test_circuit_breaker_opens_then_half_opens_then_closes() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker("llm", failure_threshold=2, reset_timeout=30, clock=clock)

    async def boom() -> None:
        raise RuntimeError("upstream 500")

    async def ok() -> str:
        return "ok"

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(boom)
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.call(ok)  # fails fast without calling upstream

    clock.advance(31)
    assert breaker.state is CircuitState.HALF_OPEN
    with pytest.raises(RuntimeError):
        await breaker.call(boom)  # failed trial re-opens immediately
    assert breaker.state is CircuitState.OPEN

    clock.advance(31)
    assert await breaker.call(ok) == "ok"
    assert breaker.state is CircuitState.CLOSED


async def test_token_bucket_allows_burst_then_throttles_then_refills() -> None:
    clock = ManualClock()
    limiter = InMemoryRateLimiter(clock)
    limit = RateLimit(capacity=3, refill_per_second=1.0)
    decisions = [await limiter.acquire("tenant-a", limit) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].retry_after == pytest.approx(1.0)
    # Another tenant is unaffected (noisy-neighbour isolation).
    assert (await limiter.acquire("tenant-b", limit)).allowed
    clock.advance(2)
    assert (await limiter.acquire("tenant-a", limit)).allowed
    assert (await limiter.acquire("tenant-a", limit)).allowed
    assert not (await limiter.acquire("tenant-a", limit)).allowed


def test_rate_limit_per_minute_helper() -> None:
    limit = RateLimit.per_minute(120, burst=20)
    assert limit.capacity == 20
    assert limit.refill_per_second == pytest.approx(2.0)
