from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest

from knowledge_service.app import KnowledgeServiceApp, build_knowledge
from knowledge_service.embeddings import Embedder
from knowledge_service.models import Base
from knowledge_service.settings import KnowledgeSettings
from platform_core.clock import ManualClock
from platform_core.config import CoreSettings
from platform_core.ids import new_id
from platform_core.infrastructure import Infrastructure
from platform_core.rbac import Role
from platform_core.testing import asgi_client, internal_headers, tenant_principal


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
                    not postgres.startswith("postgresql"),
                    reason="DATABASE_URL (PostgreSQL) not set",
                ),
            ],
        ),
    ]


@pytest.fixture(params=_database_urls())
def database_url(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def make_knowledge(
    database_url: str, infra: Infrastructure, embedder: Embedder | None = None
) -> KnowledgeServiceApp:
    settings = KnowledgeSettings(
        core=CoreSettings(service_name="knowledge", database_url=database_url),
        chunk_target_tokens=60,
    )
    built = build_knowledge(settings, infra, embedder=embedder)
    await built.runtime.start()
    await built.service.install_indexes()
    return built


@pytest.fixture
async def knowledge(database_url: str, infra: Infrastructure) -> AsyncIterator[KnowledgeServiceApp]:
    built = await make_knowledge(database_url, infra)
    yield built
    if built.runtime.db.is_postgres:
        async with built.runtime.db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await built.runtime.stop()


@pytest.fixture
def tenant_id() -> str:
    return new_id("ten")


def headers_for(
    built: KnowledgeServiceApp, tenant_id: str, role: Role = Role.MEMBER
) -> dict[str, str]:
    return internal_headers(built.runtime.internal_tokens, tenant_principal(tenant_id, role=role))


@pytest.fixture
async def client(
    knowledge: KnowledgeServiceApp, tenant_id: str
) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(knowledge.app, headers=headers_for(knowledge, tenant_id)) as client:
        yield client
