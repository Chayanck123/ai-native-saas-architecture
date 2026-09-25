"""Row-level security must isolate tenants even when application code forgets to."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import String, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from platform_core.config import CoreSettings
from platform_core.context import Principal, use_principal, use_tenant
from platform_core.db import Database, TenantScoped
from platform_core.infrastructure import Infrastructure
from platform_core.service import ServiceRuntime

pytestmark = pytest.mark.integration


class Base(DeclarativeBase):
    pass


class Note(TenantScoped, Base):
    __tablename__ = "rls_test_notes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[str] = mapped_column(String(200))


def _postgres_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL (PostgreSQL) not set")
    return url


@pytest.fixture
async def runtime(infra: Infrastructure) -> AsyncIterator[ServiceRuntime]:
    settings = CoreSettings(service_name="rls", database_url=_postgres_url())
    runtime = ServiceRuntime.create(
        "rls", settings, infra, metadata=Base.metadata, tenant_tables=["rls_test_notes"]
    )
    await runtime.start()
    yield runtime
    async with runtime.db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await runtime.stop()


async def _seed(runtime: ServiceRuntime) -> None:
    for tenant, note_id in [("ten_a", "n1"), ("ten_a", "n2"), ("ten_b", "n3")]:
        with use_tenant(tenant):
            async with runtime.db.session() as session:
                session.add(Note(id=note_id, tenant_id=tenant, body=f"secret of {tenant}"))


async def test_queries_without_tenant_filter_only_see_own_rows(runtime: ServiceRuntime) -> None:
    await _seed(runtime)
    with use_tenant("ten_a"):
        async with runtime.db.session() as session:
            # Deliberately no WHERE tenant_id = ... - the database enforces it.
            rows = (await session.execute(select(Note).order_by(Note.id))).scalars().all()
    assert [note.id for note in rows] == ["n1", "n2"]

    with use_tenant(None):
        async with runtime.db.session() as session:
            assert (await session.execute(select(Note))).first() is None


async def test_cannot_write_rows_into_another_tenant(runtime: ServiceRuntime) -> None:
    with pytest.raises(DBAPIError, match="row-level security"), use_tenant("ten_a"):
        async with runtime.db.session() as session:
            session.add(Note(id="evil", tenant_id="ten_b", body="planted"))


async def test_system_principal_can_see_all_tenants(runtime: ServiceRuntime) -> None:
    await _seed(runtime)
    with use_principal(Principal.system("rls")):
        async with runtime.db.session() as session:
            rows = (await session.execute(select(Note))).scalars().all()
    assert {note.tenant_id for note in rows} == {"ten_a", "ten_b"}


class ReplicaBase(DeclarativeBase):
    pass


class ReplicaNote(TenantScoped, ReplicaBase):
    __tablename__ = "rls_test_replica_notes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)


async def test_replicas_starting_together_do_not_race_on_schema_setup() -> None:
    # API pods and workers of one service all set up the schema as they boot.
    replicas = [Database(_postgres_url()) for _ in range(6)]
    try:
        results = await asyncio.gather(
            *(
                db.create_all(ReplicaBase.metadata, tenant_tables=["rls_test_replica_notes"])
                for db in replicas
            ),
            return_exceptions=True,
        )
        assert [r for r in results if isinstance(r, BaseException)] == []
    finally:
        async with replicas[0].engine.begin() as conn:
            await conn.run_sync(ReplicaBase.metadata.drop_all)
        for db in replicas:
            await db.dispose()
