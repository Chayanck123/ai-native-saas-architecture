"""Assemble the growth service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from growth_service.api import router
from growth_service.models import Base
from growth_service.service import PROJECTED, ClientEvent, GrowthService
from growth_service.settings import GrowthSettings
from platform_core.catalog import Events
from platform_core.infrastructure import Infrastructure
from platform_core.service import ServiceRuntime, create_service_app
from platform_core.tasks import Task
from platform_core.worker import Worker, event_of


@dataclass
class GrowthServiceApp:
    runtime: ServiceRuntime
    service: GrowthService
    app: FastAPI
    worker: Worker


def build_worker(runtime: ServiceRuntime, service: GrowthService) -> Worker:
    worker = Worker(runtime)

    @worker.task("growth.project")
    async def project(task: Task) -> dict[str, Any]:
        return await service.project(event_of(task))

    @worker.task("growth.ingest_beacon")
    async def ingest_beacon(task: Task) -> dict[str, Any]:
        data = event_of(task).data
        events = [
            ClientEvent(
                name=str(item["name"]),
                id=item.get("id"),
                anonymous_id=item.get("anonymous_id"),
                occurred_at=datetime.fromisoformat(item["occurred_at"])
                if item.get("occurred_at")
                else None,
                properties=dict(item.get("properties") or {}),
            )
            for item in data.get("events", [])
        ]
        return await service.ingest(events, source="edge")

    @worker.task("growth.scheduled_job")
    async def scheduled(task: Task) -> dict[str, Any]:
        job = event_of(task).data.get("job")
        if job == "pql_scoring":
            return await service.score_all_pqls()
        if job == "experiment_autopilot":
            return {"decisions": await service.run_autopilot()}
        return {"skipped": True}

    worker.on_event(*PROJECTED, task="growth.project")
    worker.on_event(Events.TELEMETRY_BATCH_RECEIVED, task="growth.ingest_beacon")
    worker.on_event(Events.SCHEDULED_JOB_REQUESTED, task="growth.scheduled_job")
    return worker


def build_growth(
    settings: GrowthSettings, infra: Infrastructure, *, span_exporter: SpanExporter | None = None
) -> GrowthServiceApp:
    runtime = ServiceRuntime.create(
        "growth", settings.core, infra, metadata=Base.metadata, span_exporter=span_exporter
    )
    service = GrowthService(runtime, settings)
    app = create_service_app(
        runtime,
        title="Growth Service",
        description="Telemetry, funnels, cohorts, attribution, LTV:CAC, A/B tests and PQLs.",
        routers=[router],
    )
    app.state.growth = service
    return GrowthServiceApp(runtime, service, app, build_worker(runtime, service))
