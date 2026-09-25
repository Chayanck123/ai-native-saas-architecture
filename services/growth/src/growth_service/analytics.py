"""Growth analytics as pure functions: funnels, cohorts, attribution, unit economics.

Keeping the maths free of I/O makes it easy to test and to move: the same
functions can run over rows from PostgreSQL today or a ClickHouse/warehouse
extract tomorrow.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


# --------------------------------------------------------------------------- #
# Funnels
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FunnelStep:
    name: str
    count: int
    conversion_from_previous: float | None
    conversion_from_start: float | None
    median_hours_from_previous: float | None


def funnel(
    steps: Sequence[str],
    events: Mapping[str, Sequence[tuple[str, datetime]]],
    *,
    window: timedelta,
) -> list[FunnelStep]:
    """Ordered funnel: a unit counts for step *n* only if it completed steps 1..n in
    order, with every step inside ``window`` of the first one."""
    reached = [0] * len(steps)
    gaps: list[list[float]] = [[] for _ in steps]
    for unit_events in events.values():
        timeline = sorted(unit_events, key=lambda item: item[1])
        start = next((t for name, t in timeline if name == steps[0]), None)
        if start is None:
            continue
        reached[0] += 1
        previous = start
        for index, step in enumerate(steps[1:], start=1):
            hit = next(
                (t for name, t in timeline if name == step and previous <= t <= start + window),
                None,
            )
            if hit is None:
                break
            reached[index] += 1
            gaps[index].append((hit - previous).total_seconds() / 3600)
            previous = hit
    result = []
    for index, name in enumerate(steps):
        prev_count = reached[index - 1] if index else None
        result.append(
            FunnelStep(
                name=name,
                count=reached[index],
                conversion_from_previous=(reached[index] / prev_count) if prev_count else None,
                conversion_from_start=(reached[index] / reached[0]) if reached[0] else None,
                median_hours_from_previous=(
                    round(statistics.median(gaps[index]), 2) if gaps[index] else None
                ),
            )
        )
    return result


# --------------------------------------------------------------------------- #
# Cohort retention
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CohortRow:
    cohort_start: datetime
    size: int
    retention: list[float | None]  # None = period not finished yet


def period_floor(moment: datetime, period: str) -> datetime:
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day
    if period == "week":  # ISO weeks start on Monday
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    raise ValueError(f"unknown period '{period}'")


def _add_periods(start: datetime, period: str, count: int) -> datetime:
    if period == "day":
        return start + timedelta(days=count)
    if period == "week":
        return start + timedelta(weeks=count)
    month = start.month - 1 + count
    return start.replace(year=start.year + month // 12, month=month % 12 + 1)


def retention_cohorts(
    signups: Mapping[str, datetime],
    activity: Mapping[str, Sequence[datetime]],
    *,
    period: str,
    periods: int,
    now: datetime,
) -> list[CohortRow]:
    """Classic cohort triangle: of the units that signed up in period P, what share was
    active in P+0, P+1, ... P+n? Unfinished periods are ``None``, not 0%."""
    cohorts: dict[datetime, list[str]] = defaultdict(list)
    for unit, signed_up in signups.items():
        cohorts[period_floor(signed_up, period)].append(unit)
    rows = []
    for cohort_start in sorted(cohorts):
        members = cohorts[cohort_start]
        retention: list[float | None] = []
        for offset in range(periods):
            window_start = _add_periods(cohort_start, period, offset)
            if window_start > now:
                retention.append(None)
                continue
            window_end = _add_periods(cohort_start, period, offset + 1)
            active = sum(
                1
                for unit in members
                if any(window_start <= t < window_end for t in activity.get(unit, ()))
            )
            retention.append(round(active / len(members), 4))
        rows.append(CohortRow(cohort_start, len(members), retention))
    return rows


# --------------------------------------------------------------------------- #
# Multi-touch attribution
# --------------------------------------------------------------------------- #
class AttributionModel(StrEnum):
    FIRST_TOUCH = "first_touch"
    LAST_TOUCH = "last_touch"
    LINEAR = "linear"
    TIME_DECAY = "time_decay"
    POSITION_BASED = "position_based"  # U-shaped: 40 / 20 / 40
    W_SHAPED = "w_shaped"  # 30 first / 30 lead creation / 30 last / 10 the rest


@dataclass(frozen=True)
class Touch:
    channel: str
    at: datetime
    is_lead_creation: bool = False  # the touch that turned a visitor into a signup


def attribute(
    touches: Sequence[Touch],
    model: AttributionModel,
    *,
    converted_at: datetime,
    value: float = 1.0,
    half_life: timedelta = timedelta(days=7),
) -> dict[str, float]:
    """Split one conversion's ``value`` across the channels that touched it."""
    journey = sorted((t for t in touches if t.at <= converted_at), key=lambda t: t.at)
    if not journey:
        return {"direct": value}
    weights = [0.0] * len(journey)
    n = len(journey)
    if model is AttributionModel.FIRST_TOUCH:
        weights[0] = 1.0
    elif model is AttributionModel.LAST_TOUCH:
        weights[-1] = 1.0
    elif model is AttributionModel.LINEAR:
        weights = [1.0 / n] * n
    elif model is AttributionModel.TIME_DECAY:
        weights = [0.5 ** ((converted_at - t.at) / half_life) for t in journey]
    elif model is AttributionModel.POSITION_BASED:
        if n == 1:
            weights = [1.0]
        elif n == 2:
            weights = [0.5, 0.5]
        else:
            weights = [0.4] + [0.2 / (n - 2)] * (n - 2) + [0.4]
    elif model is AttributionModel.W_SHAPED:
        lead = next((i for i, t in enumerate(journey) if t.is_lead_creation), None)
        key = sorted({0, n - 1} | ({lead} if lead is not None else set()))
        others = [i for i in range(n) if i not in key]
        for i in key:
            weights[i] = 0.9 / len(key) if others else 1.0 / len(key)
        for i in others:
            weights[i] = 0.1 / len(others)
    total = sum(weights)
    credit: dict[str, float] = defaultdict(float)
    for touch, weight in zip(journey, weights, strict=True):
        if weight > 0:
            credit[touch.channel] += value * weight / total
    return dict(credit)


# --------------------------------------------------------------------------- #
# Unit economics
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UnitEconomics:
    arpa_cents: float
    gross_margin: float
    monthly_churn: float
    ltv_cents: float
    cac_cents: float | None
    ltv_to_cac: float | None
    payback_months: float | None


def unit_economics(
    *,
    arpa_cents: float,
    gross_margin: float,
    monthly_churn: float,
    spend_cents: float,
    new_customers: float,
    churn_floor: float = 0.01,
) -> UnitEconomics:
    """LTV = ARPA x gross margin / monthly churn;  CAC = spend / new customers.

    A healthy SaaS business targets LTV:CAC >= 3 and CAC payback < 12 months.
    Churn is floored so a young business with no churn yet does not report an
    infinite LTV.
    """
    churn = max(monthly_churn, churn_floor)
    monthly_margin = arpa_cents * gross_margin
    ltv = monthly_margin / churn
    cac = spend_cents / new_customers if new_customers > 0 else None
    return UnitEconomics(
        arpa_cents=round(arpa_cents, 2),
        gross_margin=gross_margin,
        monthly_churn=round(churn, 4),
        ltv_cents=round(ltv, 2),
        cac_cents=round(cac, 2) if cac is not None else None,
        ltv_to_cac=round(ltv / cac, 2) if cac else None,
        payback_months=round(cac / monthly_margin, 2) if cac and monthly_margin else None,
    )


def monthly_churn_rate(churned: int, customers_at_start: int, months: int) -> float:
    """Convert churn over ``months`` into an equivalent compounded monthly rate."""
    if customers_at_start <= 0 or months <= 0:
        return 0.0
    survived = max(0.0, 1 - churned / customers_at_start)
    return float(1 - survived ** (1 / months))
