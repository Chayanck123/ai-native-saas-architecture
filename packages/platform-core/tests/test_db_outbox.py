from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from platform_core.config import CoreSettings
from platform_core.context import use_tenant
from platform_core.db import TenantScoped, UtcDateTime
from platform_core.events import InMemoryEventBus
from platform_core.infrastructure import Infrastructure
from platform_core.service import ServiceRuntime


class Base(DeclarativeBase):
    pass


class Widget(TenantScoped, Base):
    __tablename__ = "widgets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


@pytest.fixture
async def runtime(infra: Infrastructure) -> AsyncIterator[ServiceRuntime]:
    settings = CoreSettings(service_name="widgets", database_url="sqlite+aiosqlite:///:memory:")
    runtime = ServiceRuntime.create(
        "widgets", settings, infra, metadata=Base.metadata, tenant_tables=["widgets"]
    )
    await runtime.start()
    yield runtime
    await runtime.stop()


def published_types(infra: Infrastructure) -> list[str]:
    assert isinstance(infra.event_bus, InMemoryEventBus)
    return [event.type for event in infra.event_bus.published]


async def test_datetimes_round_trip_as_aware_utc(runtime: ServiceRuntime) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    created = datetime(2026, 3, 1, 17, 30, tzinfo=ist)
    async with runtime.db.session() as session:
        session.add(Widget(id="w1", tenant_id="ten_1", created_at=created))
    async with runtime.db.session() as session:
        widget = (await session.execute(select(Widget))).scalar_one()
    assert widget.created_at == created
    assert widget.created_at.tzinfo == UTC


async def test_naive_datetimes_are_rejected(runtime: ServiceRuntime) -> None:
    with pytest.raises(Exception, match="naive"):
        async with runtime.db.session() as session:
            session.add(Widget(id="w2", tenant_id="t", created_at=datetime(2026, 1, 1)))


async def test_unit_of_work_publishes_only_after_commit(
    runtime: ServiceRuntime, infra: Infrastructure
) -> None:
    with use_tenant("ten_1"):
        async with runtime.unit_of_work() as uow:
            uow.session.add(Widget(id="w3", tenant_id="ten_1", created_at=datetime.now(UTC)))
            await uow.publish(runtime.event("widget.created", {"id": "w3"}))
            assert published_types(infra) == []  # nothing escapes before commit
    assert published_types(infra) == ["widget.created"]
    assert infra.event_bus.published[0].tenantid == "ten_1"  # type: ignore[attr-defined]


async def test_rolled_back_work_never_publishes(
    runtime: ServiceRuntime, infra: Infrastructure
) -> None:
    with pytest.raises(RuntimeError):
        async with runtime.unit_of_work() as uow:
            uow.session.add(Widget(id="w4", tenant_id="ten_1", created_at=datetime.now(UTC)))
            await uow.publish(runtime.event("widget.created", {"id": "w4"}))
            raise RuntimeError("business rule violated")
    assert published_types(infra) == []
    async with runtime.db.session() as session:
        assert (await session.execute(select(Widget))).first() is None


async def test_relay_recovers_events_left_behind_by_a_crash(
    runtime: ServiceRuntime, infra: Infrastructure
) -> None:
    # Simulate a crash between commit and publish: the row exists, nothing was sent.
    async with runtime.db.session() as session:
        await runtime.outbox.add(session, runtime.event("widget.created", {"id": "w5"}))
    assert published_types(infra) == []
    assert await runtime.outbox.relay() == 1
    assert published_types(infra) == ["widget.created"]
    assert await runtime.outbox.relay() == 0  # marked as published
