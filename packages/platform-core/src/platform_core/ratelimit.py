"""Per-tenant rate limiting with the token-bucket algorithm.

Each tenant has a bucket holding up to ``capacity`` tokens (the allowed burst).
Tokens drip back in at ``refill_per_second``; each request spends one. When the
bucket is empty the request is rejected with ``429`` and a ``Retry-After``
hint. Limits follow the tenant's plan, so one noisy free-tier tenant cannot
degrade the service for everybody else.

    tokens_now = min(capacity, tokens_last + elapsed_seconds * refill_per_second)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

from platform_core.clock import Clock, SystemClock


@dataclass(frozen=True)
class RateLimit:
    capacity: int
    refill_per_second: float

    @classmethod
    def per_minute(cls, requests: int, *, burst: int | None = None) -> RateLimit:
        return cls(capacity=burst or requests, refill_per_second=requests / 60.0)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float


class RateLimiter(Protocol):
    async def acquire(
        self, key: str, limit: RateLimit, *, cost: float = 1.0
    ) -> RateLimitDecision: ...


def _decide(
    tokens: float, last: float, now: float, limit: RateLimit, cost: float
) -> tuple[RateLimitDecision, float]:
    tokens = min(float(limit.capacity), tokens + max(0.0, now - last) * limit.refill_per_second)
    if tokens >= cost:
        tokens -= cost
        return RateLimitDecision(True, limit.capacity, int(tokens), 0.0), tokens
    retry_after = (cost - tokens) / limit.refill_per_second
    return RateLimitDecision(False, limit.capacity, int(tokens), retry_after), tokens


class InMemoryRateLimiter:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._buckets: dict[str, tuple[float, float]] = {}

    async def acquire(self, key: str, limit: RateLimit, *, cost: float = 1.0) -> RateLimitDecision:
        now = self._clock.time()
        tokens, last = self._buckets.get(key, (float(limit.capacity), now))
        decision, tokens = _decide(tokens, last, now, limit, cost)
        self._buckets[key] = (tokens, now)
        return decision


_BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = (cost - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
return {allowed, tostring(tokens), tostring(retry_after)}
"""


class RedisRateLimiter:
    """Distributed token bucket: one atomic Lua script per request, shared by all replicas."""

    def __init__(self, redis: Redis, *, prefix: str = "ainative", clock: Clock | None = None):
        self._redis = redis
        self._prefix = prefix
        self._clock = clock or SystemClock()
        self._script = redis.register_script(_BUCKET_SCRIPT)

    async def acquire(self, key: str, limit: RateLimit, *, cost: float = 1.0) -> RateLimitDecision:
        allowed, tokens, retry_after = await self._script(
            keys=[f"{self._prefix}:rl:{key}"],
            args=[limit.capacity, limit.refill_per_second, self._clock.time(), cost],
        )
        return RateLimitDecision(
            allowed=bool(int(allowed)),
            limit=limit.capacity,
            remaining=int(float(tokens)),
            retry_after=float(retry_after),
        )
