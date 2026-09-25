from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from billing_service.app import BillingServiceApp, build_billing
from billing_service.models import Base
from billing_service.settings import BillingSettings
from platform_core.clock import ManualClock
from platform_core.config import CoreSettings
from platform_core.events import InMemoryEventBus
from platform_core.ids import new_id
from platform_core.infrastructure import Infrastructure


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def infra(clock: ManualClock) -> Infrastructure:
    return Infrastructure.in_memory(clock)


def _database_urls() -> list[object]:
    postgres = os.environ.get("DATABASE_URL", "")
    return [
        pytest.param("sqlite+aiosqlite:///:memory:", id="sqlite"),
        pytest.param(
            postgres,
            id="postgres",
            marks=[
                pytest.mark.integration,
                pytest.mark.skipif(
                    not postgres.startswith("postgresql"), reason="DATABASE_URL not set"
                ),
            ],
        ),
    ]


@pytest.fixture(params=_database_urls())
async def billing(
    request: pytest.FixtureRequest, infra: Infrastructure
) -> AsyncIterator[BillingServiceApp]:
    settings = BillingSettings(
        core=CoreSettings(service_name="billing", database_url=str(request.param))
    )
    built = build_billing(settings, infra)
    await built.runtime.start()
    await built.worker.setup()
    yield built
    if built.runtime.db.is_postgres:
        async with built.runtime.db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await built.runtime.stop()


@pytest.fixture
def tenant_id() -> str:
    return new_id("ten")


async def deliver(
    built: BillingServiceApp, event_type: str, data: dict[str, Any], tenant_id: str | None
) -> None:
    """Publish an event onto the bus and let the billing worker react to it."""
    await built.runtime.publish(built.runtime.event(event_type, data, tenant_id=tenant_id))
    await built.worker.run_until_idle()


def published(infra: Infrastructure, event_type: str) -> list[dict[str, Any]]:
    assert isinstance(infra.event_bus, InMemoryEventBus)
    return [e.data for e in infra.event_bus.published if e.type == event_type]
