from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import MetaData

from platform_core.clock import ManualClock
from platform_core.config import CoreSettings
from platform_core.context import current_principal, current_tenant_id
from platform_core.events import InMemoryEventBus
from platform_core.infrastructure import Infrastructure
from platform_core.retry import PermanentError, RetryableError, RetryPolicy
from platform_core.service import ServiceRuntime
from platform_core.tasks import Task
from platform_core.testing import SpanRecorder
from platform_core.worker import Worker, event_of


@pytest.fixture
def recorder() -> SpanRecorder:
    return SpanRecorder()


@pytest.fixture
async def runtime(infra: Infrastructure, recorder: SpanRecorder) -> AsyncIterator[ServiceRuntime]:
    settings = CoreSettings(service_name="demo", database_url="sqlite+aiosqlite:///:memory:")
    runtime = ServiceRuntime.create(
        "demo", settings, infra, metadata=MetaData(), span_exporter=recorder.exporter
    )
    await runtime.start()
    yield runtime
    await runtime.stop()


def _outcomes(runtime: ServiceRuntime, outcome: str) -> float:
    value = runtime.metrics.registry.get_sample_value(
        "tasks_processed_total",
        {"service": "demo", "queue": "demo", "task": "demo.handle", "outcome": outcome},
    )
    return value or 0.0


async def test_events_become_tasks_with_tenant_and_trace_context(
    runtime: ServiceRuntime, recorder: SpanRecorder
) -> None:
    worker = Worker(runtime)
    seen: list[tuple[str | None, dict[str, Any]]] = []

    @worker.task("demo.handle")
    async def handle(task: Task) -> dict[str, Any]:
        principal = current_principal()
        assert principal is not None and principal.tenant_id == "ten_1"
        seen.append((current_tenant_id(), event_of(task).data))
        return {"ok": True}

    worker.on_event("document.*", task="demo.handle")
    await worker.setup()

    with runtime.tracer.start_as_current_span("POST /v1/documents") as request_span:
        trace_id = request_span.get_span_context().trace_id
        await runtime.publish(runtime.event("document.indexed", {"doc": "d1"}, tenant_id="ten_1"))
    await runtime.publish(runtime.event("billing.plan.changed", {}, tenant_id="ten_1"))

    await worker.run_until_idle()

    assert seen == [("ten_1", {"doc": "d1"})]
    [task_span] = recorder.spans("task demo.handle")
    assert task_span.context is not None
    assert task_span.context.trace_id == trace_id  # one trace across the async hop
    assert task_span.attributes is not None
    assert task_span.attributes["tenant.id"] == "ten_1"


async def test_duplicate_event_delivery_executes_once(runtime: ServiceRuntime) -> None:
    worker = Worker(runtime)
    calls = 0

    @worker.task("demo.handle")
    async def handle(task: Task) -> None:
        nonlocal calls
        calls += 1

    worker.on_event("usage.recorded", task="demo.handle")
    await worker.setup()
    event = runtime.event("usage.recorded", {"tokens": 10}, tenant_id="ten_1")
    await runtime.publish(event)
    await runtime.publish(event)  # at-least-once delivery: same event arrives twice

    await worker.run_until_idle()
    assert calls == 1
    assert _outcomes(runtime, "succeeded") == 1
    duplicates = runtime.metrics.registry.get_sample_value(
        "events_duplicates_total", {"service": "demo", "type": "usage.recorded"}
    )
    assert duplicates == 1


async def test_redelivered_task_is_skipped_after_success(
    runtime: ServiceRuntime, infra: Infrastructure
) -> None:
    worker = Worker(runtime)
    calls = 0

    @worker.task("demo.handle")
    async def handle(task: Task) -> None:
        nonlocal calls
        calls += 1

    task = await runtime.enqueue("demo.handle", tenant_id="ten_1", idempotency_key="charge-1")
    await worker.run_until_idle()
    await infra.task_queue.enqueue(task)  # the queue redelivers the same task
    await worker.run_until_idle()
    assert calls == 1
    assert _outcomes(runtime, "duplicate") == 1


async def test_transient_failures_retry_with_backoff(
    runtime: ServiceRuntime, infra: Infrastructure, clock: ManualClock
) -> None:
    worker = Worker(runtime)
    attempts: list[int] = []

    @worker.task("demo.flaky", retry_policy=RetryPolicy(max_attempts=5, base_delay=1, max_delay=8))
    async def flaky(task: Task) -> dict[str, Any]:
        attempts.append(task.attempt)
        if task.attempt < 3:
            raise RetryableError("dependency temporarily unavailable")
        return {"done": True}

    await runtime.enqueue("demo.flaky", {"x": 1}, tenant_id="ten_1")
    await worker.run_until_idle()
    assert attempts == [1]
    assert (await infra.task_queue.stats("demo")).delayed == 1

    clock.advance(10)
    await worker.run_until_idle()
    clock.advance(10)
    await worker.run_until_idle()
    assert attempts == [1, 2, 3]
    stats = await infra.task_queue.stats("demo")
    assert (stats.ready, stats.delayed, stats.in_flight, stats.dead) == (0, 0, 0, 0)


async def test_permanent_errors_are_dead_lettered_and_announced(
    runtime: ServiceRuntime, infra: Infrastructure
) -> None:
    worker = Worker(runtime)

    @worker.task("demo.bad")
    async def bad(task: Task) -> None:
        raise PermanentError("document is not valid UTF-8")

    await runtime.enqueue("demo.bad", tenant_id="ten_1")
    await worker.run_until_idle()

    [letter] = await infra.task_queue.dead_letters("demo")
    assert "not valid UTF-8" in letter.error
    assert isinstance(infra.event_bus, InMemoryEventBus)
    alerts = [e for e in infra.event_bus.published if e.type == "platform.task.dead_lettered"]
    assert alerts and alerts[0].tenantid == "ten_1"


async def test_exhausted_retries_dead_letter_then_redrive_succeeds(
    runtime: ServiceRuntime, infra: Infrastructure, clock: ManualClock
) -> None:
    worker = Worker(runtime)
    healthy = False

    @worker.task("demo.sync", retry_policy=RetryPolicy(max_attempts=2, base_delay=1, max_delay=1))
    async def sync(task: Task) -> None:
        if not healthy:
            raise RetryableError("partner API down")

    await runtime.enqueue("demo.sync", tenant_id="ten_1")
    await worker.run_until_idle()
    clock.advance(5)
    await worker.run_until_idle()
    [letter] = await infra.task_queue.dead_letters("demo")
    assert letter.task.attempt == 2

    healthy = True  # the partner recovered; an operator redrives the message
    await infra.task_queue.redrive("demo", letter.id)
    await worker.run_until_idle()
    assert await infra.task_queue.dead_letters("demo") == []


async def test_task_held_by_another_worker_is_deferred_not_failed(
    runtime: ServiceRuntime, infra: Infrastructure, clock: ManualClock
) -> None:
    worker = Worker(runtime, contention_delay=5)
    calls = 0

    @worker.task("demo.handle")
    async def handle(task: Task) -> None:
        nonlocal calls
        calls += 1

    task = await runtime.enqueue("demo.handle", tenant_id="ten_1", idempotency_key="k1")
    other = await infra.idempotency.begin(task.dedupe_key, lease_seconds=3)  # another replica
    await worker.run_until_idle()
    assert calls == 0
    assert _outcomes(runtime, "deferred") == 1

    clock.advance(6)  # the other replica's lease lapsed (it crashed)
    assert other.state.value == "acquired"
    await worker.run_until_idle()
    assert calls == 1


async def test_unknown_task_goes_straight_to_dlq(
    runtime: ServiceRuntime, infra: Infrastructure
) -> None:
    worker = Worker(runtime)
    await runtime.enqueue("demo.nobody-handles-this", tenant_id="ten_1")
    await worker.run_until_idle()
    [letter] = await infra.task_queue.dead_letters("demo")
    assert "no handler" in letter.error
