"""Assemble the agents service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from agents_service.anthropic_llm import AnthropicLLM
from agents_service.api import router
from agents_service.llm import InstrumentedLLM, LLMClient
from agents_service.models import TENANT_TABLES, Base
from agents_service.offline_llm import OfflineLLM
from agents_service.service import AgentService
from agents_service.settings import AgentsSettings
from platform_core.catalog import Events
from platform_core.clients import ServiceClient
from platform_core.config import ConfigurationError
from platform_core.infrastructure import Infrastructure
from platform_core.retry import RetryPolicy
from platform_core.service import ServiceRuntime, create_service_app
from platform_core.tasks import Task
from platform_core.worker import Worker, event_of


@dataclass
class AgentsServiceApp:
    runtime: ServiceRuntime
    service: AgentService
    app: FastAPI
    worker: Worker


def make_llm(settings: AgentsSettings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        if not settings.model:
            raise ConfigurationError("AGENTS_MODEL must name the Anthropic model to use")
        return AnthropicLLM(
            model=settings.model,
            effort=settings.effort,
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
        )
    return OfflineLLM()


def build_worker(runtime: ServiceRuntime, service: AgentService) -> Worker:
    worker = Worker(runtime, lease_seconds=900)

    @worker.task(
        "agents.execute_run",
        retry_policy=RetryPolicy(max_attempts=4, base_delay=5.0, max_delay=120.0),
    )
    async def execute_run(task: Task) -> dict[str, Any]:
        return await service.execute_run(event_of(task).data["run_id"])

    worker.on_event(Events.AGENT_RUN_REQUESTED, task="agents.execute_run")
    return worker


def build_agents(
    settings: AgentsSettings,
    infra: Infrastructure,
    *,
    llm: LLMClient | None = None,
    knowledge_transport: httpx.AsyncBaseTransport | None = None,
    billing_transport: httpx.AsyncBaseTransport | None = None,
    span_exporter: SpanExporter | None = None,
) -> AgentsServiceApp:
    runtime = ServiceRuntime.create(
        "agents",
        settings.core,
        infra,
        metadata=Base.metadata,
        tenant_tables=TENANT_TABLES,
        span_exporter=span_exporter,
    )
    instrumented = InstrumentedLLM(
        llm or make_llm(settings), tracer=runtime.tracer, metrics=runtime.metrics
    )
    knowledge = ServiceClient(
        "knowledge",
        tokens=runtime.internal_tokens,
        base_url=settings.knowledge_url,
        transport=knowledge_transport,
        tracer=runtime.tracer,
    )
    billing = ServiceClient(
        "billing",
        tokens=runtime.internal_tokens,
        base_url=settings.billing_url,
        transport=billing_transport,
        timeout=3.0,
        tracer=runtime.tracer,
    )
    service = AgentService(runtime, settings, instrumented, knowledge=knowledge, billing=billing)
    app = create_service_app(
        runtime,
        title="Agents Service",
        description="Agentic orchestration: planner, tool-using actor and critic on an LLM.",
        routers=[router],
        on_shutdown=[knowledge.aclose, billing.aclose],
    )
    app.state.agents = service
    return AgentsServiceApp(runtime, service, app, build_worker(runtime, service))
