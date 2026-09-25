"""Assemble the knowledge service: runtime + domain service + HTTP app + worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from knowledge_service.api import router
from knowledge_service.embeddings import Embedder, HashingEmbedder, VoyageEmbedder
from knowledge_service.models import TENANT_TABLES, Base
from knowledge_service.service import KnowledgeService
from knowledge_service.settings import KnowledgeSettings
from platform_core.catalog import Events
from platform_core.infrastructure import Infrastructure
from platform_core.retry import RetryPolicy
from platform_core.service import ServiceRuntime, create_service_app
from platform_core.tasks import Task
from platform_core.worker import Worker, event_of


@dataclass
class KnowledgeServiceApp:
    runtime: ServiceRuntime
    service: KnowledgeService
    app: FastAPI
    worker: Worker


def make_embedder(settings: KnowledgeSettings) -> Embedder:
    if settings.embedder == "voyage":
        assert settings.voyage_api_key
        return VoyageEmbedder(
            settings.voyage_api_key,
            model=settings.voyage_model,
            dimensions=settings.embedding_dimensions,
        )
    return HashingEmbedder(settings.embedding_dimensions)


def build_worker(runtime: ServiceRuntime, service: KnowledgeService) -> Worker:
    worker = Worker(runtime)

    @worker.task(
        "knowledge.index_document",
        retry_policy=RetryPolicy(max_attempts=6, base_delay=2.0, max_delay=300.0),
    )
    async def index_document(task: Task) -> dict[str, Any]:
        return await service.index(event_of(task).data["document_id"])

    @worker.task("knowledge.apply_feedback")
    async def apply_feedback(task: Task) -> dict[str, Any]:
        data = event_of(task).data
        return await service.apply_feedback(
            list(data.get("chunk_ids", [])), int(data.get("rating", 0))
        )

    worker.on_event(Events.DOCUMENT_UPLOADED, task="knowledge.index_document")
    worker.on_event(Events.AGENT_FEEDBACK_SUBMITTED, task="knowledge.apply_feedback")
    return worker


def build_knowledge(
    settings: KnowledgeSettings,
    infra: Infrastructure,
    *,
    embedder: Embedder | None = None,
    span_exporter: SpanExporter | None = None,
) -> KnowledgeServiceApp:
    runtime = ServiceRuntime.create(
        "knowledge",
        settings.core,
        infra,
        metadata=Base.metadata,
        tenant_tables=TENANT_TABLES,
        extensions=("vector",),  # pgvector; platform_core.bootstrap installs it in prod
        span_exporter=span_exporter,
    )
    service = KnowledgeService(runtime, embedder or make_embedder(settings), settings)
    app = create_service_app(
        runtime,
        title="Knowledge Service",
        description="Document ingestion, vector embeddings and hybrid retrieval (RAG).",
        routers=[router],
        on_startup=[service.install_indexes],
    )
    app.state.knowledge = service
    return KnowledgeServiceApp(runtime, service, app, build_worker(runtime, service))
