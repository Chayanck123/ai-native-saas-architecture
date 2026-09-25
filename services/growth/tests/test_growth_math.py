from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from growth_service.analytics import (
    AttributionModel,
    Touch,
    attribute,
    funnel,
    monthly_churn_rate,
    period_floor,
    retention_cohorts,
    unit_economics,
)
from growth_service.experiments import Variant, assign_variant, bucket, evaluate
from growth_service.stats import (
    chi_square_sf,
    normal_cdf,
    probability_to_beat,
    sample_ratio_mismatch_p,
    two_proportion_z_test,
)

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)  # a Monday


def test_statistics_match_reference_values() -> None:
    assert normal_cdf(1.959964) == pytest.approx(0.975, abs=1e-6)
    z, p = two_proportion_z_test(100, 1000, 130, 1000)
    assert z == pytest.approx(2.1027, abs=1e-3)
    assert p == pytest.approx(0.0355, abs=1e-3)
    assert chi_square_sf(3.841459, 1) == pytest.approx(0.05, abs=1e-5)
    assert chi_square_sf(7.814728, 3) == pytest.approx(0.05, abs=1e-5)
    assert chi_square_sf(18.307038, 10) == pytest.approx(0.05, abs=1e-5)
    assert sample_ratio_mismatch_p([5000, 5200], [1, 1]) == pytest.approx(0.0477, abs=1e-3)
    assert sample_ratio_mismatch_p([5000, 5600], [1, 1]) < 1e-7


def test_bayesian_probability_to_beat_control() -> None:
    assert probability_to_beat(100, 1000, 130, 1000) == pytest.approx(0.982, abs=0.01)
    assert probability_to_beat(100, 1000, 100, 1000) == pytest.approx(0.5, abs=0.02)


def test_assignment_is_deterministic_and_respects_weights() -> None:
    variants = [Variant("control", 50), Variant("treatment", 50)]
    assert assign_variant("onboarding-v2", "usr_1", variants) == assign_variant(
        "onboarding-v2", "usr_1", variants
    )
    counts = Counter(assign_variant("onboarding-v2", f"usr_{i}", variants) for i in range(20_000))
    assert counts["control"] / 20_000 == pytest.approx(0.5, abs=0.02)
    skewed = [Variant("control", 90), Variant("treatment", 10)]
    counts = Counter(assign_variant("pricing-page", f"u{i}", skewed) for i in range(20_000))
    assert counts["treatment"] / 20_000 == pytest.approx(0.1, abs=0.01)
    # Independent experiments bucket the same user independently.
    assert bucket("a", "usr_1") != bucket("b", "usr_1")


def test_automated_decisions() -> None:
    variants = [Variant("control", 50), Variant("new_onboarding", 50)]
    _, _, waiting = evaluate(
        variants,
        {"control": 50, "new_onboarding": 52},
        {"control": 5, "new_onboarding": 9},
        min_sample_per_variant=100,
    )
    assert waiting.action == "continue"
    results, _, ship = evaluate(
        variants,
        {"control": 1000, "new_onboarding": 1000},
        {"control": 100, "new_onboarding": 140},
        min_sample_per_variant=100,
    )
    assert ship.action == "ship" and ship.winner == "new_onboarding"
    assert results[1].lift == pytest.approx(0.4)
    _, _, keep = evaluate(
        variants,
        {"control": 1000, "new_onboarding": 1000},
        {"control": 140, "new_onboarding": 95},
        min_sample_per_variant=100,
    )
    assert keep.action == "keep_control" and keep.winner == "control"
    _, srm_p, halt = evaluate(
        variants,
        {"control": 5000, "new_onboarding": 5600},
        {"control": 500, "new_onboarding": 560},
        min_sample_per_variant=100,
    )
    assert halt.action == "halt_srm" and srm_p < 0.001


def test_ordered_funnel_with_conversion_window() -> None:
    h = timedelta(hours=1)
    events = {
        "t1": [
            ("signed_up", T0),
            ("document_indexed", T0 + h),
            ("agent_run_succeeded", T0 + 3 * h),
            ("converted_to_paid", T0 + 48 * h),
        ],
        "t2": [("signed_up", T0), ("agent_run_succeeded", T0 + 2 * h)],  # skipped a step
        "t3": [
            ("signed_up", T0),
            ("document_indexed", T0 + 2 * h),
            ("agent_run_succeeded", T0 + 24 * 40 * h),
        ],  # outside the 30-day window
        "t4": [("document_indexed", T0)],  # never signed up (e.g. bad data)
    }
    steps = funnel(
        ["signed_up", "document_indexed", "agent_run_succeeded", "converted_to_paid"],
        events,
        window=timedelta(days=30),
    )
    assert [s.count for s in steps] == [3, 2, 1, 1]
    assert steps[1].conversion_from_previous == pytest.approx(2 / 3)
    assert steps[3].conversion_from_start == pytest.approx(1 / 3)
    assert steps[2].median_hours_from_previous == 2.0


def test_weekly_retention_cohorts() -> None:
    week = timedelta(weeks=1)
    signups = {"a": T0, "b": T0 + timedelta(days=2), "c": T0 + week}
    activity = {
        "a": [T0, T0 + week, T0 + 2 * week],
        "b": [T0 + timedelta(days=3)],
        "c": [T0 + week + timedelta(days=1)],
    }
    rows = retention_cohorts(
        signups, activity, period="week", periods=4, now=T0 + 2 * week + timedelta(days=1)
    )
    assert [r.size for r in rows] == [2, 1]
    assert rows[0].retention == [1.0, 0.5, 0.5, None]
    assert rows[1].retention == [1.0, 0.0, None, None]
    assert period_floor(T0 + timedelta(days=4), "week") == T0.replace(hour=0)
    assert period_floor(T0, "month") == datetime(2026, 3, 1, tzinfo=UTC)


def test_attribution_models_split_credit() -> None:
    touches = [
        Touch("google_ads", T0),
        Touch("blog", T0 + timedelta(days=7), is_lead_creation=True),
        Touch("newsletter", T0 + timedelta(days=13)),
        Touch("webinar", T0 + timedelta(days=14)),
    ]
    converted = T0 + timedelta(days=14)
    first = attribute(touches, AttributionModel.FIRST_TOUCH, converted_at=converted, value=100)
    assert first == {"google_ads": 100}
    last = attribute(touches, AttributionModel.LAST_TOUCH, converted_at=converted, value=100)
    assert last == {"webinar": 100}
    linear = attribute(touches, AttributionModel.LINEAR, converted_at=converted, value=100)
    assert linear["blog"] == pytest.approx(25)
    u_shape = attribute(touches, AttributionModel.POSITION_BASED, converted_at=converted, value=100)
    assert (u_shape["google_ads"], u_shape["blog"], u_shape["webinar"]) == pytest.approx(
        (40, 10, 40)
    )
    w_shape = attribute(touches, AttributionModel.W_SHAPED, converted_at=converted, value=100)
    assert (
        w_shape["google_ads"],
        w_shape["blog"],
        w_shape["webinar"],
        w_shape["newsletter"],
    ) == pytest.approx((30, 30, 30, 10))
    decay = attribute(touches, AttributionModel.TIME_DECAY, converted_at=converted, value=100)
    assert decay["webinar"] > decay["newsletter"] > decay["blog"] > decay["google_ads"]
    assert sum(decay.values()) == pytest.approx(100)
    assert attribute([], AttributionModel.LINEAR, converted_at=converted) == {"direct": 1.0}


def test_unit_economics() -> None:
    economics = unit_economics(
        arpa_cents=10_000,
        gross_margin=0.8,
        monthly_churn=0.02,
        spend_cents=5_000_000,
        new_customers=40,
    )
    assert economics.ltv_cents == pytest.approx(400_000)
    assert economics.cac_cents == pytest.approx(125_000)
    assert economics.ltv_to_cac == pytest.approx(3.2)
    assert economics.payback_months == pytest.approx(15.62, abs=0.01)
    assert (
        unit_economics(
            arpa_cents=100, gross_margin=0.8, monthly_churn=0, spend_cents=0, new_customers=0
        ).monthly_churn
        == 0.01
    )  # floored
    assert monthly_churn_rate(churned=19, customers_at_start=100, months=3) == pytest.approx(
        1 - 0.81 ** (1 / 3)
    )


def test_decisive_result_with_p_value_rounding_to_zero_ships() -> None:
    variants = [Variant("control", 50), Variant("b", 50)]
    results, _, decision = evaluate(
        variants,
        {"control": 5000, "b": 5000},
        {"control": 500, "b": 1500},
        min_sample_per_variant=100,
    )
    assert results[1].p_value == 0.0  # regression: 0.0 must not be treated as "missing"
    assert decision.action == "ship"
