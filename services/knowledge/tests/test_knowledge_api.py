from __future__ import annotations

from typing import Any

import httpx

from knowledge_service.app import KnowledgeServiceApp
from knowledge_service.embeddings import EmbeddingBatch, HashingEmbedder, InputType
from platform_core.catalog import Events, Meters
from platform_core.clock import ManualClock
from platform_core.events import InMemoryEventBus
from platform_core.infrastructure import Infrastructure
from platform_core.retry import PermanentError, RetryableError
from platform_core.testing import asgi_client

from .conftest import headers_for, make_knowledge

REFUNDS = """# Billing
## Refund policy
Customers can request a full refund within 30 days of purchase. After 30 days, refunds are
prorated for annual plans only. Refunds are paid to the original payment method.

## Invoices
Invoices are emailed on the first day of each month. Enterprise customers can pay by wire.
"""

SECURITY = """# Security
All customer data is encrypted at rest with AES-256 and in transit with TLS 1.3.
Single sign-on is available through SAML and OIDC on the Enterprise plan.

## Troubleshooting
Error ERR-4521 means the SSO certificate has expired. Upload a new certificate in the
admin console to fix it.
"""


async def upload_and_index(
    client: httpx.AsyncClient, knowledge: KnowledgeServiceApp, title: str, content: str
) -> dict[str, Any]:
    response = await client.post("/v1/documents", json={"title": title, "content": content})
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    await knowledge.worker.run_until_idle()
    document = (await client.get(f"/v1/documents/{response.json()['id']}")).json()
    assert isinstance(document, dict)
    return document


def event_types(infra: Infrastructure) -> list[str]:
    assert isinstance(infra.event_bus, InMemoryEventBus)
    return [e.type for e in infra.event_bus.published]


async def test_upload_is_indexed_asynchronously_and_metered(
    client: httpx.AsyncClient, knowledge: KnowledgeServiceApp, infra: Infrastructure
) -> None:
    await knowledge.worker.setup()
    document = await upload_and_index(client, knowledge, "Billing handbook", REFUNDS)
    assert document["status"] == "indexed"
    assert document["chunk_count"] >= 2
    assert document["embedding_model"] == "hashing-v1-384"
    types = event_types(infra)
    assert types[:1] == [Events.DOCUMENT_UPLOADED]
    assert Events.DOCUMENT_INDEXED in types
    assert isinstance(infra.event_bus, InMemoryEventBus)
    [usage] = [e for e in infra.event_bus.published if e.type == Events.USAGE_RECORDED]
    assert usage.data["meter"] == Meters.EMBEDDING_TOKENS
    assert usage.data["quantity"] > 0
    assert usage.tenantid == document_tenant(usage.tenantid)


def document_tenant(tenant_id: str | None) -> str | None:
    return tenant_id


async def test_hybrid_search_finds_semantic_and_exact_matches(
    client: httpx.AsyncClient, knowledge: KnowledgeServiceApp
) -> None:
    await knowledge.worker.setup()
    refunds = await upload_and_index(client, knowledge, "Billing handbook", REFUNDS)
    security = await upload_and_index(client, knowledge, "Security whitepaper", SECURITY)

    response = await client.post(
        "/v1/search", json={"query": "How many days do I have to get a refund?", "top_k": 3}
    )
    assert response.status_code == 200
    top = response.json()["results"][0]
    assert top["document_id"] == refunds["id"]
    assert "30 days" in top["text"]
    assert top["dense_rank"] is not None and top["lexical_rank"] is not None

    exact = await client.post("/v1/search", json={"query": "ERR-4521", "mode": "lexical"})
    [hit, *_] = exact.json()["results"]
    assert hit["document_id"] == security["id"]
    assert "ERR-4521" in hit["text"]

    dense = await client.post(
        "/v1/search", json={"query": "is customer data encrypted", "mode": "dense", "top_k": 1}
    )
    assert dense.json()["results"][0]["document_id"] == security["id"]


async def test_tenants_are_isolated(
    client: httpx.AsyncClient, knowledge: KnowledgeServiceApp
) -> None:
    await knowledge.worker.setup()
    document = await upload_and_index(client, knowledge, "Billing handbook", REFUNDS)
    async with asgi_client(knowledge.app, headers=headers_for(knowledge, "ten_other")) as other:
        assert (await other.get(f"/v1/documents/{document['id']}")).status_code == 404
        results = await other.post("/v1/search", json={"query": "refund"})
        assert results.json()["results"] == []
        assert (await other.get("/v1/documents")).json()["total"] == 0


async def test_duplicate_upload_is_deduplicated_and_delete_works(
    client: httpx.AsyncClient, knowledge: KnowledgeServiceApp
) -> None:
    await knowledge.worker.setup()
    document = await upload_and_index(client, knowledge, "Billing handbook", REFUNDS)
    again = await client.post("/v1/documents", json={"title": "copy", "content": REFUNDS})
    assert again.status_code == 200
    assert again.json()["id"] == document["id"]
    assert again.json()["deduplicated"] is True

    assert (await client.delete(f"/v1/documents/{document['id']}")).status_code == 204
    assert (await client.get(f"/v1/documents/{document['id']}")).status_code == 404
    assert (await client.post("/v1/search", json={"query": "refund"})).json()["results"] == []


async def test_viewers_can_search_but_not_upload(
    knowledge: KnowledgeServiceApp, tenant_id: str
) -> None:
    from platform_core.rbac import Role

    async with asgi_client(
        knowledge.app, headers=headers_for(knowledge, tenant_id, Role.VIEWER)
    ) as viewer:
        denied = await viewer.post("/v1/documents", json={"title": "t", "content": "c"})
        assert denied.status_code == 403
        assert (await viewer.post("/v1/search", json={"query": "anything"})).status_code == 200


async def test_positive_feedback_lifts_a_chunk_in_the_ranking(
    client: httpx.AsyncClient, knowledge: KnowledgeServiceApp, tenant_id: str
) -> None:
    await knowledge.worker.setup()
    twins = (
        "# Plans\nThe Pro plan includes priority support and a 99.9% uptime SLA.\n\n"
        "# Plans (legacy)\nThe Pro plan includes priority support and a 99.9% uptime SLA."
    )
    await upload_and_index(client, knowledge, "Plans", twins)
    before = (await client.post("/v1/search", json={"query": "pro plan support"})).json()
    ranked = [hit["chunk_id"] for hit in before["results"]]
    loser = ranked[1]

    event = knowledge.runtime.event(
        Events.AGENT_FEEDBACK_SUBMITTED,
        {"run_id": "run_1", "rating": 1, "chunk_ids": [loser]},
        tenant_id=tenant_id,
    )
    await knowledge.runtime.publish(event)
    await knowledge.worker.run_until_idle()

    after = (await client.post("/v1/search", json={"query": "pro plan support"})).json()
    assert after["results"][0]["chunk_id"] == loser
    assert after["results"][0]["feedback_quality"] > 0.5


class FlakyEmbedder:
    """Fails with a transient error a few times, then succeeds."""

    def __init__(self, failures: int, *, permanent: bool = False) -> None:
        self._inner = HashingEmbedder(384)
        self._failures = failures
        self._permanent = permanent
        self.calls = 0

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    @property
    def model(self) -> str:
        return self._inner.model

    async def embed(self, texts: list[str], *, input_type: InputType) -> EmbeddingBatch:
        self.calls += 1
        if input_type == "document" and self.calls <= self._failures:
            if self._permanent:
                raise PermanentError("provider rejected the input")
            raise RetryableError("embedding provider timed out")
        return await self._inner.embed(texts, input_type=input_type)


async def test_indexing_retries_transient_provider_failures(
    database_url: str, infra: Infrastructure, clock: ManualClock, tenant_id: str
) -> None:
    embedder = FlakyEmbedder(failures=2)
    knowledge = await make_knowledge(database_url, infra, embedder)
    await knowledge.worker.setup()
    try:
        async with asgi_client(knowledge.app, headers=headers_for(knowledge, tenant_id)) as client:
            created = await client.post("/v1/documents", json={"title": "t", "content": REFUNDS})
            for _ in range(3):
                await knowledge.worker.run_until_idle()
                clock.advance(600)  # jump past the exponential backoff
            document = (await client.get(f"/v1/documents/{created.json()['id']}")).json()
        assert document["status"] == "indexed"
        assert embedder.calls == 3
    finally:
        await _cleanup(knowledge)


async def test_permanent_provider_failure_marks_document_failed(
    database_url: str, infra: Infrastructure, tenant_id: str
) -> None:
    knowledge = await make_knowledge(database_url, infra, FlakyEmbedder(1, permanent=True))
    await knowledge.worker.setup()
    try:
        async with asgi_client(knowledge.app, headers=headers_for(knowledge, tenant_id)) as client:
            created = await client.post("/v1/documents", json={"title": "t", "content": REFUNDS})
            await knowledge.worker.run_until_idle()
            document = (await client.get(f"/v1/documents/{created.json()['id']}")).json()
        assert document["status"] == "failed"
        assert "rejected" in document["error"]
        assert len(await infra.task_queue.dead_letters("knowledge")) == 1
    finally:
        await _cleanup(knowledge)


async def _cleanup(knowledge: KnowledgeServiceApp) -> None:
    from knowledge_service.models import Base

    if knowledge.runtime.db.is_postgres:
        async with knowledge.runtime.db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await knowledge.runtime.stop()
