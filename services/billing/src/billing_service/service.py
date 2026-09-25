"""Billing use cases: subscriptions, idempotent metering, entitlements and revenue."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from billing_service.models import (
    CreditLedgerEntry,
    MrrSnapshot,
    QuotaNotification,
    Subscription,
    UsageRecord,
)
from billing_service.plans import (
    MILLI,
    PLANS,
    InvoicePreview,
    Plan,
    get_plan,
    overage_cents,
    price_invoice,
    rate_usage,
    recurring_cents,
)
from billing_service.revenue import (
    InferenceCostRates,
    arr_cents,
    gross_margin,
    gross_revenue_retention,
    inference_cost_cents,
    mrr_movements,
    net_revenue_retention,
)
from platform_core.catalog import Events, Meters
from platform_core.context import Principal, require_tenant_id, use_principal
from platform_core.errors import NotFound, ValidationFailed
from platform_core.ids import new_id
from platform_core.service import ServiceRuntime, UnitOfWork

THRESHOLDS = (80, 100)


def period_start(moment: datetime) -> datetime:
    return datetime(moment.year, moment.month, 1, tzinfo=UTC)


def month_key(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


@dataclass(frozen=True)
class Entitlement:
    allowed: bool
    plan: str
    reason: str | None
    included_credits: float
    granted_credits: float
    used_credits: float
    remaining_credits: float
    hard_limit: bool
    seats: int
    max_seats: int | None
    upgrade_to: str | None


class BillingService:
    def __init__(self, runtime: ServiceRuntime, cost_rates: InferenceCostRates) -> None:
        self.runtime = runtime
        self.cost_rates = cost_rates

    # ------------------------------------------------------------ helpers
    async def _subscription(self, session: AsyncSession, tenant_id: str) -> Subscription:
        sub = await session.get(Subscription, tenant_id)
        if sub is None:
            raise NotFound(f"no subscription for tenant '{tenant_id}'")
        return sub

    async def _period_totals(
        self, session: AsyncSession, tenant_id: str, period: datetime
    ) -> tuple[int, int]:
        """Return ``(used_millicredits, granted_millicredits)`` for a billing period."""
        rows = await session.execute(
            select(CreditLedgerEntry.entry_type, func.sum(CreditLedgerEntry.millicredits))
            .where(
                CreditLedgerEntry.tenant_id == tenant_id, CreditLedgerEntry.period_start == period
            )
            .group_by(CreditLedgerEntry.entry_type)
        )
        totals = {entry_type: int(total or 0) for entry_type, total in rows}
        return -totals.get("consume", 0), totals.get("grant", 0) + totals.get("adjust", 0)

    async def _announce_plan_change(
        self,
        uow: UnitOfWork,
        sub: Subscription,
        previous_plan: str | None,
        previous_recurring: int,
        reason: str,
    ) -> None:
        await uow.publish(
            self.runtime.event(
                Events.SUBSCRIPTION_CHANGED,
                {
                    "plan": sub.plan,
                    "previous_plan": previous_plan,
                    "status": sub.status,
                    "seats": sub.seats,
                    "recurring_cents": recurring_cents(get_plan(sub.plan), sub.seats),
                    "previous_recurring_cents": previous_recurring,
                    "reason": reason,
                },
                tenant_id=sub.tenant_id,
                subject=sub.tenant_id,
            )
        )

    # ------------------------------------------------------- provisioning
    async def provision_tenant(self, *, plan: str = "free") -> dict[str, Any]:
        tenant_id = require_tenant_id()
        now = self.runtime.clock.now()
        async with self.runtime.unit_of_work() as uow:
            if await uow.session.get(Subscription, tenant_id) is not None:
                return {"provisioned": False}
            sub = Subscription(
                tenant_id=tenant_id,
                plan=get_plan(plan).key,
                status="active",
                seats=1,
                provider="manual",
                started_at=now,
                updated_at=now,
            )
            uow.session.add(sub)
            await self._announce_plan_change(uow, sub, None, 0, "signup")
        return {"provisioned": True, "plan": plan}

    async def adjust_seats(self, delta: int) -> dict[str, Any]:
        tenant_id = require_tenant_id()
        async with self.runtime.unit_of_work() as uow:
            sub = await self._subscription(uow.session, tenant_id)
            previous = recurring_cents(get_plan(sub.plan), sub.seats)
            sub.seats = max(1, sub.seats + delta)
            sub.updated_at = self.runtime.clock.now()
            if recurring_cents(get_plan(sub.plan), sub.seats) != previous:
                await self._announce_plan_change(uow, sub, sub.plan, previous, "seats")
        return {"seats": sub.seats}

    # ------------------------------------------------------------- usage
    async def record_usage(
        self,
        *,
        meter: str,
        quantity: int,
        idempotency_key: str,
        occurred_at: datetime | None = None,
        resource_id: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        tenant_id = require_tenant_id()
        millicredits = rate_usage(meter, quantity)
        occurred = occurred_at or self.runtime.clock.now()
        period = period_start(occurred)
        try:
            async with self.runtime.unit_of_work() as uow:
                session = uow.session
                duplicate = await session.execute(
                    select(UsageRecord.id).where(
                        UsageRecord.tenant_id == tenant_id,
                        UsageRecord.idempotency_key == idempotency_key,
                    )
                )
                if duplicate.first() is not None:
                    return {"duplicate": True}
                record_id = new_id("use")
                session.add(
                    UsageRecord(
                        id=record_id,
                        tenant_id=tenant_id,
                        meter=meter,
                        quantity=quantity,
                        millicredits=millicredits,
                        idempotency_key=idempotency_key,
                        period_start=period,
                        occurred_at=occurred,
                        resource_id=resource_id,
                        source=source,
                    )
                )
                session.add(
                    CreditLedgerEntry(
                        id=new_id("led"),
                        tenant_id=tenant_id,
                        entry_type="consume",
                        millicredits=-millicredits,
                        period_start=period,
                        reference=record_id,
                        idempotency_key=f"usage:{idempotency_key}",
                        created_at=occurred,
                    )
                )
                await session.flush()  # surface a concurrent duplicate as IntegrityError
                await self._check_thresholds(uow, tenant_id, period)
        except IntegrityError:
            return {"duplicate": True}
        return {"duplicate": False, "millicredits": millicredits}

    async def _check_thresholds(self, uow: UnitOfWork, tenant_id: str, period: datetime) -> None:
        session = uow.session
        sub = await session.get(Subscription, tenant_id)
        if sub is None:
            return
        plan = get_plan(sub.plan)
        used, granted = await self._period_totals(session, tenant_id, period)
        allowance = plan.included_millicredits + granted
        already = set(
            (
                await session.execute(
                    select(QuotaNotification.threshold).where(
                        QuotaNotification.tenant_id == tenant_id,
                        QuotaNotification.period_start == period,
                    )
                )
            ).scalars()
        )
        for threshold in THRESHOLDS:
            if threshold in already or used * 100 < allowance * threshold:
                continue
            session.add(
                QuotaNotification(
                    id=new_id("qn"),
                    tenant_id=tenant_id,
                    period_start=period,
                    threshold=threshold,
                    created_at=self.runtime.clock.now(),
                )
            )
            await uow.publish(
                self.runtime.event(
                    Events.QUOTA_THRESHOLD_REACHED,
                    {
                        "threshold": threshold,
                        "plan": plan.key,
                        "hard_limit": plan.hard_limit,
                        "used_credits": used / MILLI,
                        "included_credits": allowance / MILLI,
                        "suggested_plan": "pro" if plan.key == "free" else None,
                    },
                    tenant_id=tenant_id,
                    subject=tenant_id,
                )
            )

    async def grant_credits(
        self, credits: int, *, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        if credits <= 0:
            raise ValidationFailed("credits must be positive")
        tenant_id = require_tenant_id()
        now = self.runtime.clock.now()
        try:
            async with self.runtime.db.session() as session:
                session.add(
                    CreditLedgerEntry(
                        id=new_id("led"),
                        tenant_id=tenant_id,
                        entry_type="grant",
                        millicredits=credits * MILLI,
                        period_start=period_start(now),
                        reference=reason,
                        idempotency_key=f"grant:{idempotency_key}",
                        created_at=now,
                    )
                )
        except IntegrityError:
            return {"granted": 0, "duplicate": True}
        return {"granted": credits, "duplicate": False}

    # ------------------------------------------------------- entitlements
    async def entitlement(self, *, credits: float = 0.0, seats: int | None = None) -> Entitlement:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            sub = await self._subscription(session, tenant_id)
            used, granted = await self._period_totals(
                session, tenant_id, period_start(self.runtime.clock.now())
            )
        plan = get_plan(sub.plan)
        allowance = plan.included_millicredits + granted
        remaining = allowance - used
        allowed, reason = True, None
        if sub.status == "canceled":
            allowed, reason = False, "subscription_canceled"
        elif plan.hard_limit and used + credits * MILLI > allowance:
            allowed, reason = False, "credit_limit_reached"
        elif seats is not None and plan.max_seats is not None and seats > plan.max_seats:
            allowed, reason = False, "seat_limit_reached"
        return Entitlement(
            allowed=allowed,
            plan=plan.key,
            reason=reason,
            included_credits=plan.included_credits,
            granted_credits=granted / MILLI,
            used_credits=used / MILLI,
            remaining_credits=remaining / MILLI,
            hard_limit=plan.hard_limit,
            seats=sub.seats,
            max_seats=plan.max_seats,
            upgrade_to=None if allowed else _next_plan(plan),
        )

    # ------------------------------------------------------ subscriptions
    async def get_subscription(self) -> tuple[Subscription, Plan]:
        async with self.runtime.db.session() as session:
            sub = await self._subscription(session, require_tenant_id())
        return sub, get_plan(sub.plan)

    async def change_plan(
        self,
        plan_key: str,
        *,
        reason: str = "self_serve",
        status: str = "active",
        provider: str | None = None,
        provider_customer_id: str | None = None,
        provider_subscription_id: str | None = None,
    ) -> Subscription:
        plan = get_plan(plan_key)
        tenant_id = require_tenant_id()
        async with self.runtime.unit_of_work() as uow:
            sub = await self._subscription(uow.session, tenant_id)
            if plan.max_seats is not None and sub.seats > plan.max_seats:
                raise ValidationFailed(
                    f"the {plan.name} plan allows {plan.max_seats} seats; remove members first"
                )
            previous_plan = sub.plan
            previous_recurring = recurring_cents(get_plan(previous_plan), sub.seats)
            changed = (previous_plan, sub.status) != (plan.key, status)
            sub.plan = plan.key
            sub.status = status
            sub.updated_at = self.runtime.clock.now()
            if provider:
                sub.provider = provider
                sub.provider_customer_id = provider_customer_id or sub.provider_customer_id
                sub.provider_subscription_id = (
                    provider_subscription_id or sub.provider_subscription_id
                )
            if changed:
                await self._announce_plan_change(
                    uow, sub, previous_plan, previous_recurring, reason
                )
        return sub

    async def apply_provider_update(self, data: dict[str, Any]) -> dict[str, Any]:
        """Apply a subscription update relayed from the payment provider (Stripe)."""
        status = str(data.get("status", "active"))
        provider = str(data.get("provider", "stripe"))
        customer_id = data.get("customer_id")
        subscription_id = data.get("subscription_id")
        plan = str(data.get("plan") or "pro")
        if status in {"canceled", "unpaid", "incomplete_expired"}:
            # Product-led growth: a cancelled customer falls back to Free, not to zero.
            plan, reason, status = "free", "canceled", "active"
        elif status == "past_due":
            reason = "payment_failed"
        else:
            reason, status = "provider", "active"
        sub = await self.change_plan(
            plan,
            reason=reason,
            status=status,
            provider=provider,
            provider_customer_id=customer_id,
            provider_subscription_id=subscription_id,
        )
        return {"plan": sub.plan, "status": sub.status}

    # ------------------------------------------------------ tenant reports
    async def usage_summary(self) -> dict[str, Any]:
        tenant_id = require_tenant_id()
        period = period_start(self.runtime.clock.now())
        async with self.runtime.db.session() as session:
            sub = await self._subscription(session, tenant_id)
            used, granted = await self._period_totals(session, tenant_id, period)
            rows = await session.execute(
                select(
                    UsageRecord.meter,
                    func.sum(UsageRecord.quantity),
                    func.sum(UsageRecord.millicredits),
                )
                .where(UsageRecord.tenant_id == tenant_id, UsageRecord.period_start == period)
                .group_by(UsageRecord.meter)
            )
            meters = {
                meter: {"quantity": int(quantity or 0), "credits": int(mc or 0) / MILLI}
                for meter, quantity, mc in rows
            }
        plan = get_plan(sub.plan)
        allowance = plan.included_millicredits + granted
        return {
            "period_start": period.isoformat(),
            "plan": plan.key,
            "meters": meters,
            "credits": {
                "included": plan.included_credits,
                "granted": granted / MILLI,
                "used": used / MILLI,
                "remaining": (allowance - used) / MILLI,
                "percent_used": round(100 * used / allowance, 2) if allowance else 100.0,
            },
        }

    async def invoice_preview(self) -> InvoicePreview:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            sub = await self._subscription(session, tenant_id)
            used, granted = await self._period_totals(
                session, tenant_id, period_start(self.runtime.clock.now())
            )
        return price_invoice(
            get_plan(sub.plan),
            seats=sub.seats,
            used_millicredits=used,
            granted_millicredits=granted,
        )

    # ------------------------------------------------ platform (cross-tenant)
    async def snapshot_revenue(self) -> dict[str, Any]:
        """Upsert this month's recurring + usage revenue (and inference volume) per tenant."""
        now = self.runtime.clock.now()
        period, month = period_start(now), month_key(now)
        written = 0
        with use_principal(Principal.system(self.runtime.name)):
            async with self.runtime.db.session() as session:
                subs = (await session.execute(select(Subscription))).scalars().all()
                tokens = await self._token_totals(session, period)
                existing = {
                    snap.tenant_id: snap
                    for snap in (
                        await session.execute(select(MrrSnapshot).where(MrrSnapshot.month == month))
                    ).scalars()
                }
                for sub in subs:
                    plan = get_plan(sub.plan)
                    used, granted = await self._period_totals(session, sub.tenant_id, period)
                    active = sub.status != "canceled"
                    per_meter = tokens.get(sub.tenant_id, {})
                    snap = existing.get(sub.tenant_id) or MrrSnapshot(
                        id=new_id("snap"), tenant_id=sub.tenant_id, month=month
                    )
                    snap.plan = plan.key
                    snap.seats = sub.seats
                    snap.recurring_cents = recurring_cents(plan, sub.seats) if active else 0
                    snap.usage_cents = overage_cents(plan, used, granted) if active else 0
                    snap.input_tokens = per_meter.get(Meters.LLM_INPUT_TOKENS, 0)
                    snap.output_tokens = per_meter.get(Meters.LLM_OUTPUT_TOKENS, 0)
                    snap.embedding_tokens = per_meter.get(Meters.EMBEDDING_TOKENS, 0)
                    snap.updated_at = now
                    session.add(snap)
                    written += 1
        return {"month": month, "tenants": written}

    async def _token_totals(
        self, session: AsyncSession, period: datetime
    ) -> dict[str, dict[str, int]]:
        rows = await session.execute(
            select(UsageRecord.tenant_id, UsageRecord.meter, func.sum(UsageRecord.quantity))
            .where(UsageRecord.period_start == period)
            .group_by(UsageRecord.tenant_id, UsageRecord.meter)
        )
        totals: dict[str, dict[str, int]] = defaultdict(dict)
        for tenant_id, meter, quantity in rows:
            totals[tenant_id][meter] = int(quantity or 0)
        return totals

    async def revenue_report(self, *, months: int = 6) -> dict[str, Any]:
        await self.snapshot_revenue()
        with use_principal(Principal.system(self.runtime.name)):
            async with self.runtime.db.session() as session:
                snapshots = (await session.execute(select(MrrSnapshot))).scalars().all()
        by_month: dict[str, dict[str, int]] = defaultdict(dict)
        for snap in snapshots:
            by_month[snap.month][snap.tenant_id] = snap.recurring_cents + snap.usage_cents
        ordered = sorted(by_month)[-months:]
        history = []
        paying_before: set[str] = set()
        all_months = sorted(by_month)
        for index, month in enumerate(all_months):
            previous = by_month[all_months[index - 1]] if index else {}
            current = by_month[month]
            if month in ordered:
                moves = mrr_movements(previous, current, previously_paying=frozenset(paying_before))
                history.append(
                    {
                        "month": month,
                        "mrr_cents": moves.ending_mrr_cents,
                        "arr_cents": arr_cents(moves.ending_mrr_cents),
                        "new_cents": moves.new_cents,
                        "expansion_cents": moves.expansion_cents,
                        "contraction_cents": moves.contraction_cents,
                        "churned_cents": moves.churned_cents,
                        "reactivation_cents": moves.reactivation_cents,
                        "net_new_cents": moves.net_new_cents,
                    }
                )
            paying_before |= {t for t, v in previous.items() if v > 0}

        latest = ordered[-1] if ordered else month_key(self.runtime.clock.now())
        current_mrr = by_month.get(latest, {})
        latest_snaps = [s for s in snapshots if s.month == latest]
        revenue = sum(s.recurring_cents + s.usage_cents for s in latest_snaps)
        cost = inference_cost_cents(
            sum(s.input_tokens for s in latest_snaps),
            sum(s.output_tokens for s in latest_snaps),
            sum(s.embedding_tokens for s in latest_snaps),
            self.cost_rates,
        )
        paying = [t for t, v in current_mrr.items() if v > 0]
        mrr = sum(current_mrr.values())
        by_plan: dict[str, int] = defaultdict(int)
        for snap in latest_snaps:
            by_plan[snap.plan] += snap.recurring_cents + snap.usage_cents
        first = by_month.get(ordered[0], {}) if ordered else {}
        return {
            "month": latest,
            "mrr_cents": mrr,
            "arr_cents": arr_cents(mrr),
            "paying_accounts": len(paying),
            "arpa_cents": round(mrr / len(paying)) if paying else 0,
            "mrr_by_plan_cents": dict(by_plan),
            "net_revenue_retention": net_revenue_retention(first, current_mrr),
            "gross_revenue_retention": gross_revenue_retention(first, current_mrr),
            "inference_cost_cents": round(cost, 2),
            "gross_margin": gross_margin(revenue, cost),
            "history": history,
        }


def _next_plan(plan: Plan) -> str | None:
    order = list(PLANS)
    index = order.index(plan.key)
    return order[index + 1] if index + 1 < len(order) else None
