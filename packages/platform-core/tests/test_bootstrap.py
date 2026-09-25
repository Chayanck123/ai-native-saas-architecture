"""Per-service database bootstrap: least-privilege roles, idempotent, managed-admin safe."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import asyncpg
import pytest

from platform_core.bootstrap import ServiceDatabase, bootstrap, parse_service_databases


def test_parses_and_decodes_service_urls() -> None:
    urls = {
        "billing": "postgresql+asyncpg://billing:p%40ss%27word@db:5432/billing",
        "growth": "postgresql://growth_svc:secret@db/growth",
    }
    assert parse_service_databases(urls) == [
        ServiceDatabase("billing", role="billing", password="p@ss'word", database="billing"),
        ServiceDatabase("growth", role="growth_svc", password="secret", database="growth"),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://billing:pw@db/billing;drop",  # database name smuggling SQL
        "postgresql://bil%22ling:pw@db/billing",  # role name with a quote
        "postgresql://Billing:pw@db/billing",  # identifiers must be lower-case
        "postgresql://billing@db/billing",  # no password
    ],
)
def test_rejects_unsafe_or_incomplete_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_service_databases({"billing": url})


# ------------------------------------------------------------------ PostgreSQL
@pytest.fixture
async def managed_admin() -> AsyncIterator[str]:
    """An admin like AWS RDS's master user: CREATEROLE + CREATEDB, but no superuser."""
    superuser_url = os.environ.get("ADMIN_DATABASE_URL", "")
    if not superuser_url.startswith("postgresql"):
        pytest.skip("ADMIN_DATABASE_URL (a PostgreSQL superuser) not set")
    superuser = await asyncpg.connect(superuser_url)
    password = secrets.token_hex(8)
    await superuser.execute(
        f"CREATE ROLE bootstrap_admin LOGIN PASSWORD '{password}' CREATEROLE CREATEDB"
    )
    parsed = urlparse(superuser_url)
    try:
        yield parsed._replace(
            netloc=f"bootstrap_admin:{password}@{parsed.hostname}:{parsed.port or 5432}"
        ).geturl()
    finally:
        await superuser.execute("DROP DATABASE IF EXISTS bootstrap_svc WITH (FORCE)")
        await superuser.execute("DROP ROLE IF EXISTS bootstrap_svc")
        await superuser.execute("DROP ROLE IF EXISTS bootstrap_admin")
        await superuser.close()


@pytest.mark.integration
async def test_bootstrap_as_managed_admin_is_idempotent_and_least_privilege(
    managed_admin: str,
) -> None:
    host = urlparse(managed_admin)
    service_url = (
        f"postgresql+asyncpg://bootstrap_svc:{{}}@{host.hostname}:{host.port}/bootstrap_svc"
    )
    messages: list[str] = []

    await bootstrap(
        managed_admin, {"svc": service_url.format("first")}, extensions={}, log=messages.append
    )
    # A second run (e.g. the next deploy, after the secret was rotated) must succeed.
    await bootstrap(
        managed_admin, {"svc": service_url.format("rotated")}, extensions={}, log=messages.append
    )
    assert messages == ["svc: role bootstrap_svc and database bootstrap_svc ready"] * 2

    service = await asyncpg.connect(service_url.format("rotated").replace("+asyncpg", ""))
    try:
        role = await service.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles"
            " WHERE rolname = current_user"
        )
        owner = await service.fetchval(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        )
        # The service can create its own tables in the database it owns.
        await service.execute("CREATE TABLE bootstrap_probe (id int)")
    finally:
        await service.close()
    assert dict(role) == {"rolsuper": False, "rolbypassrls": False, "rolcreaterole": False}
    assert owner == "bootstrap_svc"


@pytest.mark.integration
async def test_a_role_that_bypasses_rls_is_fixed_or_refused(managed_admin: str) -> None:
    superuser_url = os.environ["ADMIN_DATABASE_URL"]
    superuser = await asyncpg.connect(superuser_url)
    try:
        await superuser.execute("CREATE ROLE bootstrap_svc LOGIN PASSWORD 'x' BYPASSRLS")
    finally:
        await superuser.close()
    host = urlparse(superuser_url)
    urls = {"svc": f"postgresql://bootstrap_svc:pw@{host.hostname}:{host.port}/bootstrap_svc"}

    # A managed admin can't revoke BYPASSRLS, so it must refuse rather than deploy
    # a service that row-level security doesn't apply to.
    with pytest.raises(RuntimeError, match="row-level security"):
        await bootstrap(managed_admin, urls, extensions={}, log=lambda _: None)

    await bootstrap(superuser_url, urls, extensions={}, log=lambda _: None)
    superuser = await asyncpg.connect(superuser_url)
    try:
        bypasses = await superuser.fetchval(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'bootstrap_svc'"
        )
    finally:
        await superuser.close()
    assert bypasses is False
