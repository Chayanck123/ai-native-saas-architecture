"""The complete customer journey across every service, serverless function included."""

from __future__ import annotations

import json
import time

import pytest

from devstack.stack import STAFF_EMAIL, DevStack
from platform_core.clock import ManualClock
from platform_core.security import sign_payload
from platform_core.testing import SpanRecorder
from serverless_functions.common import secret_setting

from .conftest import HANDBOOK, client_for, signup

STRIPE_SECRET = "whsec_e2e"


def service_of(span: object) -> str:
    return str(span.resource.attributes.get("service.name"))  # type: ignore[attr-defined]


async def test_customer_journey_from_first_visit_to_paid_plan(
    stack: DevStack, clock: ManualClock, spans: SpanRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", STRIPE_SECRET)
    secret_setting.cache_clear()

    # 1. An anonymous visitor lands on the marketing site from a Google ad (edge beacon).
    async with stack.client() as browser:
        beacon = await browser.post(
            "/t",
            json={
                "events": [
                    {
                        "id": "pv-1",
                        "name": "page_viewed",
                        "anonymous_id": "anon_42",
                        "properties": {"path": "/", "utm_source": "google_ads"},
                    }
                ]
            },
        )
    assert beacon.status_code == 202
    await stack.drain()
    clock.advance(minutes=10)

    # 2. Self-serve signup (product-led growth: no sales call, no card).
    owner = await signup(stack, "founder@journey.io", "Journey Inc", anonymous_id="anon_42")
    tenant_id = owner["tenant"]["id"]

    async with client_for(stack, owner["access_token"]) as client:
        # 3. Upload knowledge; it is indexed asynchronously.
        doc = await client.post("/v1/documents", json={"title": "Handbook", "content": HANDBOOK})
        assert doc.status_code == 202
        await stack.drain()
        assert (await client.get(f"/v1/documents/{doc.json()['id']}")).json()["status"] == "indexed"

        # 4. The "aha moment": the agent answers from the workspace's own documents.
        clock.advance(hours=1)
        created = await client.post(
            "/v1/agents/runs", json={"question": "How long do I have to get a refund?"}
        )
        assert created.status_code == 202
        await stack.drain()
        run = (await client.get(f"/v1/agents/runs/{created.json()['id']}")).json()
        assert run["status"] == "succeeded"
        assert "30 days" in run["answer"]
        assert run["citations"][0]["document_title"] == "Handbook"

        # 5. Feedback feeds the data flywheel.
        feedback = await client.post(f"/v1/agents/runs/{run['id']}/feedback", json={"rating": 1})
        assert feedback.status_code == 201
        await stack.drain()

        # 6. Usage-based billing saw every token.
        usage = (await client.get("/v1/billing/usage")).json()
        assert usage["meters"]["agent_runs"]["quantity"] == 1
        assert usage["meters"]["llm_output_tokens"]["quantity"] == run["usage"]["output_tokens"]
        assert usage["meters"]["embedding_tokens"]["quantity"] > 0

    # 7. The customer upgrades in Stripe Checkout; Stripe calls our webhook Lambda.
    clock.advance(days=2)
    stripe_event = {
        "id": "evt_upgrade_1",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_1",
                "customer": "cus_1",
                "status": "active",
                "quantity": 1,
                "metadata": {"tenant_id": tenant_id, "plan": "pro"},
            }
        },
    }
    body = json.dumps(stripe_event).encode()
    async with stack.client() as stripe:
        response = await stripe.post(
            "/webhooks/stripe",
            content=body,
            headers={
                "stripe-signature": sign_payload(STRIPE_SECRET, body, int(time.time())),
                "content-type": "application/json",
            },
        )
        replay = await stripe.post(
            "/webhooks/stripe",
            content=body,
            headers={
                "stripe-signature": sign_payload(STRIPE_SECRET, body, int(time.time())),
                "content-type": "application/json",
            },
        )  # Stripe retries are harmless
    assert response.status_code == replay.status_code == 200
    await stack.drain()
    async with client_for(stack, owner["access_token"]) as client:
        subscription = (await client.get("/v1/billing/subscription")).json()
        me = (await client.get("/v1/me")).json()
    assert subscription["plan"]["key"] == "pro"
    assert subscription["provider"] == "stripe"
    assert me["tenant"]["plan"] == "pro"  # the gateway learned it from an event

    # 8. The growth team sees the whole funnel, attribution and revenue.
    staff = await signup(stack, STAFF_EMAIL, "Ainative HQ")
    async with client_for(stack, staff["access_token"]) as ops:
        funnel = (
            await ops.get(
                "/v1/analytics/funnel",
                params={
                    "steps": "page_viewed,signed_up,document_indexed,agent_run_succeeded,"
                    "converted_to_paid"
                },
            )
        ).json()
        attribution = (
            await ops.get("/v1/analytics/attribution", params={"model": "first_touch"})
        ).json()
        revenue = (await ops.get("/v1/billing/revenue")).json()
        overview = (await ops.get("/v1/analytics/overview")).json()
        schedule = await ops.post("/_dev/scheduler/pql_scoring")
        queues = (await ops.get("/v1/admin/queues")).json()
    assert [step["count"] for step in funnel["steps"]] == [1, 1, 1, 1, 1]
    assert attribution["channels"][0] == {
        "channel": "google_ads",
        "conversions": 1.0,
        "mrr_cents": 4900.0,
    }
    assert revenue["mrr_cents"] == 4_900
    assert revenue["arr_cents"] == 58_800
    assert overview["paying_accounts"] == 1
    assert schedule.status_code == 200
    assert all(stats["dead"] == 0 for stats in queues.values())

    # 9. One distributed trace follows the question through every service,
    #    across both synchronous HTTP calls and asynchronous queue hops.
    [request_span] = [
        s
        for s in spans.spans("POST /v1/agents/*")
        if service_of(s) == "gateway"
        and s.attributes
        and s.attributes.get("url.path") == "/v1/agents/runs"
    ]
    trace_id = request_span.context.trace_id  # type: ignore[union-attr]
    in_trace = [s for s in spans.spans() if s.context and s.context.trace_id == trace_id]
    services = {service_of(s) for s in in_trace}
    names = {s.name for s in in_trace}
    assert services == {"gateway", "agents", "billing", "knowledge", "growth"}
    assert "task agents.execute_run" in names  # async hop via the event bus + queue
    assert "POST /v1/search" in names  # knowledge, called by the agent's tool
    assert "GET /v1/billing/entitlements" in names  # the quota check before the run
    assert "task billing.record_usage" in names  # metering, two async hops later
    assert "task growth.project" in names  # the activation milestone in analytics
    assert any(n.startswith("chat ") for n in names)  # the LLM call itself
