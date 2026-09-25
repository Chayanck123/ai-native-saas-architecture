"""End-to-end agent runs across agents + knowledge + billing (offline model)."""

from __future__ import annotations

import json

import httpx

from platform_core.catalog import Events, Meters
from platform_core.events import InMemoryEventBus
from platform_core.rbac import Role
from platform_core.testing import internal_headers, tenant_principal

from .conftest import REFUNDS, Stack, close_stack, make_stack


def events(stack: Stack, event_type: str) -> list[dict[str, object]]:
    assert isinstance(stack.infra.event_bus, InMemoryEventBus)
    return [e.data for e in stack.infra.event_bus.published if e.type == event_type]


async def ask(stack: Stack, tenant_id: str, question: str) -> dict[str, object]:
    async with stack.client(tenant_id) as client:
        created = await client.post("/v1/agents/runs", json={"question": question})
        assert created.status_code == 202, created.text
        assert created.json()["status"] == "queued"
        await stack.drain()
        run = (
            await client.get(f"/v1/agents/runs/{created.json()['id']}", params={"steps": "true"})
        ).json()
    assert isinstance(run, dict)
    return run


async def billing_usage(stack: Stack, tenant_id: str) -> dict[str, object]:
    headers = internal_headers(stack.billing.runtime.internal_tokens, tenant_principal(tenant_id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.billing.app),
        base_url="http://billing",
        headers=headers,
    ) as client:
        data = (await client.get("/v1/billing/usage")).json()
    assert isinstance(data, dict)
    return data


async def test_grounded_answer_with_citations_is_metered(stack: Stack, tenant_id: str) -> None:
    await stack.onboard(tenant_id, ("Billing handbook", REFUNDS))
    run = await ask(stack, tenant_id, "How many days do I have to request a refund?")

    assert run["status"] == "succeeded"
    assert "30 days" in str(run["answer"])
    [citation] = run["citations"]  # type: ignore[misc]
    assert citation["document_title"] == "Billing handbook"
    assert f"[{citation['number']}]" in str(run["answer"])
    assert run["plan"]
    assert float(run["critic_score"]) >= 0.6  # type: ignore[arg-type]
    step_types = [step["type"] for step in run["steps"]]  # type: ignore[union-attr]
    assert step_types[0] == "plan"
    assert "tool_call" in step_types and step_types[-1] == "critique"

    [completed] = events(stack, Events.AGENT_RUN_COMPLETED)
    assert completed["status"] == "succeeded"
    usage = await billing_usage(stack, tenant_id)
    meters = usage["meters"]
    assert isinstance(meters, dict)
    assert meters[Meters.AGENT_RUNS]["quantity"] == 1
    assert meters[Meters.LLM_INPUT_TOKENS]["quantity"] == run["usage"]["input_tokens"]  # type: ignore[index]
    assert meters[Meters.LLM_OUTPUT_TOKENS]["quantity"] > 0


async def test_knowledge_gaps_are_routed_to_human_review(stack: Stack, tenant_id: str) -> None:
    await stack.onboard(tenant_id, ("Billing handbook", REFUNDS))
    run = await ask(stack, tenant_id, "Which office locations do you have in Brazil?")
    assert run["status"] == "needs_review"
    assert run["knowledge_gap"] is True

    async with stack.client(tenant_id, Role.MEMBER) as member:
        denied = await member.post(f"/v1/agents/runs/{run['id']}/review", json={"approve": True})
        assert denied.status_code == 403
    async with stack.client(tenant_id, Role.ADMIN) as admin:
        reviewed = await admin.post(
            f"/v1/agents/runs/{run['id']}/review",
            json={"approve": True, "corrected_answer": "We have no offices in Brazil."},
        )
        assert reviewed.status_code == 200
        body = reviewed.json()
        again = await admin.post(f"/v1/agents/runs/{run['id']}/review", json={"approve": True})
    assert body["status"] == "succeeded"
    assert body["answer"] == "We have no offices in Brazil."
    assert body["review"]["approved"] is True
    assert again.status_code == 409  # only needs_review runs can be reviewed


async def test_feedback_improves_retrieval_and_builds_a_dataset(
    stack: Stack, tenant_id: str
) -> None:
    await stack.onboard(tenant_id, ("Billing handbook", REFUNDS))
    run = await ask(stack, tenant_id, "How many days do I have to request a refund?")
    async with stack.client(tenant_id, Role.MEMBER) as member:
        rated = await member.post(f"/v1/agents/runs/{run['id']}/feedback", json={"rating": 1})
        assert rated.status_code == 201
        twice = await member.post(f"/v1/agents/runs/{run['id']}/feedback", json={"rating": -1})
        assert twice.status_code == 409
    await stack.drain()

    [feedback] = events(stack, Events.AGENT_FEEDBACK_SUBMITTED)
    cited = [c["chunk_id"] for c in run["citations"]]  # type: ignore[union-attr]
    assert feedback["chunk_ids"] == cited
    headers = internal_headers(stack.knowledge.runtime.internal_tokens, tenant_principal(tenant_id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.knowledge.app),
        base_url="http://knowledge",
        headers=headers,
    ) as client:
        hits = (await client.post("/v1/search", json={"query": "refund"})).json()["results"]
    boosted = {h["chunk_id"]: h["feedback_quality"] for h in hits}
    assert all(boosted[chunk] > 0.5 for chunk in cited)

    async with stack.client(tenant_id, Role.ADMIN) as admin:
        export = await admin.get("/v1/agents/dataset")
    assert export.headers["content-type"].startswith("application/x-ndjson")
    [row] = [json.loads(line) for line in export.text.splitlines()]
    assert row["run_id"] == run["id"]
    assert row["label_source"] == "user_feedback"


async def test_free_plan_exhaustion_returns_402_with_upgrade_prompt(
    stack: Stack, tenant_id: str
) -> None:
    await stack.onboard(tenant_id)
    await stack.billing.runtime.publish(
        stack.billing.runtime.event(
            Events.USAGE_RECORDED,
            {"meter": Meters.AGENT_RUNS, "quantity": 1_000, "idempotency_key": "burn"},
            tenant_id=tenant_id,
        )
    )
    await stack.drain()
    async with stack.client(tenant_id) as client:
        response = await client.post("/v1/agents/runs", json={"question": "anything"})
    assert response.status_code == 402
    problem = response.json()
    assert problem["code"] == "quota_exceeded"
    assert problem["upgrade_to"] == "pro"


async def test_runs_fail_open_when_billing_is_down(tenant_id: str) -> None:
    def billing_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "maintenance"})

    stack = await make_stack(billing_transport=httpx.MockTransport(billing_down))
    try:
        async with stack.client(tenant_id) as client:
            response = await client.post("/v1/agents/runs", json={"question": "hello?"})
        assert response.status_code == 202
        metric = stack.agents.runtime.metrics.registry.get_sample_value(
            "entitlement_fail_open_total", {"service": "agents"}
        )
        assert metric == 1
    finally:
        await close_stack(stack)


async def test_calculator_tool_is_used_for_arithmetic(stack: Stack, tenant_id: str) -> None:
    await stack.onboard(tenant_id, ("Billing handbook", REFUNDS))
    run = await ask(stack, tenant_id, "What is 1200 * 12 / 3?")
    assert "= 4800" in str(run["answer"])
    names = [s["name"] for s in run["steps"] if s["type"] == "tool_call"]  # type: ignore[union-attr]
    assert "calculator" in names


async def test_tenants_cannot_read_each_others_runs(stack: Stack, tenant_id: str) -> None:
    await stack.onboard(tenant_id, ("Billing handbook", REFUNDS))
    run = await ask(stack, tenant_id, "How many days do I have to request a refund?")
    async with stack.client("ten_intruder") as intruder:
        assert (await intruder.get(f"/v1/agents/runs/{run['id']}")).status_code == 404
        assert (await intruder.get("/v1/agents/runs")).json()["total"] == 0
