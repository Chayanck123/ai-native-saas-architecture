"""Billing HTTP API (``/v1/billing/*`` through the gateway)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from billing_service.plans import PLANS, Plan, recurring_cents
from billing_service.service import BillingService
from platform_core.context import Principal, use_tenant
from platform_core.rbac import Permission
from platform_core.service import require, require_authenticated

router = APIRouter(prefix="/v1/billing", tags=["billing"])


def _service(request: Request) -> BillingService:
    service = request.app.state.billing
    assert isinstance(service, BillingService)
    return service


class PlanOut(BaseModel):
    key: str
    name: str
    monthly_base_cents: int
    included_seats: int
    seat_price_cents: int
    max_seats: int | None
    included_credits: int
    overage_cents_per_1k_credits: int
    hard_limit: bool
    requests_per_minute: int
    features: list[str]

    @classmethod
    def of(cls, plan: Plan) -> PlanOut:
        return cls(**{**asdict(plan), "features": list(plan.features)})


class SubscriptionOut(BaseModel):
    tenant_id: str
    plan: PlanOut
    status: str
    seats: int
    recurring_cents: int
    provider: str
    started_at: datetime
    updated_at: datetime


class ChangePlanIn(BaseModel):
    plan: str = Field(pattern="^(free|pro|enterprise)$")


class GrantIn(BaseModel):
    tenant_id: str
    credits: int = Field(gt=0, le=10_000_000)
    reason: str = Field(min_length=3, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=100)


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(_: Principal = require_authenticated()) -> list[PlanOut]:
    return [PlanOut.of(plan) for plan in PLANS.values()]


async def _subscription_out(service: BillingService) -> SubscriptionOut:
    sub, plan = await service.get_subscription()
    return SubscriptionOut(
        tenant_id=sub.tenant_id,
        plan=PlanOut.of(plan),
        status=sub.status,
        seats=sub.seats,
        recurring_cents=recurring_cents(plan, sub.seats),
        provider=sub.provider,
        started_at=sub.started_at,
        updated_at=sub.updated_at,
    )


@router.get("/subscription", response_model=SubscriptionOut)
async def get_subscription(
    request: Request, _: Principal = require(Permission.BILLING_READ)
) -> SubscriptionOut:
    return await _subscription_out(_service(request))


@router.put("/subscription", response_model=SubscriptionOut)
async def change_subscription(
    body: ChangePlanIn, request: Request, _: Principal = require(Permission.BILLING_MANAGE)
) -> SubscriptionOut:
    """Self-serve plan change. (In production an upgrade goes through Stripe Checkout and
    lands here via the provider webhook; direct changes are kept for downgrades and demos.)"""
    service = _service(request)
    await service.change_plan(body.plan)
    return await _subscription_out(service)


@router.get("/usage")
async def usage(
    request: Request, _: Principal = require(Permission.BILLING_READ)
) -> dict[str, Any]:
    return await _service(request).usage_summary()


@router.get("/invoice/preview")
async def invoice_preview(
    request: Request, _: Principal = require(Permission.BILLING_READ)
) -> dict[str, Any]:
    return asdict(await _service(request).invoice_preview())


@router.get("/entitlements")
async def entitlements(
    request: Request,
    credits: float = Query(default=0.0, ge=0),
    seats: int | None = Query(default=None, ge=1),
    _: Principal = require(Permission.WORKSPACE_READ),
) -> dict[str, Any]:
    """Can this tenant spend ``credits`` more / have ``seats`` seats? Used by other
    services before expensive work - the free tier's hard limit is enforced here."""
    return asdict(await _service(request).entitlement(credits=credits, seats=seats))


@router.post("/credits/grants")
async def grant_credits(
    body: GrantIn, request: Request, _: Principal = require(Permission.PLATFORM_ADMIN, tenant=False)
) -> dict[str, Any]:
    """Staff: grant promotional credits (support gestures, growth experiments)."""
    with use_tenant(body.tenant_id):
        return await _service(request).grant_credits(
            body.credits, reason=body.reason, idempotency_key=body.idempotency_key
        )


@router.get("/revenue")
async def revenue(
    request: Request,
    months: int = Query(default=6, ge=1, le=36),
    _: Principal = require(Permission.ANALYTICS_READ, tenant=False),
) -> dict[str, Any]:
    """Staff: MRR, ARR, MRR movements, NRR/GRR and inference gross margin."""
    return await _service(request).revenue_report(months=months)
