"""Background worker: turns events into tasks and executes tasks safely.

Each service runs one or more worker replicas next to its API. A worker:

1. **Consumes events** from the bus through the service's consumer group and
   converts the interesting ones into tasks on the service's own queue (keyed by
   the event id, so a redelivered event becomes a duplicate task, not duplicate work).
2. **Executes tasks** with:
   * an **idempotency guard** - a task that already completed is skipped;
   * the **tenant context** and **trace context** restored from the message;
   * **retries** with exponential backoff + full jitter for failures;
   * a **dead-letter queue** once retries are exhausted (or for permanent errors),
     plus a ``platform.task.dead_lettered`` event for alerting.
3. **Relays the outbox**, republishing events a crash left unpublished.

Scale out by running more replicas: queues and consumer groups share the load.
"""

from __future__ import annotations

import asyncio
import os
import random
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import SpanKind, Status, StatusCode

from platform_core.context import Principal, use_principal, use_request_id
from platform_core.events import Event, matches
from platform_core.idempotency import IdempotencyState
from platform_core.observability.tracing import context_from_traceparent
from platform_core.rbac import TENANT_PERMISSIONS
from platform_core.retry import RetryableError, RetryPolicy
from platform_core.service import ServiceRuntime
from platform_core.tasks import Reservation, Task

TaskHandler = Callable[[Task], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class TaskRegistration:
    name: str
    handler: TaskHandler
    retry_policy: RetryPolicy


@dataclass(frozen=True)
class EventRoute:
    patterns: tuple[str, ...]
    task_name: str


def event_of(task: Task) -> Event:
    """The event that triggered an event-routed task."""
    return Event.model_validate(task.payload["event"])


class Worker:
    def __init__(
        self,
        runtime: ServiceRuntime,
        *,
        queue: str | None = None,
        consumer: str | None = None,
        retry_policy: RetryPolicy | None = None,
        batch_size: int = 20,
        lease_seconds: float = 300.0,
        contention_delay: float = 5.0,
        dedupe_window_seconds: float = 86_400.0,
    ) -> None:
        self.runtime = runtime
        self.queue = queue or runtime.name
        self.group = runtime.name
        self.consumer = consumer or f"{runtime.name}-{socket.gethostname()}-{os.getpid()}"
        self._tasks: dict[str, TaskRegistration] = {}
        self._routes: list[EventRoute] = []
        self._default_policy = retry_policy or RetryPolicy()
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._contention_delay = contention_delay
        self._dedupe_window = dedupe_window_seconds
        self._stopping = asyncio.Event()
        self._rng = random.Random()

    # -- registration ----------------------------------------------------------
    def task(
        self, name: str, *, retry_policy: RetryPolicy | None = None
    ) -> Callable[[TaskHandler], TaskHandler]:
        def decorator(handler: TaskHandler) -> TaskHandler:
            self._tasks[name] = TaskRegistration(
                name, handler, retry_policy or self._default_policy
            )
            return handler

        return decorator

    def on_event(self, *patterns: str, task: str) -> None:
        self._routes.append(EventRoute(tuple(patterns), task))

    @property
    def task_names(self) -> list[str]:
        return sorted(self._tasks)

    async def setup(self) -> None:
        if self._routes:
            await self.runtime.infra.event_bus.ensure_group(self.group)

    # -- event consumption -----------------------------------------------------
    async def poll_events(self, *, block_ms: int = 0) -> int:
        if not self._routes:
            return 0
        bus = self.runtime.infra.event_bus
        deliveries = await bus.read(
            self.group, self.consumer, count=self._batch_size, block_ms=block_ms
        )
        for delivery in deliveries:
            event = delivery.event
            for route in self._routes:
                if matches(event.type, route.patterns):
                    await self._enqueue_for_event(route, event)
            await bus.ack(delivery)
        return len(deliveries)

    async def _enqueue_for_event(self, route: EventRoute, event: Event) -> None:
        # Inbox de-duplication: an event redelivered (or re-published by the outbox
        # relay) must not start a second task chain for this consumer group.
        idempotency = self.runtime.infra.idempotency
        key = f"inbox:{self.group}:{route.task_name}:{event.id}"
        lock = await idempotency.begin(key, lease_seconds=60)
        if lock.state is not IdempotencyState.ACQUIRED:
            self.runtime.metrics.events_duplicates.labels(self.runtime.name, event.type).inc()
            return
        assert lock.owner is not None
        task = Task(
            name=route.task_name,
            queue=self.queue,
            payload={"event": event.model_dump(mode="json")},
            tenant_id=event.tenantid,
            idempotency_key=event.id,
            traceparent=event.traceparent,
            created_at=self.runtime.clock.now(),
        )
        try:
            await self.runtime.infra.task_queue.enqueue(task)
        except Exception:
            await idempotency.release(key, lock.owner)
            raise
        await idempotency.complete(
            key, lock.owner, response={"task_id": task.id}, ttl_seconds=self._dedupe_window
        )
        self.runtime.metrics.events_consumed.labels(self.runtime.name, event.type).inc()

    # -- task execution ----------------------------------------------------------
    async def process_tasks(self, *, block_ms: int = 0) -> int:
        reservations = await self.runtime.infra.task_queue.reserve(
            self.queue, self.consumer, count=self._batch_size, block_ms=block_ms
        )
        for reservation in reservations:
            await self._execute(reservation)
        return len(reservations)

    async def _execute(self, reservation: Reservation) -> None:
        runtime = self.runtime
        queue = runtime.infra.task_queue
        task = reservation.task
        registration = self._tasks.get(task.name)
        if registration is None:
            await queue.dead_letter(
                self.queue, reservation, error=f"no handler registered for '{task.name}'"
            )
            self._count(task, "dead_lettered")
            return

        lock = await runtime.infra.idempotency.begin(
            task.dedupe_key, lease_seconds=self._lease_seconds
        )
        if lock.state is IdempotencyState.COMPLETED:
            await queue.ack(self.queue, reservation)
            self._count(task, "duplicate")
            return
        if lock.state is IdempotencyState.IN_PROGRESS:
            await queue.retry(
                self.queue,
                reservation,
                delay_seconds=self._contention_delay,
                error="already in progress on another worker",
                increment_attempt=False,
            )
            self._count(task, "deferred")
            return
        assert lock.owner is not None

        principal = (
            Principal.service(runtime.name, task.tenant_id, TENANT_PERMISSIONS)
            if task.tenant_id
            else Principal.system(runtime.name)
        )
        started = time.perf_counter()
        with (
            runtime.tracer.start_as_current_span(
                f"task {task.name}",
                context=context_from_traceparent(task.traceparent),
                kind=SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "ainative.tasks",
                    "messaging.destination.name": self.queue,
                    "messaging.message.id": task.id,
                    "task.name": task.name,
                    "task.attempt": task.attempt,
                    "tenant.id": task.tenant_id or "",
                },
            ) as span,
            use_principal(principal),
            use_request_id(task.id),
        ):
            try:
                result = await registration.handler(task)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                await runtime.infra.idempotency.release(task.dedupe_key, lock.owner)
                await self._handle_failure(reservation, registration, exc)
                return
            finally:
                runtime.metrics.task_latency.labels(runtime.name, self.queue, task.name).observe(
                    time.perf_counter() - started
                )
            await runtime.infra.idempotency.complete(
                task.dedupe_key, lock.owner, response=result or {}
            )
            await queue.ack(self.queue, reservation)
            self._count(task, "succeeded")

    async def _handle_failure(
        self, reservation: Reservation, registration: TaskRegistration, exc: Exception
    ) -> None:
        runtime = self.runtime
        task = reservation.task
        error = f"{type(exc).__name__}: {exc}"
        policy = registration.retry_policy
        if policy.should_retry(task.attempt, exc):
            delay = policy.backoff(task.attempt, self._rng)
            if isinstance(exc, RetryableError) and exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            await runtime.infra.task_queue.retry(
                self.queue, reservation, delay_seconds=delay, error=error
            )
            runtime.logger.warning(
                "task failed; retry scheduled",
                extra={
                    "task": task.name,
                    "task_id": task.id,
                    "attempt": task.attempt,
                    "retry_in_s": round(delay, 3),
                    "error": error,
                },
            )
            self._count(task, "retried")
            return

        await runtime.infra.task_queue.dead_letter(self.queue, reservation, error=error)
        runtime.logger.error(
            "task dead-lettered",
            extra={"task": task.name, "task_id": task.id, "attempt": task.attempt, "error": error},
        )
        self._count(task, "dead_lettered")
        await runtime.publish(
            runtime.event(
                "platform.task.dead_lettered",
                {
                    "queue": self.queue,
                    "task": task.name,
                    "task_id": task.id,
                    "attempts": task.attempt,
                    "error": error[:500],
                },
                subject=task.id,
                tenant_id=task.tenant_id,
            )
        )

    def _count(self, task: Task, outcome: str) -> None:
        self.runtime.metrics.tasks.labels(self.runtime.name, self.queue, task.name, outcome).inc()

    # -- loops ---------------------------------------------------------------------
    async def run_once(self, *, block_ms: int = 0) -> int:
        processed = await self.poll_events()
        processed += await self.process_tasks(block_ms=0 if processed else block_ms)
        return processed

    async def run_until_idle(self, *, max_rounds: int = 1000) -> int:
        """Process until no ready events or tasks remain (tests and the devstack)."""
        total = 0
        for _ in range(max_rounds):
            processed = await self.run_once()
            total += processed
            if processed == 0:
                break
        return total

    async def update_queue_metrics(self) -> None:
        stats = await self.runtime.infra.task_queue.stats(self.queue)
        gauge = self.runtime.metrics.queue_depth
        for state in ("ready", "delayed", "in_flight", "dead"):
            gauge.labels(self.runtime.name, self.queue, state).set(getattr(stats, state))

    async def run_forever(self, *, idle_block_ms: int = 1000) -> None:
        await self.setup()
        runtime = self.runtime
        runtime.logger.info("worker started", extra={"queue": self.queue, "tasks": self.task_names})
        last_housekeeping = 0.0
        while not self._stopping.is_set():
            try:
                await self.run_once(block_ms=idle_block_ms)
                if time.monotonic() - last_housekeeping > 10:
                    last_housekeeping = time.monotonic()
                    await runtime.outbox.relay(older_than_seconds=10)
                    await self.update_queue_metrics()
            except asyncio.CancelledError:
                raise
            except Exception:
                runtime.logger.exception("worker loop error; backing off")
                await asyncio.sleep(1.0)
        runtime.logger.info("worker stopped", extra={"queue": self.queue})

    def stop(self) -> None:
        self._stopping.set()
