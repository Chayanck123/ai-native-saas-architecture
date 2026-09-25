"""Shared infrastructure handles (event bus, task queue, idempotency, rate limiting).

``REDIS_URL`` set -> Redis-backed implementations (production, docker compose).
Otherwise -> in-memory implementations (tests, the single-process devstack).
Business code only sees the interfaces, so it cannot tell the difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from redis.asyncio import Redis

from platform_core.clock import Clock, SystemClock
from platform_core.config import CoreSettings
from platform_core.events import EventBus, InMemoryEventBus, RedisStreamsEventBus
from platform_core.idempotency import (
    IdempotencyStore,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
)
from platform_core.ratelimit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter
from platform_core.tasks import InMemoryTaskQueue, RedisTaskQueue, TaskQueue


@dataclass
class Infrastructure:
    event_bus: EventBus
    task_queue: TaskQueue
    idempotency: IdempotencyStore
    rate_limiter: RateLimiter
    clock: Clock = field(default_factory=SystemClock)
    redis: Redis | None = None

    @classmethod
    def in_memory(cls, clock: Clock | None = None) -> Infrastructure:
        clock = clock or SystemClock()
        return cls(
            event_bus=InMemoryEventBus(),
            task_queue=InMemoryTaskQueue(clock),
            idempotency=InMemoryIdempotencyStore(clock),
            rate_limiter=InMemoryRateLimiter(clock),
            clock=clock,
        )

    @classmethod
    def from_redis(
        cls, redis: Redis, *, prefix: str = "ainative", clock: Clock | None = None
    ) -> Infrastructure:
        clock = clock or SystemClock()
        return cls(
            event_bus=RedisStreamsEventBus(redis, stream=f"{prefix}:events"),
            task_queue=RedisTaskQueue(redis, prefix=prefix, clock=clock),
            idempotency=RedisIdempotencyStore(redis, prefix=prefix),
            rate_limiter=RedisRateLimiter(redis, prefix=prefix, clock=clock),
            clock=clock,
            redis=redis,
        )

    @classmethod
    def from_settings(cls, settings: CoreSettings) -> Infrastructure:
        if settings.redis_url:
            redis = Redis.from_url(settings.redis_url)
            return cls.from_redis(redis, prefix=settings.redis_prefix)
        return cls.in_memory()

    async def ping(self) -> bool:
        if self.redis is None:
            return True
        try:
            return bool(await self.redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
