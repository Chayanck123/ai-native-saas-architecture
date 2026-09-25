"""Create one database and one least-privilege login per service (idempotent).

Every service owns its database (database-per-service) and connects with its
own role. Roles are NOSUPERUSER and NOBYPASSRLS so PostgreSQL row-level
security applies to them (superusers silently bypass it). Run before each
deploy - in Kubernetes as the ``db-bootstrap`` Job (infra/k8s/jobs), which
gets both inputs from AWS Secrets Manager:

    ADMIN_DATABASE_URL=postgresql://admin:...@host:5432/postgres \\
    SERVICE_DATABASE_URLS='{"gateway": "postgresql+asyncpg://gateway:pw@host:5432/gateway"}' \\
    python -m platform_core.bootstrap

Re-running is safe: existing roles get their password re-synced (so rotating a
secret in Secrets Manager takes effect on the next deploy) and existing
databases are left alone.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import asyncpg

# Extensions a service needs that only an administrator may install.
SERVICE_EXTENSIONS: Mapping[str, Sequence[str]] = {"knowledge": ("vector",)}

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class ServiceDatabase:
    service: str
    role: str
    password: str
    database: str


def _plain_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


def _literal(value: str) -> str:
    # Role passwords can't be bind parameters in DDL; quote them as a literal.
    return "'" + value.replace("'", "''") + "'"


def parse_service_databases(service_urls: Mapping[str, str]) -> list[ServiceDatabase]:
    """Validate the services' connection URLs before any SQL runs."""
    databases = []
    for service, url in service_urls.items():
        parsed = urlparse(_plain_dsn(url))
        if not parsed.password:
            raise ValueError(f"{service}: the database URL has no password")
        databases.append(
            ServiceDatabase(
                service=service,
                role=_identifier(unquote(parsed.username or "")),
                password=unquote(parsed.password),
                database=_identifier(parsed.path.lstrip("/")),
            )
        )
    return databases


def _database_dsn(admin_url: str, database: str) -> str:
    return urlparse(_plain_dsn(admin_url))._replace(path=f"/{database}").geturl()


async def _enforce_row_level_security(
    admin: asyncpg.Connection, role: str, *, is_superuser: bool
) -> None:
    """A service role that bypasses RLS would void tenant isolation: fix it or stop."""
    bypasses = await admin.fetchval(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = $1", role
    )
    if not bypasses:
        return
    if not is_superuser:
        raise RuntimeError(
            f"role {role} is a superuser or has BYPASSRLS, so row-level security would not "
            "apply to it; only a superuser can revoke that"
        )
    await admin.execute(f"ALTER ROLE {role} NOSUPERUSER NOBYPASSRLS")


async def bootstrap(
    admin_url: str,
    service_urls: Mapping[str, str],
    *,
    extensions: Mapping[str, Sequence[str]] = SERVICE_EXTENSIONS,
    log: Callable[[str], None] = print,
) -> None:
    databases = parse_service_databases(service_urls)
    admin = await asyncpg.connect(_plain_dsn(admin_url))
    try:
        # Creating a database for another owner requires being able to SET ROLE to
        # it (PostgreSQL 16+; plain membership before). Managed "admins" such as
        # AWS RDS's rds_superuser are not superusers, so they join the role first.
        can_own = (
            "SELECT pg_has_role(current_user, $1, 'SET')"
            if admin.get_server_version().major >= 16
            else "SELECT pg_has_role(current_user, $1, 'MEMBER')"
        )
        is_superuser = await admin.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        for db in databases:
            password = _literal(db.password)
            if await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", db.role):
                await _enforce_row_level_security(admin, db.role, is_superuser=bool(is_superuser))
                await admin.execute(f"ALTER ROLE {db.role} LOGIN PASSWORD {password}")
            else:
                await admin.execute(
                    f"CREATE ROLE {db.role} LOGIN PASSWORD {password} NOSUPERUSER NOBYPASSRLS"
                )
            if not await admin.fetchval(can_own, db.role):
                await admin.execute(f"GRANT {db.role} TO CURRENT_USER")
            found = await admin.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", db.database
            )
            if not found:
                await admin.execute(f"CREATE DATABASE {db.database} OWNER {db.role}")
            log(f"{db.service}: role {db.role} and database {db.database} ready")
    finally:
        await admin.close()

    for db in databases:
        wanted = [_identifier(name) for name in extensions.get(db.service, ())]
        if not wanted:
            continue
        connection = await asyncpg.connect(_database_dsn(admin_url, db.database))
        try:
            for name in wanted:
                await connection.execute(f"CREATE EXTENSION IF NOT EXISTS {name}")
        finally:
            await connection.close()
        log(f"{db.service}: extensions {', '.join(wanted)} ready")


def main() -> None:
    try:
        admin_url = os.environ["ADMIN_DATABASE_URL"]
        service_urls = json.loads(os.environ["SERVICE_DATABASE_URLS"])
    except KeyError as missing:
        raise SystemExit(f"{missing.args[0]} is not set") from None
    asyncio.run(bootstrap(admin_url, service_urls))


if __name__ == "__main__":
    main()
