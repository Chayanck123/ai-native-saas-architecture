from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from agents_service.app import AgentsServiceApp, build_agents
from agents_service.llm import LLMClient, LLMResponse, LLMUsage, Message, ToolCall, ToolSpec
from agents_service.settings import AgentsSettings
from billing_service.app import BillingServiceApp, build_billing
from billing_service.settings import BillingSettings
from knowledge_service.app import KnowledgeServiceApp, build_knowledge
from knowledge_service.settings import KnowledgeSettings
from platform_core.catalog import Events
from platform_core.clock import ManualClock
from platform_core.config import CoreSettings
from platform_core.ids import new_id
from platform_core.infrastructure import Infrastructure
from platform_core.rbac import Role
from platform_core.testing import internal_headers, tenant_principal
from platform_core.worker import Worker

MEMORY = "sqlite+aiosqlite:///:memory:"

REFUNDS = """# Billing
## Refund policy
Customers can request a full refund within 30 days of purchase. After 30 days, refunds are
prorated for annual plans only. Refunds are paid to the original payment method.

## Invoices
Invoices are emailed on the first day of each month. Enterprise customers can pay by wire.
"""


@dataclass
class Stack:
    infra: Infrastructure
    clock: ManualClock
    knowledge: KnowledgeServiceApp
    billing: BillingServiceApp
    agents: AgentsServiceApp

    @property
    def workers(self) -> list[Worker]:
        return [self.knowledge.worker, self.billing.worker, self.agents.worker]

    async def drain(self) -> None:
        """Run every worker until the whole system is quiet."""
        for _ in range(50):
            progress = 0
            for worker in self.workers:
                progress += await worker.run_until_idle()
            if progress == 0:
                return

    def client(self, tenant_id: str, role: Role = Role.OWNER) -> httpx.AsyncClient:
        headers = internal_headers(
            self.agents.runtime.internal_tokens,
            tenant_principal(tenant_id, role=role, subject=f"usr_{role}"),
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.agents.app),
            base_url="http://agents",
            headers=headers,
        )

    async def onboard(self, tenant_id: str, *documents: tuple[str, str]) -> None:
        """Provision billing for a tenant and index its documents."""
        await self.billing.runtime.publish(
            self.billing.runtime.event(Events.TENANT_CREATED, {}, tenant_id=tenant_id)
        )
        headers = internal_headers(
            self.knowledge.runtime.internal_tokens, tenant_principal(tenant_id)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.knowledge.app),
            base_url="http://knowledge",
            headers=headers,
        ) as client:
            for title, content in documents:
                response = await client.post(
                    "/v1/documents", json={"title": title, "content": content}
                )
                assert response.status_code == 202
        await self.drain()


async def make_stack(
    *,
    llm: LLMClient | None = None,
    billing_transport: httpx.AsyncBaseTransport | None = None,
    **agent_overrides: Any,
) -> Stack:
    clock = ManualClock()
    infra = Infrastructure.in_memory(clock)
    knowledge = build_knowledge(
        KnowledgeSettings(core=CoreSettings(service_name="knowledge", database_url=MEMORY)), infra
    )
    billing = build_billing(
        BillingSettings(core=CoreSettings(service_name="billing", database_url=MEMORY)), infra
    )
    agents = build_agents(
        AgentsSettings(
            core=CoreSettings(service_name="agents", database_url=MEMORY), **agent_overrides
        ),
        infra,
        llm=llm,
        knowledge_transport=httpx.ASGITransport(app=knowledge.app),
        billing_transport=billing_transport or httpx.ASGITransport(app=billing.app),
    )
    stack = Stack(infra, clock, knowledge, billing, agents)
    for built in (knowledge, billing, agents):
        await built.runtime.start()
        await built.worker.setup()
    return stack


async def close_stack(stack: Stack) -> None:
    for built in (stack.knowledge, stack.billing, stack.agents):
        await built.runtime.stop()


@pytest.fixture
async def stack() -> AsyncIterator[Stack]:
    built = await make_stack()
    yield built
    await close_stack(built)


@pytest.fixture
def tenant_id() -> str:
    return new_id("ten")


class ScriptedLLM:
    """Replays prepared responses and records every request (for orchestrator tests)."""

    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return "scripted"

    @property
    def provider(self) -> str:
        return "scripted"

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_schema: dict[str, Any] | None = None,
        disable_tools: bool = False,
    ) -> LLMResponse:
        self.requests.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": tools,
                "json_schema": json_schema,
                "disable_tools": disable_tools,
            }
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def text(value: str, *, tokens: int = 10) -> LLMResponse:
    return LLMResponse(
        value,
        [],
        "end_turn",
        LLMUsage(tokens, tokens),
        "scripted",
        [{"type": "text", "text": value}],
    )


def tool_use(*calls: tuple[str, str, dict[str, Any]], tokens: int = 10) -> LLMResponse:
    tool_calls = [ToolCall(call_id, name, args) for call_id, name, args in calls]
    content = [
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in tool_calls
    ]
    return LLMResponse("", tool_calls, "tool_use", LLMUsage(tokens, tokens), "scripted", content)
