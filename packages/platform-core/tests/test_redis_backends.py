"""The Redis-backed implementations must behave exactly like the in-memory ones."""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis
from sqlalchemy import MetaData

from platform_core.clock import ManualClock
from platform_core.config import CoreSettings
from platform_core.events import Event, RedisStreamsEventBus
from platform_core.idempotency import IdempotencyState, RedisIdempotencyStore
from platform_core.infrastructure import Infrastructure
from platform_core.ratelimit import RateLimit, RedisRateLimiter
from platform_core.retry import RetryableError, RetryPolicy
from platform_core.service import ServiceRuntime
from platform_core.tasks import RedisTaskQueue, Task
from platform_core.worker import Worker

pytestmark = pytest.mark.integration


async def test_task_queue_retry_dlq_and_redrive(redis_client: Redis) -> None:
    clock = ManualClock()
    queue = RedisTaskQueue(redis_client, prefix="t", clock=clock)
    await queue.enqueue(Task(name="index", queue="knowledge", payload={"doc": "d1"}))
    [reservation] = await queue.reserve("knowledge", "w1")
    assert reservation.task.payload == {"doc": "d1"}
    assert (await queue.stats("knowledge")).in_flight == 1

    await queue.retry("knowledge", reservation, delay_seconds=30, error="boom")
    assert await queue.reserve("knowledge", "w1") == []
    assert (await queue.stats("knowledge")).delayed == 1
    clock.advance(31)
    [retried] = await queue.reserve("knowledge", "w1")
    assert retried.task.attempt == 2

    await queue.dead_letter("knowledge", retried, error="PermanentError: bad")
    stats = await queue.stats("knowledge")
    assert (stats.ready, stats.delayed, stats.in_flight, stats.dead) == (0, 0, 0, 1)
    [letter] = await queue.dead_letters("knowledge")
    await queue.redrive("knowledge", letter.id)
    [again] = await queue.reserve("knowledge", "w2")
    assert again.task.attempt == 1
    await queue.ack("knowledge", again)
    assert (await queue.stats("knowledge")).ready == 0


async def test_task_queue_reclaims_tasks_from_crashed_consumers(redis_client: Redis) -> None:
    queue = RedisTaskQueue(redis_client, prefix="t", visibility_timeout=0.05)
    await queue.enqueue(Task(name="t", queue="q"))
    [first] = await queue.reserve("q", "consumer-that-dies")
    await asyncio.sleep(0.1)  # XAUTOCLAIM idle time is measured by the Redis server
    [reclaimed] = await queue.reserve("q", "healthy-consumer")
    assert reclaimed.task.id == first.task.id


async def test_idempotency_store(redis_client: Redis) -> None:
    store = RedisIdempotencyStore(redis_client, prefix="t")
    first = await store.begin("pay:inv_1", fingerprint="fp", lease_seconds=10)
    assert first.state is IdempotencyState.ACQUIRED and first.owner
    assert (await store.begin("pay:inv_1")).state is IdempotencyState.IN_PROGRESS
    assert not await store.complete("pay:inv_1", "lease_intruder", response={})
    assert await store.complete("pay:inv_1", first.owner, response={"paid": True})
    replay = await store.begin("pay:inv_1")
    assert replay.state is IdempotencyState.COMPLETED
    assert replay.response == {"paid": True}
    assert replay.fingerprint == "fp"

    lease = await store.begin("other", lease_seconds=10)
    assert lease.owner and await store.release("other", lease.owner)
    assert (await store.begin("other")).state is IdempotencyState.ACQUIRED


async def test_distributed_token_bucket(redis_client: Redis) -> None:
    clock = ManualClock()
    limiter = RedisRateLimiter(redis_client, prefix="t", clock=clock)
    limit = RateLimit(capacity=2, refill_per_second=0.5)
    results = [await limiter.acquire("ten_1", limit) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert results[-1].retry_after == pytest.approx(2.0)
    clock.advance(2)
    assert (await limiter.acquire("ten_1", limit)).allowed


async def test_event_bus_consumer_groups_and_recovery(redis_client: Redis) -> None:
    bus = RedisStreamsEventBus(redis_client, stream="t:events", claim_idle_ms=50)
    await bus.ensure_group("billing")
    await bus.ensure_group("growth")
    await bus.publish(Event.create("usage.recorded", source="/t", tenant_id="ten_1"))

    [billing] = await bus.read("billing", "billing-1")
    [growth] = await bus.read("growth", "growth-1")
    assert billing.event.type == growth.event.type == "usage.recorded"
    assert billing.event.tenantid == "ten_1"
    await bus.ack(growth)

    # billing-1 crashed without acking: another replica picks the event up.
    await asyncio.sleep(0.1)
    [recovered] = await bus.read("billing", "billing-2")
    assert recovered.event.id == billing.event.id
    await bus.ack(recovered)
    await asyncio.sleep(0.1)
    assert await bus.read("billing", "billing-2") == []


async def test_worker_end_to_end_on_redis(redis_client: Redis) -> None:
    infra = Infrastructure.from_redis(redis_client, prefix="t")
    settings = CoreSettings(service_name="demo", database_url="sqlite+aiosqlite:///:memory:")
    runtime = ServiceRuntime.create("demo", settings, infra, metadata=MetaData())
    await runtime.start()
    worker = Worker(runtime)
    attempts: list[int] = []

    @worker.task(
        "demo.flaky", retry_policy=RetryPolicy(max_attempts=3, base_delay=0.01, max_delay=0.01)
    )
    async def flaky(task: Task) -> None:
        attempts.append(task.attempt)
        if task.attempt == 1:
            raise RetryableError("first attempt fails")

    worker.on_event("thing.happened", task="demo.flaky")
    await worker.setup()
    event = runtime.event("thing.happened", {}, tenant_id="ten_1")
    await runtime.publish(event)
    await runtime.publish(event)  # duplicate delivery

    for _ in range(20):
        await worker.run_until_idle()
        if attempts == [1, 2]:
            break
        await asyncio.sleep(0.02)
    await worker.run_until_idle()
    assert attempts == [1, 2]
    await runtime.stop()
