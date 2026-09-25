"""Assemble the gateway: identity + edge protections + proxy + webhooks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from gateway_service.api import auth, me, ops, proxy
from gateway_service.edge import EdgeMiddleware
from gateway_service.identity import IdentityService
from gateway_service.models import TENANT_TABLES, Base
from gateway_service.proxy import Proxy
from gateway_service.settings import GatewaySettings
from gateway_service.webhooks import WebhookService
from platform_core.catalog import WEBHOOK_EVENT_TYPES, Events
from platform_core.clients import ServiceClient
from platform_core.infrastructure import Infrastructure
from platform_core.retry import RetryableError, RetryPolicy
from platform_core.service import ServiceRuntime, create_service_app
from platform_core.tasks import Task
from platform_core.worker import Worker, event_of

WEBHOOK_RETRY = RetryPolicy(max_attempts=8, base_delay=5.0, max_delay=3_600.0)

DESCRIPTION = """
The public, API-first interface of the platform. Every capability of the product
is available here: authenticate with a user token (`Authorization: Bearer <jwt>`)
or a scoped API key (`X-API-Key: ank_...`).

* Send `Idempotency-Key` on any POST/PUT/PATCH/DELETE to make retries safe.
* Responses carry `X-Request-Id` / `X-Trace-Id` for support and tracing, and
  `X-RateLimit-*` headers; errors are RFC 9457 problem documents.
* The full platform schema (all services) is served at `/v1/openapi.json`.
"""


@dataclass
class GatewayServiceApp:
    runtime: ServiceRuntime
    identity: IdentityService
    webhooks: WebhookService
    app: FastAPI
    worker: Worker


def build_worker(
    runtime: ServiceRuntime, identity: IdentityService, webhooks: WebhookService
) -> Worker:
    worker = Worker(runtime)

    @worker.task("gateway.fanout_webhooks")
    async def fan_out(task: Task) -> dict[str, Any]:
        return await webhooks.fan_out(event_of(task))

    @worker.task("gateway.deliver_webhook", retry_policy=WEBHOOK_RETRY)
    async def deliver(task: Task) -> dict[str, Any]:
        delivery_id = task.payload["delivery_id"]
        try:
            return await webhooks.deliver(delivery_id)
        except RetryableError:
            if task.attempt >= WEBHOOK_RETRY.max_attempts:
                await webhooks.mark_failed(delivery_id)
            raise

    @worker.task("gateway.sync_plan")
    async def sync_plan(task: Task) -> dict[str, Any]:
        event = event_of(task)
        assert event.tenantid
        return await identity.set_tenant_plan(event.tenantid, str(event.data["plan"]))

    worker.on_event(*WEBHOOK_EVENT_TYPES, task="gateway.fanout_webhooks")
    worker.on_event(Events.SUBSCRIPTION_CHANGED, task="gateway.sync_plan")
    return worker


def build_gateway(
    settings: GatewaySettings,
    infra: Infrastructure,
    *,
    transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    webhook_transport: httpx.AsyncBaseTransport | None = None,
    span_exporter: SpanExporter | None = None,
) -> GatewayServiceApp:
    transports = transports or {}
    runtime = ServiceRuntime.create(
        "gateway",
        settings.core,
        infra,
        metadata=Base.metadata,
        tenant_tables=TENANT_TABLES,
        span_exporter=span_exporter,
    )
    billing = ServiceClient(
        "billing",
        tokens=runtime.internal_tokens,
        base_url=settings.upstreams["billing"],
        transport=transports.get("billing"),
        timeout=3.0,
        tracer=runtime.tracer,
    )
    identity = IdentityService(runtime, settings, billing)
    webhooks = WebhookService(runtime, settings, webhook_transport)
    router_proxy = Proxy(runtime, settings, transports)
    app = create_service_app(
        runtime,
        title="AI-Native SaaS Platform API",
        description=DESCRIPTION,
        routers=[auth, me, ops, proxy],  # the proxy catch-all must stay last
        authenticator=identity.authenticate,
        middleware=[
            (
                EdgeMiddleware,
                {
                    "runtime": runtime,
                    "plan_limits": settings.plan_rate_limits,
                    "anonymous_limit": settings.anonymous_rate_limit,
                    "resolve_plan": identity.tenant_plan,
                },
            )
        ],
        on_shutdown=[billing.aclose, webhooks.aclose, router_proxy.aclose],
    )
    app.state.identity = identity
    app.state.webhooks = webhooks
    app.state.proxy = router_proxy
    return GatewayServiceApp(
        runtime, identity, webhooks, app, build_worker(runtime, identity, webhooks)
    )
