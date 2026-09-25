from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from platform_core.clock import ManualClock
from platform_core.infrastructure import Infrastructure


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def infra(clock: ManualClock) -> Infrastructure:
    return Infrastructure.in_memory(clock)


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL not set")
    client = Redis.from_url(url)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()
