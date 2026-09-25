"""Assemble the billing service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from billing_service.api import router
from billing_service.models import TENANT_TABLES, Base
from billing_service.service import BillingService
from billing_service.settings import BillingSettings
from platform_core.catalog import Events
from platform_core.infrastructure import Infrastructure
from platform_core.retry import PermanentError
from platform_core.service import ServiceRuntime, create_service_app
from platform_core.tasks import Task
from platform_core.worker import Worker, event_of


@dataclass
class BillingServiceApp:
    runtime: ServiceRuntime
    service: BillingService
    app: FastAPI
    worker: Worker


def build_worker(runtime: ServiceRuntime, service: BillingService) -> Worker:
    worker = Worker(runtime)

    @worker.task("billing.provision_tenant")
    async def provision(task: Task) -> dict[str, Any]:
        return await service.provision_tenant(plan=event_of(task).data.get("plan", "free"))

    @worker.task("billing.record_usage")
    async def record_usage(task: Task) -> dict[str, Any]:
        event = event_of(task)
        data = event.data
        if not event.tenantid:
            raise PermanentError("usage event without tenant")
        occurred = data.get("occurred_at")
        return await service.record_usage(
            meter=str(data["meter"]),
            quantity=int(data["quantity"]),
            idempotency_key=str(data.get("idempotency_key") or event.id),
            occurred_at=datetime.fromisoformat(occurred) if occurred else event.time,
            resource_id=data.get("resource_id"),
            source=event.source,
        )

    @worker.task("billing.seat_added")
    async def seat_added(task: Task) -> dict[str, Any]:
        return await service.adjust_seats(+1)

    @worker.task("billing.seat_removed")
    async def seat_removed(task: Task) -> dict[str, Any]:
        return await service.adjust_seats(-1)

    @worker.task("billing.apply_provider_update")
    async def provider_update(task: Task) -> dict[str, Any]:
        return await service.apply_provider_update(event_of(task).data)

    @worker.task("billing.scheduled_job")
    async def scheduled(task: Task) -> dict[str, Any]:
        if event_of(task).data.get("job") == "revenue_snapshot":
            return await service.snapshot_revenue()
        return {"skipped": True}

    worker.on_event(Events.TENANT_CREATED, task="billing.provision_tenant")
    worker.on_event(Events.USAGE_RECORDED, task="billing.record_usage")
    worker.on_event(Events.MEMBER_ADDED, task="billing.seat_added")
    worker.on_event(Events.MEMBER_REMOVED, task="billing.seat_removed")
    worker.on_event(Events.PROVIDER_SUBSCRIPTION_UPDATED, task="billing.apply_provider_update")
    worker.on_event(Events.SCHEDULED_JOB_REQUESTED, task="billing.scheduled_job")
    return worker


def build_billing(
    settings: BillingSettings, infra: Infrastructure, *, span_exporter: SpanExporter | None = None
) -> BillingServiceApp:
    runtime = ServiceRuntime.create(
        "billing",
        settings.core,
        infra,
        metadata=Base.metadata,
        tenant_tables=TENANT_TABLES,
        span_exporter=span_exporter,
    )
    service = BillingService(runtime, settings.cost_rates)
    app = create_service_app(
        runtime,
        title="Billing Service",
        description="Plans, usage-based metering, credits, entitlements and ARR analytics.",
        routers=[router],
    )
    app.state.billing = service
    return BillingServiceApp(runtime, service, app, build_worker(runtime, service))
