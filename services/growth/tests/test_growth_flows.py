from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from growth_service.app import GrowthServiceApp, build_growth
from growth_service.service import ClientEvent, classify_channel
from growth_service.settings import GrowthSettings
from platform_core.catalog import Events
from platform_core.clock import ManualClock
from platform_core.config import CoreSettings
from platform_core.events import InMemoryEventBus
from platform_core.infrastructure import Infrastructure
from platform_core.rbac import Role
from platform_core.testing import asgi_client, internal_headers, staff_principal, tenant_principal


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
async def growth(clock: ManualClock) -> AsyncIterator[GrowthServiceApp]:
    infra = Infrastructure.in_memory(clock)
    settings = GrowthSettings(
        core=CoreSettings(service_name="growth", database_url="sqlite+aiosqlite:///:memory:")
    )
    built = build_growth(settings, infra)
    await built.runtime.start()
    await built.worker.setup()
    yield built
    await built.runtime.stop()


async def deliver(
    growth: GrowthServiceApp, event_type: str, data: dict[str, Any], tenant_id: str | None = None
) -> None:
    await growth.runtime.publish(growth.runtime.event(event_type, data, tenant_id=tenant_id))
    await growth.worker.run_until_idle()


async def page_views(growth: GrowthServiceApp, anonymous_id: str, *channels: str) -> None:
    await deliver(
        growth,
        Events.TELEMETRY_BATCH_RECEIVED,
        {
            "events": [
                {
                    "name": "page_viewed",
                    "anonymous_id": anonymous_id,
                    "id": f"{anonymous_id}-{i}",
                    "properties": {"utm_source": channel, "path": "/pricing"},
                }
                for i, channel in enumerate(channels)
            ]
        },
    )


async def signup(
    growth: GrowthServiceApp,
    tenant: str,
    user: str,
    anonymous_id: str | None = None,
    utm: str | None = None,
) -> None:
    await deliver(growth, Events.TENANT_CREATED, {"name": tenant}, tenant)
    await deliver(
        growth,
        Events.USER_SIGNED_UP,
        {
            "user_id": user,
            "anonymous_id": anonymous_id,
            "attribution": {"utm_source": utm} if utm else {},
        },
        tenant,
    )


async def upgrade(
    growth: GrowthServiceApp, tenant: str, cents: int = 4_900, previous: int = 0
) -> None:
    await deliver(
        growth,
        Events.SUBSCRIPTION_CHANGED,
        {
            "plan": "pro" if cents else "free",
            "recurring_cents": cents,
            "previous_recurring_cents": previous,
        },
        tenant,
    )


def staff(growth: GrowthServiceApp) -> dict[str, str]:
    return internal_headers(growth.runtime.internal_tokens, staff_principal())


async def test_journey_projection_activation_and_funnel(
    growth: GrowthServiceApp, clock: ManualClock
) -> None:
    await page_views(growth, "anon_1", "google_ads", "blog")
    clock.advance(hours=1)
    await signup(growth, "ten_a", "usr_a", anonymous_id="anon_1")
    clock.advance(hours=2)
    await deliver(growth, Events.DOCUMENT_INDEXED, {"document_id": "d1"}, "ten_a")
    clock.advance(hours=1)
    run_done = growth.runtime.event(
        Events.AGENT_RUN_COMPLETED,
        {"run_id": "r1", "status": "succeeded", "created_by": "usr_a"},
        tenant_id="ten_a",
    )
    await growth.runtime.publish(run_done)
    await growth.runtime.publish(run_done)  # redelivery is harmless
    await growth.worker.run_until_idle()
    clock.advance(days=2)
    await upgrade(growth, "ten_a")
    await signup(growth, "ten_b", "usr_b", utm="linkedin")  # never activates

    async with asgi_client(growth.app, headers=staff(growth)) as client:
        overview = (await client.get("/v1/analytics/overview")).json()
        funnel = (
            await client.get(
                "/v1/analytics/funnel",
                params={
                    "steps": "page_viewed,signed_up,document_indexed,agent_run_succeeded,"
                    "converted_to_paid"
                },
            )
        ).json()
    assert overview["accounts"] == 2
    assert overview["paying_accounts"] == 1
    assert overview["mrr_cents"] == 4_900
    assert overview["activation_rate"] == 0.5
    assert overview["median_hours_to_value"] == 3.0
    assert overview["acquisition_channels"] == {"google_ads": 1, "linkedin": 1}
    # The anonymous page view was stitched to the workspace it became.
    assert [s["count"] for s in funnel["steps"]] == [1, 1, 1, 1, 1]


async def test_multi_touch_attribution_and_unit_economics(
    growth: GrowthServiceApp, clock: ManualClock
) -> None:
    await page_views(growth, "anon_1", "google_ads")
    await page_views(growth, "anon_2", "blog")
    clock.advance(days=1)
    await signup(growth, "ten_1", "usr_1", anonymous_id="anon_1", utm="webinar")
    await signup(growth, "ten_2", "usr_2", anonymous_id="anon_2", utm="blog")
    clock.advance(days=3)
    await upgrade(growth, "ten_1", 10_000)
    await upgrade(growth, "ten_2", 4_000)

    async with asgi_client(growth.app, headers=staff(growth)) as client:
        for channel, cents in [("google_ads", 1_000_000), ("blog", 200_000)]:
            spend = {"channel": channel, "month": "2026-01", "amount_cents": cents}
            assert (
                await client.put("/v1/analytics/marketing-spend", json=spend)
            ).status_code == 204
        first = (
            await client.get("/v1/analytics/attribution", params={"model": "first_touch"})
        ).json()
        linear = (await client.get("/v1/analytics/attribution", params={"model": "linear"})).json()
        economics = (
            await client.get("/v1/analytics/unit-economics", params={"model": "first_touch"})
        ).json()

    by_channel = {c["channel"]: c for c in first["channels"]}
    assert by_channel["google_ads"]["conversions"] == 1.0
    assert by_channel["google_ads"]["mrr_cents"] == 10_000
    linear_channels = {c["channel"]: c["conversions"] for c in linear["channels"]}
    assert linear_channels == {"google_ads": 0.5, "webinar": 0.5, "blog": 1.0}

    blended = economics["blended"]
    assert blended["arpa_cents"] == 7_000
    assert blended["cac_cents"] == 600_000  # $12,000 spend / 2 customers
    channels = {c["channel"]: c for c in economics["channels"]}
    assert channels["blog"]["cac_cents"] == 200_000
    assert channels["blog"]["ltv_to_cac"] == pytest.approx(7_000 * 0.75 / 0.01 / 200_000, abs=0.01)


async def test_weekly_retention_cohorts(growth: GrowthServiceApp, clock: ManualClock) -> None:
    await signup(growth, "ten_1", "usr_1")
    await signup(growth, "ten_2", "usr_2")
    clock.advance(weeks=1)
    await deliver(growth, Events.SEARCH_PERFORMED, {"user_id": "usr_1"}, "ten_1")
    clock.advance(weeks=1)
    await signup(growth, "ten_3", "usr_3")
    await deliver(growth, Events.USER_LOGGED_IN, {"user_id": "usr_1"}, "ten_1")

    async with asgi_client(growth.app, headers=staff(growth)) as client:
        report = (
            await client.get("/v1/analytics/cohorts", params={"period": "week", "periods": 3})
        ).json()
    first, second = report["cohorts"]
    assert first["size"] == 2
    assert first["retention"] == [0.0, 0.5, 0.5]
    assert second["size"] == 1


async def test_automated_ab_test_ships_the_winner(
    growth: GrowthServiceApp, clock: ManualClock
) -> None:
    experiment = {
        "key": "onboarding-checklist",
        "name": "Onboarding checklist",
        "variants": [{"name": "control", "weight": 50}, {"name": "checklist", "weight": 50}],
        "primary_metric": "first_document_uploaded",
        "min_sample_per_variant": 150,
    }
    async with asgi_client(growth.app, headers=staff(growth)) as admin:
        assert (await admin.post("/v1/experiments", json=experiment)).status_code == 201
        assert (await admin.post("/v1/experiments", json=experiment)).status_code == 409
        await admin.post("/v1/experiments/onboarding-checklist/start")

    headers = internal_headers(
        growth.runtime.internal_tokens, tenant_principal("ten_x", role=Role.MEMBER)
    )
    assignments: dict[str, str] = {}
    async with asgi_client(growth.app, headers=headers) as client:
        for i in range(400):
            response = await client.get(
                "/v1/experiments/onboarding-checklist/assignment", params={"unit_id": f"usr_{i}"}
            )
            assignments[f"usr_{i}"] = response.json()["variant"]
        again = await client.get(
            "/v1/experiments/onboarding-checklist/assignment", params={"unit_id": "usr_0"}
        )
        assert again.json()["variant"] == assignments["usr_0"]  # sticky

    clock.advance(minutes=5)
    # Deterministic outcome: 45% of checklist users convert vs 20% of control users.
    converted = [
        user
        for user, variant in assignments.items()
        if int(user.removeprefix("usr_")) % 100 < (45 if variant == "checklist" else 20)
    ]
    for user in converted:
        await growth.service.ingest(
            [ClientEvent(name="first_document_uploaded", id=f"c-{user}")],
            source="client",
            tenant_id="ten_x",
            user_id=user,
        )

    async with asgi_client(growth.app, headers=staff(growth)) as admin:
        results = (await admin.get("/v1/experiments/onboarding-checklist/results")).json()
        decisions = (await admin.post("/v1/analytics/jobs/experiment_autopilot")).json()
        final = (await admin.get("/v1/experiments")).json()[0]
    rates = {v["name"]: v["rate"] for v in results["variants"]}
    assert rates["checklist"] > rates["control"]
    assert results["sample_ratio_mismatch_p"] > 0.001
    assert decisions["decisions"][0]["action"] == "ship"
    assert final["status"] == "completed" and final["winner"] == "checklist"

    async with asgi_client(growth.app, headers=headers) as client:
        rollout = await client.get(
            "/v1/experiments/onboarding-checklist/assignment", params={"unit_id": "brand_new_user"}
        )
    assert rollout.json()["variant"] == "checklist"  # winner rolled out to everyone
    assert isinstance(growth.runtime.infra.event_bus, InMemoryEventBus)
    assert any(
        e.type == Events.EXPERIMENT_CONCLUDED for e in growth.runtime.infra.event_bus.published
    )


async def test_product_qualified_leads_are_flagged_once(
    growth: GrowthServiceApp, clock: ManualClock
) -> None:
    await signup(growth, "ten_hot", "usr_1")
    for i in range(3):
        await deliver(growth, Events.SEARCH_PERFORMED, {"user_id": f"usr_{i}"}, "ten_hot")
    for i in range(6):
        await deliver(
            growth,
            Events.AGENT_RUN_COMPLETED,
            {"run_id": f"r{i}", "status": "succeeded", "created_by": "usr_1"},
            "ten_hot",
        )
    await deliver(
        growth, Events.QUOTA_THRESHOLD_REACHED, {"threshold": 80, "plan": "free"}, "ten_hot"
    )
    await deliver(
        growth, Events.QUOTA_THRESHOLD_REACHED, {"threshold": 100, "plan": "free"}, "ten_hot"
    )
    bus = growth.runtime.infra.event_bus
    assert isinstance(bus, InMemoryEventBus)
    pqls = [e for e in bus.published if e.type == Events.PQL_IDENTIFIED]
    assert len(pqls) == 1
    assert pqls[0].tenantid == "ten_hot"
    assert pqls[0].data["score"] >= 60
    async with asgi_client(growth.app, headers=staff(growth)) as client:
        listed = (await client.get("/v1/analytics/pqls")).json()
    assert listed[0]["tenant_id"] == "ten_hot"


async def test_telemetry_ingest_and_access_control(growth: GrowthServiceApp) -> None:
    member = internal_headers(
        growth.runtime.internal_tokens, tenant_principal("ten_1", role=Role.MEMBER)
    )
    batch = {
        "events": [
            {"id": "e1", "name": "dashboard_viewed"},
            {"id": "e1", "name": "dashboard_viewed"},
            {"id": "e2", "name": "export_clicked", "properties": {"format": "csv"}},
        ]
    }
    async with asgi_client(growth.app, headers=member) as client:
        accepted = await client.post("/v1/telemetry/events", json=batch)
        replay = await client.post("/v1/telemetry/events", json=batch)
        invalid = await client.post(
            "/v1/telemetry/events", json={"events": [{"name": "Bad Name!"}]}
        )
        forbidden = await client.get("/v1/analytics/overview")
    assert accepted.json() == {"accepted": 2, "duplicates": 1}
    assert replay.json() == {"accepted": 0, "duplicates": 3}
    assert invalid.status_code == 422
    assert forbidden.status_code == 403  # customers never see platform analytics

    async with asgi_client(growth.app, headers=staff(growth)) as client:
        live = (await client.get("/v1/analytics/live")).json()
    assert live["totals"] == {"dashboard_viewed": 1, "export_clicked": 1}


def test_channel_classification() -> None:
    assert classify_channel({"utm_source": "Google Ads"}) == "google_ads"
    assert classify_channel({"referrer": "https://www.google.com/search?q=x"}) == "organic_search"
    assert classify_channel({"referrer": "https://www.linkedin.com/feed"}) == "social"
    assert classify_channel({"referrer": "https://news.ycombinator.com"}) == "referral"
    assert classify_channel({}) is None
