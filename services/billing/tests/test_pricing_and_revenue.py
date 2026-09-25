from __future__ import annotations

import pytest

from billing_service.plans import (
    PLANS,
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
from platform_core.catalog import Meters
from platform_core.errors import ValidationFailed


def test_usage_is_rated_into_millicredits_rounding_up() -> None:
    assert rate_usage(Meters.LLM_INPUT_TOKENS, 1_000) == 1_000  # 1 credit
    assert rate_usage(Meters.LLM_OUTPUT_TOKENS, 1_000) == 5_000
    assert rate_usage(Meters.AGENT_RUNS, 3) == 6_000
    assert rate_usage(Meters.EMBEDDING_TOKENS, 1) == 1  # never free, never fractional
    with pytest.raises(ValidationFailed):
        rate_usage("gpu_hours", 1)
    with pytest.raises(ValidationFailed):
        rate_usage(Meters.SEARCHES, -1)


def test_recurring_revenue_combines_platform_fee_and_seats() -> None:
    pro = PLANS["pro"]
    assert recurring_cents(pro, 1) == 4_900
    assert recurring_cents(pro, 5) == 4_900 + 2 * 1_500
    assert recurring_cents(PLANS["free"], 3) == 0


def test_overage_only_for_paid_plans() -> None:
    used = 60_000 * 1000  # 60k credits
    assert overage_cents(PLANS["free"], used) == 0  # free has a hard limit instead
    assert overage_cents(PLANS["pro"], used) == 10 * 100  # 10k credits over at $1 / 1k
    assert overage_cents(PLANS["pro"], used, granted_millicredits=10_000 * 1000) == 0


def test_invoice_preview_lines_add_up() -> None:
    invoice = price_invoice(PLANS["pro"], seats=4, used_millicredits=52_500_000)
    assert [line.description for line in invoice.lines] == [
        "Pro platform fee",
        "Additional seats",
        "Usage overage (credits)",
    ]
    assert invoice.total_cents == 4_900 + 1_500 + 250
    assert invoice.total_cents == sum(line.amount_cents for line in invoice.lines)


def test_mrr_movements_classify_every_change() -> None:
    previous = {"a": 4_900, "b": 7_900, "c": 4_900, "d": 150_000}
    current = {"a": 4_900, "b": 10_900, "c": 0, "d": 120_000, "e": 4_900, "f": 4_900}
    moves = mrr_movements(previous, current, previously_paying=frozenset({"f"}))
    assert moves.new_cents == 4_900  # e
    assert moves.reactivation_cents == 4_900  # f came back
    assert moves.expansion_cents == 3_000  # b
    assert moves.contraction_cents == 30_000  # d
    assert moves.churned_cents == 4_900  # c
    assert moves.starting_mrr_cents + moves.net_new_cents == moves.ending_mrr_cents


def test_retention_ratios() -> None:
    start = {"a": 100, "b": 100}
    end = {"a": 150, "b": 0, "new": 500}
    assert net_revenue_retention(start, end) == pytest.approx(0.75)
    assert gross_revenue_retention(start, end) == pytest.approx(0.5)
    assert net_revenue_retention({}, end) is None
    assert net_revenue_retention({"a": 100}, {"a": 130}) == pytest.approx(1.3)  # expansion
    assert arr_cents(10_000) == 120_000


def test_inference_cost_and_gross_margin() -> None:
    rates = InferenceCostRates(input_cents_per_mtok=500, output_cents_per_mtok=2_500)
    cost = inference_cost_cents(2_000_000, 400_000, 0, rates)
    assert cost == pytest.approx(1_000 + 1_000)  # $10 + $10
    assert gross_margin(10_000, cost) == pytest.approx(0.8)
    assert gross_margin(0, cost) is None
