"""A narrated, end-to-end simulation of the business running on the platform.

``uv run devstack demo`` spins up every service in-process, then simulates five
weeks of a SaaS business on a virtual clock: marketing traffic, self-serve
signups, activation through the AI agent, feedback, usage-based billing,
upgrades through Stripe, an automated A/B test - and finally prints what the
growth and revenue dashboards show. No API keys, Docker or network needed.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from devstack.stack import STAFF_EMAIL, DevStack, build_devstack
from devstack.webhooks import WebhookReceiver
from growth_service.service import ClientEvent
from platform_core.clock import ManualClock
from platform_core.security import sign_payload
from platform_core.testing import SpanRecorder

HANDBOOK = """# Acme Cloud handbook

## Refund policy
Customers can request a full refund within 30 days of purchase. After 30 days, refunds are
prorated for annual plans only. Refunds are paid to the original payment method within five
business days.

## Plans
The Pro plan includes priority support, SSO and a 99.9% uptime SLA. Enterprise adds a
dedicated success manager and custom data retention.

## Security
All customer data is encrypted at rest with AES-256 and in transit with TLS 1.3.
Error ERR-4521 means the SSO certificate has expired; upload a new certificate in the admin
console to fix it.
"""

QUESTIONS = [
    "How long do I have to request a refund?",
    "Is customer data encrypted?",
    "What does error ERR-4521 mean?",
    "What does the Pro plan include?",
]
CHANNELS = ["google_ads", "blog", "linkedin", "newsletter"]
STRIPE_SECRET = "whsec_demo_secret"  # noqa: S105 - demo-only value
PASSWORD = "demo-password-123"  # noqa: S105 - demo-only value


def headline(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def line(text: str) -> None:
    print(f"   {text}")


def money(cents: float) -> str:
    return f"${cents / 100:,.2f}"


class Demo:
    def __init__(self) -> None:
        self.clock = ManualClock(datetime(2026, 5, 4, 9, 0, tzinfo=UTC))  # a Monday
        self.spans = SpanRecorder()
        self.receiver = WebhookReceiver(self.clock)
        self.stack: DevStack = build_devstack(
            clock=self.clock,
            span_exporter=self.spans.exporter,
            webhook_transport=self.receiver.transport,
        )
        self.tokens: dict[str, str] = {}
        self.tenants: dict[str, str] = {}

    def client(self, token: str | None = None, **headers: str) -> httpx.AsyncClient:
        if token:
            headers["authorization"] = f"Bearer {token}"
        return self.stack.client(**headers)

    async def call(
        self,
        method: str,
        path: str,
        token: str | None = None,
        expect: int | tuple[int, ...] = (200, 201, 202, 204),
        **kwargs: Any,
    ) -> Any:
        async with self.client(token) as client:
            response = await client.request(method, path, **kwargs)
        allowed = (expect,) if isinstance(expect, int) else expect
        if response.status_code not in allowed:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text}")
        return response.json() if response.content else None

    # ------------------------------------------------------------ scenario
    async def run(self) -> None:
        stack = self.stack
        await stack.start()
        try:
            await self.acquisition_and_signup()
            await self.activation()
            await self.flywheel()
            await self.api_first()
            await self.monetization()
            await self.retention_weeks()
            await self.growth_team()
            await self.observability()
        finally:
            await stack.stop()

    async def acquisition_and_signup(self) -> None:
        headline("1. Acquisition - marketing traffic hits the edge beacon (AWS Lambda)")
        visitors = [(f"anon_{i}", CHANNELS[i % len(CHANNELS)]) for i in range(40)]
        events = [
            {
                "id": f"pv-{anon}",
                "name": "page_viewed",
                "anonymous_id": anon,
                "properties": {"path": "/", "utm_source": channel},
            }
            for anon, channel in visitors
        ]
        for start in range(0, len(events), 20):
            await self.call("POST", "/t", json={"events": events[start : start + 20]}, expect=202)
        await self.stack.drain()
        line(f"{len(visitors)} anonymous visitors from {', '.join(CHANNELS)}")

        headline("2. Product-led signup - self-serve, free plan, no sales call")
        for index in range(12):
            anon, _ = visitors[index]
            self.clock.advance(hours=2)
            company = f"company{index:02d}"
            body = await self.call(
                "POST",
                "/v1/auth/signup",
                json={
                    "email": f"founder@{company}.io",
                    "password": PASSWORD,
                    "workspace_name": company.title(),
                    "anonymous_id": anon,
                },
            )
            self.tokens[company] = body["access_token"]
            self.tenants[company] = body["tenant"]["id"]
        await self.stack.drain()
        line("12 workspaces created; each got a free subscription and a first-touch channel")

    async def activation(self) -> None:
        headline("3. Activation - customers upload knowledge and ask the AI agent")
        for company in list(self.tokens)[:8]:
            await self.call(
                "POST",
                "/v1/documents",
                self.tokens[company],
                json={"title": "Acme Cloud handbook", "content": HANDBOOK},
            )
        await self.stack.drain()
        for index, company in enumerate(list(self.tokens)[:8]):
            self.clock.advance(minutes=30)
            question = QUESTIONS[index % len(QUESTIONS)]
            run = await self.call(
                "POST", "/v1/agents/runs", self.tokens[company], json={"question": question}
            )
            await self.stack.drain()
            run = await self.call("GET", f"/v1/agents/runs/{run['id']}", self.tokens[company])
            if index < 3:
                line(f"Q: {question}")
                line(f"A: {run['answer']}")
                usage = run["usage"]
                line(
                    f"   status={run['status']}  critic={run['critic_score']}  "
                    f"tools={usage['tool_calls']}  tokens={usage['input_tokens']}+"
                    f"{usage['output_tokens']}"
                )
        company = "company00"
        run = await self.call(
            "POST",
            "/v1/agents/runs",
            self.tokens[company],
            json={"question": "Which offices do you have in Brazil?"},
        )
        await self.stack.drain()
        run = await self.call("GET", f"/v1/agents/runs/{run['id']}", self.tokens[company])
        line(
            f"Q: Which offices do you have in Brazil?  -> status={run['status']} "
            f"(knowledge gap={run['knowledge_gap']}): routed to a human"
        )
        reviewed = await self.call(
            "POST",
            f"/v1/agents/runs/{run['id']}/review",
            self.tokens[company],
            json={"approve": True, "corrected_answer": "We have no offices in Brazil."},
        )
        line(
            f"   admin reviewed it -> status={reviewed['status']}; the correction becomes "
            "training data"
        )

    async def flywheel(self) -> None:
        headline("4. Data flywheel - feedback improves retrieval for this customer only")
        token = self.tokens["company01"]
        runs = await self.call("GET", "/v1/agents/runs", token)
        run = runs["data"][0]
        before = await self.call("POST", "/v1/search", token, json={"query": run["question"]})
        await self.call("POST", f"/v1/agents/runs/{run['id']}/feedback", token, json={"rating": 1})
        await self.stack.drain()
        after = await self.call("POST", "/v1/search", token, json={"query": run["question"]})
        cited = {c["chunk_id"] for c in run["citations"]}
        quality = [h["feedback_quality"] for h in after["results"] if h["chunk_id"] in cited]
        line(
            f"cited passage helpfulness prior: 0.5 -> {max(quality):.2f}; top score "
            f"{before['results'][0]['score']:.4f} -> {after['results'][0]['score']:.4f}"
        )
        export = await self._raw("GET", "/v1/agents/dataset", self.tokens["company00"])
        line(f"labelled Q&A pairs exportable for evals/fine-tuning: {len(export.splitlines())}")

    async def _raw(self, method: str, path: str, token: str) -> str:
        async with self.client(token) as client:
            return (await client.request(method, path)).text

    async def api_first(self) -> None:
        headline("5. API-first integrations - scoped API keys, idempotency and webhooks")
        token = self.tokens["company02"]
        key = await self.call(
            "POST", "/v1/api-keys", token, json={"name": "CMS sync", "scopes": ["documents:write"]}
        )
        hook = await self.call(
            "POST",
            "/v1/webhooks",
            token,
            json={
                "url": "https://hooks.company02.io/ainative",
                "event_types": ["document.indexed"],
            },
        )
        document = {"title": "Release notes", "content": "Version 2 ships SSO for all plans."}
        headers = {"idempotency-key": "cms-sync-0001"}
        async with self.client(**{"x-api-key": key["key"]}) as integration:
            first = await integration.post("/v1/documents", json=document, headers=headers)
            retry = await integration.post("/v1/documents", json=document, headers=headers)
        line(
            f"API key {key['id']} uploaded {first.json()['id']}; network retry returned the "
            f"same id (replayed={retry.headers.get('idempotent-replayed')})"
        )
        await self.stack.drain()
        delivery = self.receiver.received[-1]
        line(
            f"webhook {delivery.event['type']} delivered, signature valid="
            f"{self.receiver.verify(delivery, hook['secret'])}"
        )

    async def monetization(self) -> None:
        headline("6. Usage-based monetization - credits, quota nudges and upgrades")
        token = self.tokens["company03"]
        blocked = None
        for _ in range(40):
            async with self.client(token) as client:
                response = await client.post(
                    "/v1/agents/runs",
                    json={"question": "Summarise every policy we have in detail?"},
                )
            await self.stack.drain()
            if response.status_code == 402:
                blocked = response.json()
                break
            # Heavy usage: pretend each run also burned a large model context.
            await self.stack.billing.runtime.publish(
                self.stack.billing.runtime.event(
                    "usage.recorded",
                    {
                        "meter": "llm_input_tokens",
                        "quantity": 60_000,
                        "idempotency_key": f"bulk-{response.json()['id']}",
                    },
                    tenant_id=self.tenants["company03"],
                )
            )
        await self.stack.drain()
        usage = await self.call("GET", "/v1/billing/usage", token)
        line(
            f"company03 used {usage['credits']['used']:.0f} of "
            f"{usage['credits']['included']} free credits"
        )
        if blocked:
            line(f"next run -> HTTP 402 {blocked['code']}: upgrade_to={blocked['upgrade_to']}")

        for company, plan in [
            ("company03", "pro"),
            ("company01", "pro"),
            ("company04", "enterprise"),
        ]:
            self.clock.advance(hours=6)
            await self._stripe_upgrade(company, plan)
        await self.stack.drain()
        subscription = await self.call("GET", "/v1/billing/subscription", token)
        line(
            f"3 workspaces upgraded via Stripe Checkout -> webhook Lambda -> billing; "
            f"company03 is now on {subscription['plan']['name']} "
            f"({money(subscription['recurring_cents'])}/month)"
        )

    async def _stripe_upgrade(self, company: str, plan: str) -> None:
        event = {
            "id": f"evt_{company}_{plan}",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": f"sub_{company}",
                    "customer": f"cus_{company}",
                    "status": "active",
                    "quantity": 1,
                    "metadata": {"tenant_id": self.tenants[company], "plan": plan},
                }
            },
        }
        body = json.dumps(event).encode()
        async with self.client() as stripe:
            response = await stripe.post(
                "/webhooks/stripe",
                content=body,
                headers={
                    "stripe-signature": sign_payload(STRIPE_SECRET, body, int(time.time())),
                    "content-type": "application/json",
                },
            )
        assert response.status_code == 200, response.text

    async def _ask(self, token: str, question: str) -> dict[str, Any]:
        run = await self.call("POST", "/v1/agents/runs", token, json={"question": question})
        await self.stack.drain()
        result = await self.call("GET", f"/v1/agents/runs/{run['id']}", token)
        assert isinstance(result, dict)
        return result

    async def retention_weeks(self) -> None:
        headline("7. Weeks 2-5 - some workspaces keep coming back, some drift away")
        for week in range(1, 5):
            self.clock.advance(weeks=1)
            active = [c for i, c in enumerate(self.tokens) if i < 8 - week]
            for index, company in enumerate(active):
                await self._ask(self.tokens[company], QUESTIONS[(week + index) % len(QUESTIONS)])
            line(f"week {week + 1}: {len(active)} workspaces asked the agent questions")

        headline("7b. A busy free-plan team becomes a product-qualified lead (PQL)")
        owner = self.tokens["company05"]
        tokens = [owner]
        for email in ("pm@company05.io", "ops@company05.io"):
            invite = await self.call(
                "POST", "/v1/members", owner, json={"email": email, "role": "member"}
            )
            accepted = await self.call(
                "POST",
                "/v1/auth/accept-invitation",
                json={"token": invite["invitation_token"], "password": PASSWORD},
            )
            tokens.append(accepted["access_token"])
        await self.stack.drain()
        for token in tokens:
            for question in QUESTIONS:
                await self._ask(token, question)
        line("company05: 3 members asked 12 questions in a week -> flagged for sales-assist")

    async def growth_team(self) -> None:
        headline("8. Growth dashboards (platform staff only)")
        staff = await self.call(
            "POST",
            "/v1/auth/signup",
            json={"email": STAFF_EMAIL, "password": PASSWORD, "workspace_name": "Ainative HQ"},
        )
        token = staff["access_token"]
        for channel, cents in [
            ("google_ads", 120_000),
            ("linkedin", 90_000),
            ("newsletter", 15_000),
            ("blog", 20_000),
        ]:
            await self.call(
                "PUT",
                "/v1/analytics/marketing-spend",
                token,
                json={"channel": channel, "month": "2026-05", "amount_cents": cents},
            )
        await self._experiment(token)
        await self.stack.drain()

        overview = await self.call("GET", "/v1/analytics/overview", token)
        line(
            f"accounts={overview['accounts']}  activation={overview['activation_rate']:.0%}  "
            f"median time-to-value={overview['median_hours_to_value']}h  "
            f"paying={overview['paying_accounts']}  PQLs={overview['product_qualified_leads']}"
        )
        funnel = await self.call(
            "GET",
            "/v1/analytics/funnel",
            token,
            params={
                "steps": "page_viewed,signed_up,document_indexed,agent_run_succeeded,"
                "converted_to_paid",
                "window_days": 60,
            },
        )
        line("funnel: " + "  ->  ".join(f"{s['name']} {s['count']}" for s in funnel["steps"]))
        cohorts = await self.call(
            "GET", "/v1/analytics/cohorts", token, params={"period": "week", "periods": 5}
        )
        for cohort in cohorts["cohorts"][:2]:
            retention = " ".join("  -  " if r is None else f"{r:4.0%}" for r in cohort["retention"])
            line(f"cohort {cohort['cohort_start'][:10]} (n={cohort['size']}): {retention}")
        attribution = await self.call(
            "GET", "/v1/analytics/attribution", token, params={"model": "position_based"}
        )
        line(
            "attribution (U-shaped): "
            + ", ".join(f"{c['channel']} {c['conversions']:.2f}" for c in attribution["channels"])
        )
        economics = await self.call(
            "GET", "/v1/analytics/unit-economics", token, params={"model": "first_touch"}
        )
        blended = economics["blended"]
        line(
            f"blended ARPA {money(blended['arpa_cents'])}  CAC "
            f"{money(blended['cac_cents'] or 0)}  LTV {money(blended['ltv_cents'])}  "
            f"LTV:CAC {blended['ltv_to_cac']}  payback {blended['payback_months']} months"
        )
        revenue = await self.call("GET", "/v1/billing/revenue", token)
        margin = revenue["gross_margin"]
        line(
            f"MRR {money(revenue['mrr_cents'])}  ARR {money(revenue['arr_cents'])}  "
            f"paying accounts {revenue['paying_accounts']}"
        )
        if margin is not None:
            line(
                f"this month's model inference cost {money(revenue['inference_cost_cents'])} "
                f"-> gross margin {margin:.2%}"
            )

    async def _experiment(self, token: str) -> None:
        await self.call(
            "POST",
            "/v1/experiments",
            token,
            json={
                "key": "onboarding-checklist",
                "name": "Onboarding checklist",
                "variants": [
                    {"name": "control", "weight": 50},
                    {"name": "checklist", "weight": 50},
                ],
                "primary_metric": "first_document_uploaded",
                "min_sample_per_variant": 100,
            },
        )
        await self.call("POST", "/v1/experiments/onboarding-checklist/start", token)
        for wave, users in enumerate([range(0, 300), range(300, 800)], start=1):
            await self._experiment_traffic(token, users)
            results = await self.call("GET", "/v1/experiments/onboarding-checklist/results", token)
            rates = ", ".join(
                f"{v['name']} {v['rate']:.0%} of {v['exposures']}" for v in results["variants"]
            )
            decisions = await self.call("POST", "/v1/analytics/jobs/experiment_autopilot", token)
            decision = decisions["decisions"][0] if decisions["decisions"] else results["decision"]
            line(
                f"A/B wave {wave}: {rates} -> autopilot: {decision['action']} "
                f"({decision['reason']})"
            )

    async def _experiment_traffic(self, token: str, users: range) -> None:
        """Simulated trial users: 42% convert with the checklist vs 25% without."""
        converted: list[str] = []
        for index in users:
            self.clock.advance(seconds=1)  # real traffic is spread out (and rate limited)
            assignment = await self.call(
                "GET",
                "/v1/experiments/onboarding-checklist/assignment",
                token,
                params={"unit_id": f"trial_user_{index}"},
            )
            if index % 100 < (42 if assignment["variant"] == "checklist" else 25):
                converted.append(f"trial_user_{index}")
        for user in converted:
            await self.stack.growth.service.ingest(
                [ClientEvent(name="first_document_uploaded", id=f"conv-{user}")],
                source="client",
                tenant_id=None,
                user_id=user,
            )

    async def observability(self) -> None:
        headline("9. Observability - distributed traces and reliability")
        spans = self.spans.spans()
        run_spans = [s for s in spans if s.name.startswith("POST /v1/agents/*")]
        if run_spans and run_spans[0].context:
            trace_id = run_spans[0].context.trace_id
            in_trace = [s for s in spans if s.context and s.context.trace_id == trace_id]
            services = sorted({str(s.resource.attributes.get("service.name")) for s in in_trace})
            line(f"one agent question = {len(in_trace)} spans across {', '.join(services)}")
        queue = self.stack.infra.task_queue
        dead = 0
        for name in ("gateway", "knowledge", "agents", "billing", "growth"):
            dead += (await queue.stats(name)).dead
        line(f"{len(spans)} spans recorded; dead-lettered tasks: {dead}")
        print()


def main() -> None:
    import os

    os.environ["STRIPE_WEBHOOK_SECRET"] = STRIPE_SECRET
    print("\033[1mAI-native SaaS platform - simulated end-to-end run (offline model)\033[0m")
    started = time.perf_counter()
    asyncio.run(Demo().run())
    print(f"done in {time.perf_counter() - started:.1f}s")
