"""Tenant-scoped persistence on SQLAlchemy (PostgreSQL in production, SQLite locally).

Multi-tenancy is enforced twice:

1. **In code** - every repository query filters on ``tenant_id``.
2. **In the database** - PostgreSQL row-level security (RLS). Each transaction
   runs ``set_config('app.tenant_id', <tenant>, true)`` and a policy on every
   tenant table hides rows belonging to anyone else. Even a query that *forgets*
   its ``WHERE tenant_id = ...`` cannot leak another customer's data.

Trusted background jobs that must see every tenant (e.g. nightly revenue
roll-ups) run under a *system* principal, which sets ``app.bypass_rls``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, MetaData, String, TypeDecorator, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from platform_core.context import current_tenant_id, is_system_context

logger = logging.getLogger(__name__)

JsonType = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use timezone-aware UTC values")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class TenantScoped:
    """Mixin for tables whose rows belong to exactly one tenant."""

    tenant_id: Mapped[str] = mapped_column(String(64), index=True)


_RLS_POLICY = """
CREATE POLICY tenant_isolation ON {table}
    USING (current_setting('app.bypass_rls', true) = 'on'
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.bypass_rls', true) = 'on'
           OR tenant_id = current_setting('app.tenant_id', true))
"""

# Arbitrary constant naming the "schema changes" advisory lock (ASCII "ainative").
_SCHEMA_LOCK_KEY = 0x61696E6174697665


class Database:
    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 10) -> None:
        self.url = url
        self.is_postgres = url.startswith("postgresql")
        kwargs: dict[str, Any] = {"echo": echo}
        if url.startswith("sqlite"):
            if ":memory:" in url or url.endswith("://"):
                kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
            else:
                kwargs.update(connect_args={"timeout": 30})
        else:
            kwargs.update(pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A transaction bound to the current tenant (commit on success, rollback on error)."""
        async with self._sessionmaker() as session, session.begin():
            if self.is_postgres:
                await session.execute(
                    text(
                        "SELECT set_config('app.tenant_id', :tenant, true),"
                        " set_config('app.bypass_rls', :bypass, true)"
                    ),
                    {
                        "tenant": current_tenant_id() or "",
                        "bypass": "on" if is_system_context() else "off",
                    },
                )
            yield session

    @asynccontextmanager
    async def migration(self) -> AsyncIterator[AsyncConnection]:
        """A DDL transaction that runs alone, even across processes.

        Every replica (API pods and workers alike) sets up the schema when it
        starts. On PostgreSQL a transaction-scoped advisory lock makes them take
        turns: without it, two processes racing through ``CREATE TABLE`` both see
        the table missing and the slower one crashes on a duplicate-key error.
        """
        async with self.engine.begin() as conn:
            if self.is_postgres:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_LOCK_KEY}
                )
            yield conn

    async def create_all(
        self,
        metadata: MetaData,
        *,
        tenant_tables: Iterable[str] = (),
        extensions: Iterable[str] = (),
    ) -> None:
        """Create tables (idempotent) and install RLS policies on PostgreSQL.

        Production would run versioned migrations (e.g. Alembic) from CI/CD instead;
        this keeps local development and tests zero-setup.
        """
        async with self.migration() as conn:
            if self.is_postgres:
                for name in extensions:
                    await self._try_create_extension(conn, name)
            await conn.run_sync(metadata.create_all)
            if self.is_postgres:
                for table in tenant_tables:
                    await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
                    await conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
                    await conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
                    await conn.execute(text(_RLS_POLICY.format(table=table)))

    @staticmethod
    async def _try_create_extension(conn: AsyncConnection, name: str) -> None:
        try:
            async with conn.begin_nested():
                await conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {name}"))
        except Exception as exc:  # pragma: no cover - depends on database privileges
            logger.warning("could not create extension %s: %s", name, exc)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
