"""Transactional outbox: never lose an event, never publish a lie.

The naive approach - commit to the database, *then* publish the event - loses
the event if the process dies in between, and publishing *before* committing can
announce something that is then rolled back. Instead the event is written to an
``event_outbox`` table **in the same transaction** as the business change. After
the commit it is published and marked as sent; a relay loop republishes anything
left behind by a crash. Consumers de-duplicate by event id, so the occasional
double publish is harmless.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import Column, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.clock import Clock
from platform_core.context import Principal, use_principal
from platform_core.db import Database, UtcDateTime
from platform_core.events import Event, EventBus

logger = logging.getLogger(__name__)


def outbox_table(metadata: MetaData) -> Table:
    return Table(
        "event_outbox",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("event_type", String(128), nullable=False),
        Column("payload", Text, nullable=False),
        Column("created_at", UtcDateTime, nullable=False, index=True),
        Column("published_at", UtcDateTime, nullable=True, index=True),
    )


class Outbox:
    def __init__(
        self, service: str, db: Database, table: Table, bus: EventBus, clock: Clock
    ) -> None:
        self._service = service
        self._db = db
        self._table = table
        self._bus = bus
        self._clock = clock

    async def add(self, session: AsyncSession, event: Event) -> Event:
        await session.execute(
            insert(self._table).values(
                id=event.id,
                event_type=event.type,
                payload=event.model_dump_json(),
                created_at=self._clock.now(),
                published_at=None,
            )
        )
        return event

    async def relay(
        self,
        event_ids: Iterable[str] | None = None,
        *,
        older_than_seconds: float = 0.0,
        limit: int = 200,
    ) -> int:
        """Publish unpublished outbox rows; returns how many were published."""
        table = self._table
        query = select(table.c.id, table.c.payload).where(table.c.published_at.is_(None))
        if event_ids is not None:
            ids = list(event_ids)
            if not ids:
                return 0
            query = query.where(table.c.id.in_(ids))
        if older_than_seconds > 0:
            cutoff = self._clock.now() - timedelta(seconds=older_than_seconds)
            query = query.where(table.c.created_at <= cutoff)
        query = query.order_by(table.c.created_at).limit(limit).with_for_update(skip_locked=True)

        with use_principal(Principal.system(self._service)):
            async with self._db.session() as session:
                rows = (await session.execute(query)).all()
                published: list[str] = []
                for row in rows:
                    await self._bus.publish(Event.model_validate_json(row.payload))
                    published.append(row.id)
                if published:
                    await session.execute(
                        update(table)
                        .where(table.c.id.in_(published))
                        .values(published_at=self._clock.now())
                    )
        return len(published)
