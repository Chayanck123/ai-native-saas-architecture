"""Event-driven backbone: CloudEvents on a publish/subscribe bus.

Services never call each other to say "something happened". They publish an
immutable event (``document.indexed``, ``usage.recorded`` ...) and every
interested service reacts in its own time. Each service reads through its own
*consumer group*, so:

* every service sees every event (fan-out), and
* replicas of the same service share the work (horizontal scaling).

Two interchangeable backends implement the same pull-based interface:
:class:`InMemoryEventBus` (tests and the single-process devstack) and
:class:`RedisStreamsEventBus` (production).
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from platform_core.clock import utcnow
from platform_core.context import current_tenant_id
from platform_core.ids import new_id
from platform_core.observability.tracing import current_traceparent


class Event(BaseModel):
    """A CloudEvents 1.0 envelope (structured JSON mode).

    ``tenantid`` and ``traceparent`` are CloudEvents extension attributes: the
    first keeps multi-tenancy explicit on every message, the second implements the
    CloudEvents *Distributed Tracing* extension.
    """

    model_config = ConfigDict(frozen=True)

    specversion: Literal["1.0"] = "1.0"
    id: str = Field(default_factory=lambda: new_id("evt"))
    source: str
    type: str
    subject: str | None = None
    time: datetime = Field(default_factory=utcnow)
    datacontenttype: str = "application/json"
    tenantid: str | None = None
    traceparent: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        type: str,
        *,
        source: str,
        data: dict[str, Any] | None = None,
        subject: str | None = None,
        tenant_id: str | None = None,
        event_id: str | None = None,
        time: datetime | None = None,
    ) -> Event:
        """Build an event, inheriting tenant and trace context from the caller."""
        values: dict[str, Any] = {
            "type": type,
            "source": source,
            "data": data or {},
            "subject": subject,
            "tenantid": tenant_id if tenant_id is not None else current_tenant_id(),
            "traceparent": current_traceparent(),
        }
        if event_id:
            values["id"] = event_id
        if time:
            values["time"] = time
        return cls(**values)


def matches(event_type: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(event_type, pattern) for pattern in patterns)


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    group: str
    event: Event


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

    async def ensure_group(self, group: str) -> None: ...

    async def read(
        self, group: str, consumer: str, *, count: int = 50, block_ms: int = 0
    ) -> list[Delivery]: ...

    async def ack(self, delivery: Delivery) -> None: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# In-memory implementation
# --------------------------------------------------------------------------- #
@dataclass
class _Group:
    offset: int
    pending: dict[str, Event] = field(default_factory=dict)


class InMemoryEventBus:
    """An append-only in-process log with consumer groups (Redis Streams semantics)."""

    def __init__(self) -> None:
        self._log: list[Event] = []
        self._groups: dict[str, _Group] = {}
        self._new_event = asyncio.Condition()

    @property
    def published(self) -> list[Event]:
        return list(self._log)

    async def publish(self, event: Event) -> None:
        async with self._new_event:
            self._log.append(event)
            self._new_event.notify_all()

    async def ensure_group(self, group: str) -> None:
        # Like ``XGROUP CREATE ... $``: a new group only sees events published after it.
        self._groups.setdefault(group, _Group(offset=len(self._log)))

    async def read(
        self, group: str, consumer: str, *, count: int = 50, block_ms: int = 0
    ) -> list[Delivery]:
        await self.ensure_group(group)
        state = self._groups[group]
        if state.offset >= len(self._log) and block_ms > 0:
            async with self._new_event:
                try:
                    await asyncio.wait_for(
                        self._new_event.wait_for(lambda: state.offset < len(self._log)),
                        timeout=block_ms / 1000,
                    )
                except TimeoutError:
                    return []
        batch = self._log[state.offset : state.offset + count]
        deliveries = []
        for index, event in enumerate(batch, start=state.offset):
            delivery_id = str(index)
            state.pending[delivery_id] = event
            deliveries.append(Delivery(delivery_id=delivery_id, group=group, event=event))
        state.offset += len(batch)
        return deliveries

    async def ack(self, delivery: Delivery) -> None:
        self._groups[delivery.group].pending.pop(delivery.delivery_id, None)

    async def pending_count(self, group: str) -> int:
        return len(self._groups[group].pending) if group in self._groups else 0

    async def lag(self, group: str) -> int:
        state = self._groups.get(group)
        return 0 if state is None else len(self._log) - state.offset

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Redis Streams implementation
# --------------------------------------------------------------------------- #
class RedisStreamsEventBus:
    """Durable event bus on a Redis Stream with one consumer group per service.

    * ``XADD ... MAXLEN ~ N`` bounds memory while keeping recent history.
    * ``XREADGROUP`` load-balances events across replicas of a service.
    * ``XAUTOCLAIM`` hands events held by a crashed replica to a live one, so an
      event is never lost - it may be delivered twice, which is why every handler
      is idempotent.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        stream: str = "ainative:events",
        maxlen: int = 100_000,
        claim_idle_ms: int = 60_000,
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen
        self._claim_idle_ms = claim_idle_ms
        self._known_groups: set[str] = set()

    async def publish(self, event: Event) -> None:
        await self._redis.xadd(
            self._stream,
            {"event": event.model_dump_json()},
            maxlen=self._maxlen,
            approximate=True,
        )

    async def ensure_group(self, group: str) -> None:
        if group in self._known_groups:
            return
        try:
            await self._redis.xgroup_create(self._stream, group, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._known_groups.add(group)

    async def read(
        self, group: str, consumer: str, *, count: int = 50, block_ms: int = 0
    ) -> list[Delivery]:
        await self.ensure_group(group)
        deliveries: list[Delivery] = []
        # 1) Recover events abandoned by crashed consumers.
        claimed = await self._redis.xautoclaim(
            self._stream,
            group,
            consumer,
            min_idle_time=self._claim_idle_ms,
            start_id="0-0",
            count=count,
        )
        deliveries.extend(self._to_deliveries(group, claimed[1]))
        # 2) Read new events.
        remaining = count - len(deliveries)
        if remaining > 0:
            response = await self._redis.xreadgroup(
                group,
                consumer,
                {self._stream: ">"},
                count=remaining,
                block=block_ms if block_ms > 0 else None,
            )
            for _stream, entries in cast(Any, response) or []:
                deliveries.extend(self._to_deliveries(group, entries))
        return deliveries

    def _to_deliveries(self, group: str, entries: Any) -> list[Delivery]:
        deliveries = []
        for entry_id, fields in entries:
            if not fields:  # entry trimmed from the stream while pending
                continue
            raw = fields.get(b"event") or fields.get("event")
            event = Event.model_validate_json(raw)
            deliveries.append(Delivery(delivery_id=_decode(entry_id), group=group, event=event))
        return deliveries

    async def ack(self, delivery: Delivery) -> None:
        await self._redis.xack(self._stream, delivery.group, delivery.delivery_id)

    async def close(self) -> None:
        return None


def _decode(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value
