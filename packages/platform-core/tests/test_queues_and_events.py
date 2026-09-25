from __future__ import annotations

import pytest

from platform_core.clock import ManualClock
from platform_core.context import use_tenant
from platform_core.errors import NotFound
from platform_core.events import Event, InMemoryEventBus, matches
from platform_core.idempotency import IdempotencyState, InMemoryIdempotencyStore
from platform_core.tasks import InMemoryTaskQueue, Task


async def test_task_lifecycle_ready_reserve_ack() -> None:
    queue = InMemoryTaskQueue(ManualClock())
    await queue.enqueue(Task(name="index", queue="knowledge", payload={"doc": 1}))
    [reservation] = await queue.reserve("knowledge", "w1")
    assert reservation.task.payload == {"doc": 1}
    assert (await queue.stats("knowledge")).in_flight == 1
    await queue.ack("knowledge", reservation)
    stats = await queue.stats("knowledge")
    assert (stats.ready, stats.in_flight, stats.delayed, stats.dead) == (0, 0, 0, 0)


async def test_delayed_retry_becomes_ready_after_backoff() -> None:
    clock = ManualClock()
    queue = InMemoryTaskQueue(clock)
    await queue.enqueue(Task(name="t", queue="q"))
    [reservation] = await queue.reserve("q", "w1")
    await queue.retry("q", reservation, delay_seconds=30, error="boom")
    assert await queue.reserve("q", "w1") == []
    assert queue.next_due_in("q") == pytest.approx(30)
    clock.advance(30)
    [retried] = await queue.reserve("q", "w1")
    assert retried.task.attempt == 2
    assert retried.task.last_error == "boom"


async def test_visibility_timeout_recovers_tasks_from_crashed_workers() -> None:
    clock = ManualClock()
    queue = InMemoryTaskQueue(clock, visibility_timeout=60)
    await queue.enqueue(Task(name="t", queue="q"))
    [first] = await queue.reserve("q", "worker-that-crashes")
    clock.advance(61)
    [again] = await queue.reserve("q", "healthy-worker")
    assert again.task.id == first.task.id


async def test_dead_letter_and_redrive() -> None:
    queue = InMemoryTaskQueue(ManualClock())
    await queue.enqueue(Task(name="t", queue="q"))
    [reservation] = await queue.reserve("q", "w")
    await queue.dead_letter("q", reservation, error="ValueError: bad payload")
    [letter] = await queue.dead_letters("q")
    assert letter.error.startswith("ValueError")
    redriven = await queue.redrive("q", letter.id)
    assert redriven.attempt == 1
    assert (await queue.stats("q")).dead == 0
    assert len(await queue.reserve("q", "w")) == 1
    with pytest.raises(NotFound):
        await queue.redrive("q", "dlq_missing")


async def test_idempotency_store_state_machine() -> None:
    clock = ManualClock()
    store = InMemoryIdempotencyStore(clock)
    first = await store.begin("charge:inv_1", fingerprint="abc", lease_seconds=10)
    assert first.state is IdempotencyState.ACQUIRED
    assert first.owner is not None
    second = await store.begin("charge:inv_1")
    assert second.state is IdempotencyState.IN_PROGRESS
    # A stale owner cannot complete someone else's lease.
    assert not await store.complete("charge:inv_1", "lease_not_mine", response={"x": 1})
    assert await store.complete("charge:inv_1", first.owner, response={"charged": True})
    replay = await store.begin("charge:inv_1")
    assert replay.state is IdempotencyState.COMPLETED
    assert replay.response == {"charged": True}
    assert replay.fingerprint == "abc"


async def test_idempotency_lease_expires_so_crashes_do_not_block_forever() -> None:
    clock = ManualClock()
    store = InMemoryIdempotencyStore(clock)
    first = await store.begin("k", lease_seconds=5)
    assert first.owner is not None
    clock.advance(6)
    takeover = await store.begin("k", lease_seconds=5)
    assert takeover.state is IdempotencyState.ACQUIRED
    assert not await store.release("k", first.owner)  # old owner lost the lease
    assert takeover.owner is not None
    assert await store.release("k", takeover.owner)


async def test_event_bus_fans_out_to_every_consumer_group() -> None:
    bus = InMemoryEventBus()
    await bus.ensure_group("billing")
    await bus.ensure_group("growth")
    await bus.publish(Event.create("usage.recorded", source="/test", tenant_id="ten_1"))
    billing = await bus.read("billing", "b1")
    growth = await bus.read("growth", "g1")
    assert [d.event.type for d in billing] == ["usage.recorded"]
    assert [d.event.type for d in growth] == ["usage.recorded"]
    assert await bus.pending_count("billing") == 1
    await bus.ack(billing[0])
    assert await bus.pending_count("billing") == 0
    # A group created later only sees events published after it existed.
    await bus.ensure_group("late")
    assert await bus.read("late", "l1") == []


async def test_event_create_inherits_tenant_context() -> None:
    with use_tenant("ten_ctx"):
        event = Event.create("document.indexed", source="/services/knowledge", data={"n": 3})
    assert event.tenantid == "ten_ctx"
    assert event.specversion == "1.0"
    assert event.id.startswith("evt_")


def test_event_type_patterns() -> None:
    assert matches("document.indexed", ["document.*"])
    assert matches("agent.run.completed", ["agent.run.*", "x"])
    assert not matches("billing.plan.changed", ["document.*"])
    assert matches("anything", ["*"])
