"""Idempotency: doing something twice has the same effect as doing it once.

Networks retry, clients double-click and queues redeliver. Before executing a
side effect we *begin* it under a key:

* ``ACQUIRED``    - nobody has done this yet; we hold a short lease and proceed.
* ``IN_PROGRESS`` - someone else holds the lease right now; back off and retry later.
* ``COMPLETED``   - it already happened; return the stored result instead.

Leases expire, so a crashed worker cannot block a key forever, and every write
is checked against the lease *owner* so a slow worker cannot clobber the result
of the worker that took over.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from redis.asyncio import Redis

from platform_core.clock import Clock, SystemClock
from platform_core.ids import new_id


class IdempotencyState(StrEnum):
    ACQUIRED = "acquired"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(frozen=True)
class IdempotencyResult:
    state: IdempotencyState
    owner: str | None = None
    fingerprint: str | None = None
    response: dict[str, Any] | None = None


class IdempotencyStore(Protocol):
    async def begin(
        self, key: str, *, fingerprint: str = "", lease_seconds: float = 60.0
    ) -> IdempotencyResult: ...

    async def complete(
        self,
        key: str,
        owner: str,
        *,
        response: dict[str, Any] | None = None,
        ttl_seconds: float = 86_400.0,
    ) -> bool: ...

    async def release(self, key: str, owner: str) -> bool: ...


class InMemoryIdempotencyStore:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._records: dict[str, tuple[dict[str, Any], float]] = {}

    def _get(self, key: str) -> dict[str, Any] | None:
        entry = self._records.get(key)
        if entry is None:
            return None
        record, expires_at = entry
        if expires_at <= self._clock.time():
            del self._records[key]
            return None
        return record

    async def begin(
        self, key: str, *, fingerprint: str = "", lease_seconds: float = 60.0
    ) -> IdempotencyResult:
        record = self._get(key)
        if record is None:
            owner = new_id("lease")
            self._records[key] = (
                {"s": IdempotencyState.IN_PROGRESS.value, "o": owner, "f": fingerprint},
                self._clock.time() + lease_seconds,
            )
            return IdempotencyResult(
                IdempotencyState.ACQUIRED, owner=owner, fingerprint=fingerprint
            )
        return _result_from_record(record)

    async def complete(
        self,
        key: str,
        owner: str,
        *,
        response: dict[str, Any] | None = None,
        ttl_seconds: float = 86_400.0,
    ) -> bool:
        record = self._get(key)
        if record is None or record.get("o") != owner or record["s"] != "in_progress":
            return False
        self._records[key] = (
            {"s": IdempotencyState.COMPLETED.value, "f": record.get("f", ""), "r": response},
            self._clock.time() + ttl_seconds,
        )
        return True

    async def release(self, key: str, owner: str) -> bool:
        record = self._get(key)
        if record is None or record.get("o") != owner or record["s"] != "in_progress":
            return False
        del self._records[key]
        return True


_BEGIN = """
local current = redis.call('GET', KEYS[1])
if current then return current end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
return false
"""

_COMPLETE = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local record = cjson.decode(current)
if record['s'] ~= 'in_progress' or record['o'] ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
return 1
"""

_RELEASE = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local record = cjson.decode(current)
if record['s'] ~= 'in_progress' or record['o'] ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


class RedisIdempotencyStore:
    """Redis-backed store; every transition is a single atomic Lua script."""

    def __init__(self, redis: Redis, *, prefix: str = "ainative") -> None:
        self._redis = redis
        self._prefix = prefix
        self._begin = redis.register_script(_BEGIN)
        self._complete = redis.register_script(_COMPLETE)
        self._release = redis.register_script(_RELEASE)

    def _key(self, key: str) -> str:
        return f"{self._prefix}:idem:{key}"

    async def begin(
        self, key: str, *, fingerprint: str = "", lease_seconds: float = 60.0
    ) -> IdempotencyResult:
        owner = new_id("lease")
        record = json.dumps({"s": "in_progress", "o": owner, "f": fingerprint})
        current = await self._begin(keys=[self._key(key)], args=[record, int(lease_seconds * 1000)])
        if not current:
            return IdempotencyResult(
                IdempotencyState.ACQUIRED, owner=owner, fingerprint=fingerprint
            )
        return _result_from_record(json.loads(current))

    async def complete(
        self,
        key: str,
        owner: str,
        *,
        response: dict[str, Any] | None = None,
        ttl_seconds: float = 86_400.0,
    ) -> bool:
        current = await self._redis.get(self._key(key))
        fingerprint = json.loads(current).get("f", "") if current else ""
        record = json.dumps({"s": "completed", "f": fingerprint, "r": response})
        done = await self._complete(
            keys=[self._key(key)], args=[owner, record, int(ttl_seconds * 1000)]
        )
        return bool(done)

    async def release(self, key: str, owner: str) -> bool:
        return bool(await self._release(keys=[self._key(key)], args=[owner]))


def _result_from_record(record: dict[str, Any]) -> IdempotencyResult:
    if record.get("s") == IdempotencyState.COMPLETED.value:
        return IdempotencyResult(
            IdempotencyState.COMPLETED, fingerprint=record.get("f"), response=record.get("r")
        )
    return IdempotencyResult(IdempotencyState.IN_PROGRESS, fingerprint=record.get("f"))
