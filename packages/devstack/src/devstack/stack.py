"""The whole platform in one process.

Every service is built exactly as in production, but:

* infrastructure is in-memory (event bus, task queues, idempotency, rate limits);
* databases are SQLite (in memory, or files under ``--data-dir``);
* service-to-service HTTP goes through ``httpx.ASGITransport`` - real HTTP
  semantics (headers, status codes, internal tokens, trace propagation) without
  sockets;
* the Lambda functions are served by a tiny emulator on the same port.

So ``devstack serve`` gives a developer the full, working system with no Docker,
and the end-to-end tests exercise every service boundary deterministically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from agents_service.app import AgentsServiceApp, build_agents
from agents_service.llm import LLMClient
from agents_service.settings import AgentsSettings
from billing_service.app import BillingServiceApp, build_billing
from billing_service.settings import BillingSettings
from gateway_service.app import GatewayServiceApp, build_gateway
from gateway_service.settings import GatewaySettings
from growth_service.app import GrowthServiceApp, build_growth
from growth_service.settings import GrowthSettings
from knowledge_service.app import KnowledgeServiceApp, build_knowledge
from knowledge_service.settings import KnowledgeSettings
from platform_core.clock import Clock, SystemClock
from platform_core.config import CoreSettings
from platform_core.infrastructure import Infrastructure
from platform_core.service import ServiceRuntime
from platform_core.worker import Worker
from serverless_functions.local import create_app as create_functions_app

STAFF_EMAIL = "staff@ainative.dev"

AnyService = (
    GatewayServiceApp
    | KnowledgeServiceApp
    | AgentsServiceApp
    | BillingServiceApp
    | (GrowthServiceApp)
)


@dataclass
class DevStack:
    infra: Infrastructure
    gateway: GatewayServiceApp
    knowledge: KnowledgeServiceApp
    agents: AgentsServiceApp
    billing: BillingServiceApp
    growth: GrowthServiceApp
    app: FastAPI  # gateway + Lambda emulator: the single public entry point
    _worker_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def services(self) -> list[AnyService]:
        return [self.gateway, self.knowledge, self.agents, self.billing, self.growth]

    @property
    def runtimes(self) -> list[ServiceRuntime]:
        return [s.runtime for s in self.services]

    @property
    def workers(self) -> list[Worker]:
        return [s.worker for s in self.services]

    @property
    def clock(self) -> Clock:
        return self.infra.clock

    async def start(self) -> None:
        for runtime in self.runtimes:
            await runtime.start()
        await self.knowledge.service.install_indexes()
        for worker in self.workers:
            await worker.setup()

    async def stop(self) -> None:
        await self.stop_workers()
        for runtime in self.runtimes:
            await runtime.stop()

    async def drain(self, *, max_rounds: int = 200) -> int:
        """Process events and tasks across all services until the system is quiet."""
        total = 0
        for _ in range(max_rounds):
            progress = 0
            for worker in self.workers:
                progress += await worker.run_until_idle()
            total += progress
            if progress == 0:
                break
        return total

    def start_workers(self) -> None:
        """Run every worker loop in the background (``devstack serve``)."""
        for worker in self.workers:
            self._worker_tasks.append(asyncio.create_task(worker.run_forever(idle_block_ms=200)))

    async def stop_workers(self) -> None:
        for worker in self.workers:
            worker.stop()
        for task in self._worker_tasks:
            task.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

    def client(self, **headers: str) -> httpx.AsyncClient:
        """An HTTP client for the public API, as an external customer would use it."""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://api.ainative.local",
            headers=headers,
        )


def _database_url(name: str, data_dir: Path | None) -> str:
    if data_dir is None:
        return "sqlite+aiosqlite:///:memory:"
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{data_dir / f'{name}.db'}"


def build_devstack(
    *,
    data_dir: Path | None = None,
    clock: Clock | None = None,
    llm: LLMClient | None = None,
    span_exporter: SpanExporter | None = None,
    webhook_transport: httpx.AsyncBaseTransport | None = None,
    log_json: bool = False,
) -> DevStack:
    infra = Infrastructure.in_memory(clock or SystemClock())

    def core(name: str) -> CoreSettings:
        return CoreSettings(
            service_name=name, database_url=_database_url(name, data_dir), log_json=log_json
        )

    knowledge = build_knowledge(
        KnowledgeSettings(core=core("knowledge")), infra, span_exporter=span_exporter
    )
    billing = build_billing(
        BillingSettings(core=core("billing")), infra, span_exporter=span_exporter
    )
    growth = build_growth(GrowthSettings(core=core("growth")), infra, span_exporter=span_exporter)
    agents = build_agents(
        AgentsSettings(core=core("agents")),
        infra,
        llm=llm,
        knowledge_transport=httpx.ASGITransport(app=knowledge.app),
        billing_transport=httpx.ASGITransport(app=billing.app),
        span_exporter=span_exporter,
    )
    gateway = build_gateway(
        GatewaySettings(core=core("gateway"), staff_emails=frozenset({STAFF_EMAIL})),
        infra,
        transports={
            "knowledge": httpx.ASGITransport(app=knowledge.app),
            "agents": httpx.ASGITransport(app=agents.app),
            "billing": httpx.ASGITransport(app=billing.app),
            "growth": httpx.ASGITransport(app=growth.app),
        },
        webhook_transport=webhook_transport,
        span_exporter=span_exporter,
    )
    public = create_functions_app(publish=infra.event_bus.publish)
    public.mount("/", gateway.app)
    return DevStack(infra, gateway, knowledge, agents, billing, growth, public)


async def with_stack(body: Callable[[DevStack], Awaitable[None]], **options: Any) -> None:
    stack = build_devstack(**options)
    await stack.start()
    try:
        await body(stack)
    finally:
        await stack.stop()
