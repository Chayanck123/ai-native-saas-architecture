"""Asynchronous task queues with delayed retries and dead-letter queues.

Slow or failure-prone work (indexing a document, running an agent, delivering a
webhook) never happens inside an HTTP request. The request enqueues a
:class:`Task` and returns immediately; a worker picks it up later.

Delivery is **at-least-once**: a task can be delivered twice (for example when a
worker crashes after doing the work but before acknowledging it). Workers pair
this with an idempotency store (see :mod:`platform_core.idempotency`) so the
*effect* happens exactly once.

Lifecycle of a task::

    enqueue --> ready --reserve--> in-flight --ack--> done
                  ^                    |
                  |                    +--retry(delay)--> delayed --(due)--> ready
                  |                    +--dead_letter--> DLQ --redrive--+
                  +---------------------------------------------------------+
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from platform_core.clock import Clock, SystemClock, utcnow
from platform_core.errors import NotFound
from platform_core.ids import new_id


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    name: str
    queue: str
    payload: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None
    idempotency_key: str | None = None
    attempt: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    traceparent: str | None = None
    last_error: str | None = None

    @property
    def dedupe_key(self) -> str:
        return f"task:{self.name}:{self.idempotency_key or self.id}"


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    task: Task


@dataclass(frozen=True)
class DeadLetter:
    id: str
    task: Task
    error: str
    failed_at: datetime


@dataclass(frozen=True)
class QueueStats:
    ready: int
    delayed: int
    in_flight: int
    dead: int


class TaskQueue(Protocol):
    async def enqueue(self, task: Task, *, delay_seconds: float = 0.0) -> None: ...

    async def reserve(
        self, queue: str, consumer: str, *, count: int = 10, block_ms: int = 0
    ) -> list[Reservation]: ...

    async def ack(self, queue: str, reservation: Reservation) -> None: ...

    async def retry(
        self,
        queue: str,
        reservation: Reservation,
        *,
        delay_seconds: float,
        error: str,
        increment_attempt: bool = True,
    ) -> None: ...

    async def dead_letter(self, queue: str, reservation: Reservation, *, error: str) -> None: ...

    async def dead_letters(self, queue: str, limit: int = 100) -> list[DeadLetter]: ...

    async def redrive(self, queue: str, dead_letter_id: str) -> Task: ...

    async def stats(self, queue: str) -> QueueStats: ...

    async def close(self) -> None: ...


def _next_attempt(task: Task, error: str, increment: bool = True) -> Task:
    attempt = task.attempt + 1 if increment else task.attempt
    return task.model_copy(update={"attempt": attempt, "last_error": error[:2000]})


def _fresh_copy(task: Task) -> Task:
    return task.model_copy(update={"attempt": 1, "last_error": None})


# --------------------------------------------------------------------------- #
# In-memory implementation
# --------------------------------------------------------------------------- #
class InMemoryTaskQueue:
    """Process-local queue used by tests and the single-process devstack.

    Honours delays through an injectable :class:`Clock`, so tests can "fast
    forward" through exponential backoff instantly.
    """

    def __init__(self, clock: Clock | None = None, *, visibility_timeout: float = 300.0):
        self._clock = clock or SystemClock()
        self._visibility_timeout = visibility_timeout
        self._ready: dict[str, deque[Task]] = {}
        self._delayed: dict[str, list[tuple[float, int, Task]]] = {}
        self._in_flight: dict[str, dict[str, tuple[Task, float]]] = {}
        self._dead: dict[str, list[DeadLetter]] = {}
        self._seq = itertools.count()
        self._wakeup = asyncio.Event()

    async def enqueue(self, task: Task, *, delay_seconds: float = 0.0) -> None:
        if delay_seconds > 0:
            due = self._clock.time() + delay_seconds
            heapq.heappush(self._delayed.setdefault(task.queue, []), (due, next(self._seq), task))
        else:
            self._ready.setdefault(task.queue, deque()).append(task)
            self._wakeup.set()

    def _promote(self, queue: str) -> None:
        now = self._clock.time()
        delayed = self._delayed.get(queue, [])
        ready = self._ready.setdefault(queue, deque())
        while delayed and delayed[0][0] <= now:
            ready.append(heapq.heappop(delayed)[2])
        # Visibility timeout: tasks held too long by a (presumably dead) worker return.
        in_flight = self._in_flight.get(queue, {})
        for reservation_id, (task, reserved_at) in list(in_flight.items()):
            if now - reserved_at >= self._visibility_timeout:
                del in_flight[reservation_id]
                ready.append(task)

    async def reserve(
        self, queue: str, consumer: str, *, count: int = 10, block_ms: int = 0
    ) -> list[Reservation]:
        self._promote(queue)
        ready = self._ready.setdefault(queue, deque())
        if not ready and block_ms > 0:
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=block_ms / 1000)
            except TimeoutError:
                return []
            self._promote(queue)
        reservations: list[Reservation] = []
        in_flight = self._in_flight.setdefault(queue, {})
        while ready and len(reservations) < count:
            task = ready.popleft()
            reservation_id = new_id("rsv")
            in_flight[reservation_id] = (task, self._clock.time())
            reservations.append(Reservation(reservation_id=reservation_id, task=task))
        return reservations

    async def ack(self, queue: str, reservation: Reservation) -> None:
        self._in_flight.get(queue, {}).pop(reservation.reservation_id, None)

    async def retry(
        self,
        queue: str,
        reservation: Reservation,
        *,
        delay_seconds: float,
        error: str,
        increment_attempt: bool = True,
    ) -> None:
        await self.ack(queue, reservation)
        task = _next_attempt(reservation.task, error, increment_attempt)
        await self.enqueue(task, delay_seconds=delay_seconds)

    async def dead_letter(self, queue: str, reservation: Reservation, *, error: str) -> None:
        await self.ack(queue, reservation)
        task = reservation.task.model_copy(update={"last_error": error[:2000]})
        self._dead.setdefault(queue, []).append(
            DeadLetter(id=new_id("dlq"), task=task, error=error, failed_at=self._clock.now())
        )

    async def dead_letters(self, queue: str, limit: int = 100) -> list[DeadLetter]:
        return list(reversed(self._dead.get(queue, [])))[:limit]

    async def redrive(self, queue: str, dead_letter_id: str) -> Task:
        letters = self._dead.get(queue, [])
        for index, letter in enumerate(letters):
            if letter.id == dead_letter_id:
                del letters[index]
                task = _fresh_copy(letter.task)
                await self.enqueue(task)
                return task
        raise NotFound(f"dead letter '{dead_letter_id}' not found")

    async def stats(self, queue: str) -> QueueStats:
        return QueueStats(
            ready=len(self._ready.get(queue, ())),
            delayed=len(self._delayed.get(queue, ())),
            in_flight=len(self._in_flight.get(queue, {})),
            dead=len(self._dead.get(queue, ())),
        )

    def next_due_in(self, queue: str) -> float | None:
        """Seconds until the next delayed task is due (``None`` if none are waiting)."""
        delayed = self._delayed.get(queue)
        if not delayed:
            return None
        return max(0.0, delayed[0][0] - self._clock.time())

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Redis implementation
# --------------------------------------------------------------------------- #
# Atomically move due tasks from the delayed sorted set onto the ready stream.
_PROMOTE_DUE = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, member in ipairs(due) do
  redis.call('ZREM', KEYS[1], member)
  redis.call('XADD', KEYS[2], '*', 'task', member)
end
return #due
"""

_GROUP = "workers"


class RedisTaskQueue:
    """Redis Streams work queue.

    Keys per queue (``{queue}`` is a Redis Cluster hash tag, so all keys of one
    queue live on the same shard and the Lua script can touch them atomically)::

        <prefix>:q:{name}          stream      ready + in-flight tasks
        <prefix>:q:{name}:delayed  sorted set  retries waiting for their backoff
        <prefix>:q:{name}:dlq      stream      dead letters

    In-flight tasks sit in the consumer group's pending list. If a worker dies,
    ``XAUTOCLAIM`` gives its tasks to another worker after ``visibility_timeout``.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = "ainative",
        visibility_timeout: float = 300.0,
        clock: Clock | None = None,
        dlq_maxlen: int = 10_000,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._visibility_ms = int(visibility_timeout * 1000)
        self._clock = clock or SystemClock()
        self._dlq_maxlen = dlq_maxlen
        self._promote = redis.register_script(_PROMOTE_DUE)
        self._groups_ready: set[str] = set()

    def _stream(self, queue: str) -> str:
        return f"{self._prefix}:q:{{{queue}}}"

    def _delayed_key(self, queue: str) -> str:
        return f"{self._stream(queue)}:delayed"

    def _dlq(self, queue: str) -> str:
        return f"{self._stream(queue)}:dlq"

    async def _ensure_group(self, queue: str) -> None:
        if queue in self._groups_ready:
            return
        try:
            # id="0": tasks enqueued before the first worker started are still processed.
            await self._redis.xgroup_create(self._stream(queue), _GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups_ready.add(queue)

    async def enqueue(self, task: Task, *, delay_seconds: float = 0.0) -> None:
        body = task.model_dump_json()
        if delay_seconds > 0:
            due_ms = int((self._clock.time() + delay_seconds) * 1000)
            await self._redis.zadd(self._delayed_key(task.queue), {body: due_ms})
        else:
            await self._redis.xadd(self._stream(task.queue), {"task": body})

    async def reserve(
        self, queue: str, consumer: str, *, count: int = 10, block_ms: int = 0
    ) -> list[Reservation]:
        await self._ensure_group(queue)
        stream = self._stream(queue)
        now_ms = int(self._clock.time() * 1000)
        await self._promote(keys=[self._delayed_key(queue), stream], args=[now_ms, 100])

        reservations: list[Reservation] = []
        claimed = await self._redis.xautoclaim(
            stream,
            _GROUP,
            consumer,
            min_idle_time=self._visibility_ms,
            start_id="0-0",
            count=count,
        )
        reservations.extend(_parse_entries(claimed[1]))

        remaining = count - len(reservations)
        if remaining > 0:
            response = await self._redis.xreadgroup(
                _GROUP,
                consumer,
                {stream: ">"},
                count=remaining,
                block=block_ms if block_ms > 0 else None,
            )
            for _stream, entries in cast(Any, response) or []:
                reservations.extend(_parse_entries(entries))
        return reservations

    async def ack(self, queue: str, reservation: Reservation) -> None:
        stream = self._stream(queue)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xack(stream, _GROUP, reservation.reservation_id)
            pipe.xdel(stream, reservation.reservation_id)
            await pipe.execute()

    async def retry(
        self,
        queue: str,
        reservation: Reservation,
        *,
        delay_seconds: float,
        error: str,
        increment_attempt: bool = True,
    ) -> None:
        stream = self._stream(queue)
        task = _next_attempt(reservation.task, error, increment_attempt)
        due_ms = int((self._clock.time() + delay_seconds) * 1000)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xack(stream, _GROUP, reservation.reservation_id)
            pipe.xdel(stream, reservation.reservation_id)
            pipe.zadd(self._delayed_key(queue), {task.model_dump_json(): due_ms})
            await pipe.execute()

    async def dead_letter(self, queue: str, reservation: Reservation, *, error: str) -> None:
        stream = self._stream(queue)
        task = reservation.task.model_copy(update={"last_error": error[:2000]})
        fields: dict[Any, Any] = {
            "task": task.model_dump_json(),
            "error": error[:2000],
            "failed_at": self._clock.now().isoformat(),
        }
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xack(stream, _GROUP, reservation.reservation_id)
            pipe.xdel(stream, reservation.reservation_id)
            pipe.xadd(self._dlq(queue), fields, maxlen=self._dlq_maxlen, approximate=True)
            await pipe.execute()

    async def dead_letters(self, queue: str, limit: int = 100) -> list[DeadLetter]:
        entries = cast(Any, await self._redis.xrevrange(self._dlq(queue), count=limit))
        return [_parse_dead_letter(entry_id, fields) for entry_id, fields in entries]

    async def redrive(self, queue: str, dead_letter_id: str) -> Task:
        entries = cast(
            Any, await self._redis.xrange(self._dlq(queue), dead_letter_id, dead_letter_id)
        )
        if not entries:
            raise NotFound(f"dead letter '{dead_letter_id}' not found")
        letter = _parse_dead_letter(*entries[0])
        task = _fresh_copy(letter.task)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xdel(self._dlq(queue), dead_letter_id)
            pipe.xadd(self._stream(queue), {"task": task.model_dump_json()})
            await pipe.execute()
        return task

    async def stats(self, queue: str) -> QueueStats:
        await self._ensure_group(queue)
        stream = self._stream(queue)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.xlen(stream)
            pipe.xpending(stream, _GROUP)
            pipe.zcard(self._delayed_key(queue))
            pipe.xlen(self._dlq(queue))
            length, pending, delayed, dead = await pipe.execute()
        in_flight = int(pending["pending"]) if isinstance(pending, dict) else int(pending[0])
        return QueueStats(
            ready=max(0, int(length) - in_flight),
            delayed=int(delayed),
            in_flight=in_flight,
            dead=int(dead),
        )

    async def close(self) -> None:
        return None


def _field(fields: dict[Any, Any], name: str) -> str:
    value = fields.get(name.encode(), fields.get(name))
    if value is None:
        raise KeyError(name)
    return value.decode() if isinstance(value, bytes) else str(value)


def _parse_entries(entries: Any) -> list[Reservation]:
    reservations = []
    for entry_id, fields in entries:
        if not fields:  # deleted while pending
            continue
        task = Task.model_validate_json(_field(fields, "task"))
        rid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
        reservations.append(Reservation(reservation_id=rid, task=task))
    return reservations


def _parse_dead_letter(entry_id: Any, fields: dict[Any, Any]) -> DeadLetter:
    return DeadLetter(
        id=entry_id.decode() if isinstance(entry_id, bytes) else entry_id,
        task=Task.model_validate_json(_field(fields, "task")),
        error=_field(fields, "error"),
        failed_at=datetime.fromisoformat(_field(fields, "failed_at")),
    )
