"""Plans, usage rating and invoice pricing (pure functions, no I/O).

Monetisation is *hybrid*: a predictable platform fee plus per-seat pricing
(recurring revenue that ARR is built on) plus usage-based credits (revenue that
grows with the value customers get - and that covers the variable LLM cost of
serving them).

Money is integer cents; credits are integer *millicredits* (1 credit = 1000) so
nothing is ever rounded twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from platform_core.catalog import Meters
from platform_core.errors import ValidationFailed

MILLI = 1000


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    monthly_base_cents: int
    included_seats: int
    seat_price_cents: int
    max_seats: int | None
    included_credits: int
    overage_cents_per_1k_credits: int
    hard_limit: bool  # free tier: block at 100% (upgrade moment); paid: bill overage
    requests_per_minute: int
    features: tuple[str, ...] = ()

    @property
    def included_millicredits(self) -> int:
        return self.included_credits * MILLI


PLANS: dict[str, Plan] = {
    "free": Plan(
        key="free",
        name="Free",
        monthly_base_cents=0,
        included_seats=3,
        seat_price_cents=0,
        max_seats=3,
        included_credits=2_000,
        overage_cents_per_1k_credits=0,
        hard_limit=True,
        requests_per_minute=60,
        features=("rag", "agents"),
    ),
    "pro": Plan(
        key="pro",
        name="Pro",
        monthly_base_cents=4_900,
        included_seats=3,
        seat_price_cents=1_500,
        max_seats=None,
        included_credits=50_000,
        overage_cents_per_1k_credits=100,
        hard_limit=False,
        requests_per_minute=600,
        features=("rag", "agents", "webhooks", "api_keys"),
    ),
    "enterprise": Plan(
        key="enterprise",
        name="Enterprise",
        monthly_base_cents=150_000,
        included_seats=25,
        seat_price_cents=1_000,
        max_seats=None,
        included_credits=1_000_000,
        overage_cents_per_1k_credits=60,
        hard_limit=False,
        requests_per_minute=3_000,
        features=("rag", "agents", "webhooks", "api_keys", "sso", "audit_log"),
    ),
}

# Millicredits charged per 1,000 units of each meter.
METER_RATES: dict[str, int] = {
    Meters.LLM_INPUT_TOKENS: 1_000,  # 1 credit per 1k input tokens
    Meters.LLM_OUTPUT_TOKENS: 5_000,  # 5 credits per 1k output tokens
    Meters.EMBEDDING_TOKENS: 100,  # 0.1 credit per 1k embedded tokens
    Meters.AGENT_RUNS: 2_000_000,  # 2 credits per agent run
    Meters.SEARCHES: 500_000,  # 0.5 credit per search
}


def get_plan(key: str) -> Plan:
    try:
        return PLANS[key]
    except KeyError as exc:
        raise ValidationFailed(f"unknown plan '{key}'", plans=sorted(PLANS)) from exc


def rate_usage(meter: str, quantity: int) -> int:
    """Millicredits for ``quantity`` units of ``meter`` (rounded up, never negative)."""
    if meter not in METER_RATES:
        raise ValidationFailed(f"unknown meter '{meter}'")
    if quantity < 0:
        raise ValidationFailed("usage quantity cannot be negative")
    return ceil(quantity * METER_RATES[meter] / 1000)


def recurring_cents(plan: Plan, seats: int) -> int:
    """The recurring (subscription) part of the monthly bill - the basis of ARR."""
    extra_seats = max(0, seats - plan.included_seats)
    return plan.monthly_base_cents + extra_seats * plan.seat_price_cents


def overage_cents(plan: Plan, used_millicredits: int, granted_millicredits: int = 0) -> int:
    allowance = plan.included_millicredits + granted_millicredits
    over = max(0, used_millicredits - allowance)
    if plan.hard_limit or over == 0:
        return 0
    return ceil(over * plan.overage_cents_per_1k_credits / (1000 * MILLI))


@dataclass(frozen=True)
class InvoiceLine:
    description: str
    quantity: int
    unit_amount_cents: int
    amount_cents: int


@dataclass(frozen=True)
class InvoicePreview:
    plan: str
    lines: list[InvoiceLine]
    total_cents: int


def price_invoice(
    plan: Plan, *, seats: int, used_millicredits: int, granted_millicredits: int = 0
) -> InvoicePreview:
    lines = [
        InvoiceLine(
            f"{plan.name} platform fee", 1, plan.monthly_base_cents, plan.monthly_base_cents
        )
    ]
    extra_seats = max(0, seats - plan.included_seats)
    if extra_seats:
        lines.append(
            InvoiceLine(
                "Additional seats",
                extra_seats,
                plan.seat_price_cents,
                extra_seats * plan.seat_price_cents,
            )
        )
    overage = overage_cents(plan, used_millicredits, granted_millicredits)
    if overage:
        allowance = plan.included_millicredits + granted_millicredits
        over_credits = ceil((used_millicredits - allowance) / MILLI)
        lines.append(
            InvoiceLine(
                "Usage overage (credits)", over_credits, plan.overage_cents_per_1k_credits, overage
            )
        )
    return InvoicePreview(
        plan=plan.key, lines=lines, total_cents=sum(line.amount_cents for line in lines)
    )
