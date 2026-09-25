from __future__ import annotations

from datetime import UTC, datetime

import httpx

from billing_service.app import BillingServiceApp
from billing_service.models import MrrSnapshot
from platform_core.catalog import Events, Meters
from platform_core.context import Principal, use_principal
from platform_core.infrastructure import Infrastructure
from platform_core.rbac import Role
from platform_core.testing import asgi_client, internal_headers, staff_principal, tenant_principal

from .conftest import deliver, published


def client_for(
    built: BillingServiceApp, tenant_id: str, role: Role = Role.OWNER
) -> httpx.AsyncClient:
    headers = internal_headers(
        built.runtime.internal_tokens, tenant_principal(tenant_id, role=role)
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built.app), base_url="http://test", headers=headers
    )


async def signup(built: BillingServiceApp, tenant_id: str) -> None:
    await deliver(built, Events.TENANT_CREATED, {"name": "Acme"}, tenant_id)


async def use(
    built: BillingServiceApp, tenant_id: str, meter: str, quantity: int, key: str
) -> None:
    await deliver(
        built,
        Events.USAGE_RECORDED,
        {"meter": meter, "quantity": quantity, "idempotency_key": key},
        tenant_id,
    )


async def test_signup_provisions_a_free_subscription(
    billing: BillingServiceApp, infra: Infrastructure, tenant_id: str
) -> None:
    await signup(billing, tenant_id)
    await signup(billing, tenant_id)  # redelivered event: still one subscription
    async with client_for(billing, tenant_id) as client:
        sub = (await client.get("/v1/billing/subscription")).json()
    assert sub["plan"]["key"] == "free"
    assert sub["recurring_cents"] == 0
    [change] = published(infra, Events.SUBSCRIPTION_CHANGED)
    assert change["reason"] == "signup" and change["previous_plan"] is None


async def test_usage_is_metered_exactly_once(billing: BillingServiceApp, tenant_id: str) -> None:
    await signup(billing, tenant_id)
    await use(billing, tenant_id, Meters.LLM_INPUT_TOKENS, 12_000, key="run_1:in")
    await use(billing, tenant_id, Meters.LLM_OUTPUT_TOKENS, 2_000, key="run_1:out")
    # Same idempotency key in a *different* event (e.g. a producer retry): billed once.
    await use(billing, tenant_id, Meters.LLM_OUTPUT_TOKENS, 2_000, key="run_1:out")
    async with client_for(billing, tenant_id, Role.ADMIN) as client:
        summary = (await client.get("/v1/billing/usage")).json()
    assert summary["meters"][Meters.LLM_OUTPUT_TOKENS] == {"quantity": 2_000, "credits": 10.0}
    assert summary["credits"]["used"] == 12.0 + 10.0
    assert summary["credits"]["remaining"] == 2_000 - 22.0


async def test_quota_thresholds_fire_once_and_free_tier_is_hard_limited(
    billing: BillingServiceApp, infra: Infrastructure, tenant_id: str
) -> None:
    await signup(billing, tenant_id)
    await use(billing, tenant_id, Meters.AGENT_RUNS, 850, key="burst-1")  # 1,700 credits = 85%
    await use(billing, tenant_id, Meters.AGENT_RUNS, 10, key="burst-2")
    thresholds = [e["threshold"] for e in published(infra, Events.QUOTA_THRESHOLD_REACHED)]
    assert thresholds == [80]

    async with client_for(billing, tenant_id) as client:
        ok = (await client.get("/v1/billing/entitlements", params={"credits": 100})).json()
        assert ok["allowed"] is True
        await use(billing, tenant_id, Meters.AGENT_RUNS, 200, key="burst-3")  # now 2,120
        blocked = (await client.get("/v1/billing/entitlements", params={"credits": 1})).json()
    assert blocked["allowed"] is False
    assert blocked["reason"] == "credit_limit_reached"
    assert blocked["upgrade_to"] == "pro"
    thresholds = [e["threshold"] for e in published(infra, Events.QUOTA_THRESHOLD_REACHED)]
    assert thresholds == [80, 100]
    assert published(infra, Events.QUOTA_THRESHOLD_REACHED)[-1]["suggested_plan"] == "pro"


async def test_upgrade_changes_entitlements_and_announces_mrr(
    billing: BillingServiceApp, infra: Infrastructure, tenant_id: str
) -> None:
    await signup(billing, tenant_id)
    await use(billing, tenant_id, Meters.AGENT_RUNS, 1_100, key="big")  # over the free limit
    async with client_for(billing, tenant_id, Role.ADMIN) as admin:
        denied = await admin.put("/v1/billing/subscription", json={"plan": "pro"})
        assert denied.status_code == 403  # only owners hold billing:manage
    async with client_for(billing, tenant_id) as owner:
        upgraded = await owner.put("/v1/billing/subscription", json={"plan": "pro"})
        assert upgraded.status_code == 200
        assert upgraded.json()["recurring_cents"] == 4_900
        entitlement = (await owner.get("/v1/billing/entitlements", params={"credits": 5})).json()
    assert entitlement["allowed"] is True
    change = published(infra, Events.SUBSCRIPTION_CHANGED)[-1]
    assert (change["previous_plan"], change["plan"]) == ("free", "pro")
    assert (change["previous_recurring_cents"], change["recurring_cents"]) == (0, 4_900)


async def test_seats_follow_membership_and_drive_seat_revenue(
    billing: BillingServiceApp, tenant_id: str
) -> None:
    await signup(billing, tenant_id)
    async with client_for(billing, tenant_id) as owner:
        await owner.put("/v1/billing/subscription", json={"plan": "pro"})
        for index in range(4):
            await deliver(billing, Events.MEMBER_ADDED, {"user_id": f"u{index}"}, tenant_id)
        sub = (await owner.get("/v1/billing/subscription")).json()
        assert sub["seats"] == 5
        assert sub["recurring_cents"] == 4_900 + 2 * 1_500
        seat_check = await owner.put("/v1/billing/subscription", json={"plan": "free"})
        assert seat_check.status_code == 422  # free allows 3 seats
        await use(billing, tenant_id, Meters.LLM_OUTPUT_TOKENS, 10_400_000, key="heavy")
        invoice = (await owner.get("/v1/billing/invoice/preview")).json()
    overage = invoice["lines"][-1]
    assert overage["description"] == "Usage overage (credits)"
    assert overage["quantity"] == 2_000  # 52,000 credits used vs 50,000 included
    assert invoice["total_cents"] == 4_900 + 3_000 + 200


async def test_payment_provider_updates(billing: BillingServiceApp, tenant_id: str) -> None:
    await signup(billing, tenant_id)
    update = {"provider": "stripe", "customer_id": "cus_1", "subscription_id": "sub_1"}
    await deliver(
        billing,
        Events.PROVIDER_SUBSCRIPTION_UPDATED,
        {**update, "status": "active", "plan": "enterprise"},
        tenant_id,
    )
    async with client_for(billing, tenant_id) as owner:
        sub = (await owner.get("/v1/billing/subscription")).json()
        assert (sub["plan"]["key"], sub["provider"]) == ("enterprise", "stripe")
        await deliver(
            billing,
            Events.PROVIDER_SUBSCRIPTION_UPDATED,
            {**update, "status": "past_due", "plan": "enterprise"},
            tenant_id,
        )
        assert (await owner.get("/v1/billing/subscription")).json()["status"] == "past_due"
        await deliver(
            billing,
            Events.PROVIDER_SUBSCRIPTION_UPDATED,
            {**update, "status": "canceled"},
            tenant_id,
        )
        sub = (await owner.get("/v1/billing/subscription")).json()
    assert (sub["plan"]["key"], sub["status"]) == ("free", "active")  # falls back to free


async def test_staff_revenue_report_and_credit_grants(
    billing: BillingServiceApp, tenant_id: str
) -> None:
    other = "ten_other_customer"
    await signup(billing, tenant_id)
    await signup(billing, other)
    async with client_for(billing, tenant_id) as owner:
        await owner.put("/v1/billing/subscription", json={"plan": "pro"})
    await use(billing, tenant_id, Meters.LLM_INPUT_TOKENS, 1_000_000, key="in")
    await use(billing, tenant_id, Meters.LLM_OUTPUT_TOKENS, 100_000, key="out")

    # History: last month the customer paid less (expansion) and one account churned.
    with use_principal(Principal.system("billing")):
        async with billing.runtime.db.session() as session:
            now = datetime(2025, 12, 31, tzinfo=UTC)
            for tid, cents in [(tenant_id, 3_000), ("ten_churned", 9_900)]:
                session.add(
                    MrrSnapshot(
                        id=f"snap_{tid}",
                        tenant_id=tid,
                        month="2025-12",
                        plan="pro",
                        seats=1,
                        recurring_cents=cents,
                        usage_cents=0,
                        updated_at=now,
                    )
                )

    staff = internal_headers(billing.runtime.internal_tokens, staff_principal())
    async with asgi_client(billing.app, headers=staff) as client:
        report = (await client.get("/v1/billing/revenue")).json()
        grant = {
            "tenant_id": other,
            "credits": 500,
            "reason": "onboarding bonus",
            "idempotency_key": "promo-1",
        }
        assert (await client.post("/v1/billing/credits/grants", json=grant)).json()[
            "granted"
        ] == 500
        assert (await client.post("/v1/billing/credits/grants", json=grant)).json()[
            "duplicate"
        ] is True
    assert report["month"] == "2026-01"
    assert report["mrr_cents"] == 4_900
    assert report["arr_cents"] == 12 * 4_900
    assert report["paying_accounts"] == 1
    latest = report["history"][-1]
    assert latest["expansion_cents"] == 1_900
    assert latest["churned_cents"] == 9_900
    assert report["inference_cost_cents"] == 500 + 250  # $5 input + $2.50 output
    assert 0 < report["gross_margin"] < 1

    async with client_for(billing, other) as owner:
        usage = (await owner.get("/v1/billing/usage")).json()
    assert usage["credits"]["granted"] == 500

    async with client_for(billing, tenant_id) as owner:
        forbidden = await owner.get("/v1/billing/revenue")
    assert forbidden.status_code == 403  # tenants never see platform revenue
