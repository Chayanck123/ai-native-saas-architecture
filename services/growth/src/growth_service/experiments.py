"""Automated A/B testing: deterministic assignment and automatic decisions.

Assignment is a pure function of ``(experiment key, unit id)``: the same user
always sees the same variant, on every server, with no lookup table - and
different experiments are independent of each other.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from growth_service.stats import (
    probability_to_beat,
    sample_ratio_mismatch_p,
    two_proportion_z_test,
)


@dataclass(frozen=True)
class Variant:
    name: str
    weight: float


def bucket(experiment_key: str, unit_id: str) -> float:
    """A stable number in [0, 100) for this unit in this experiment."""
    digest = hashlib.sha256(f"{experiment_key}:{unit_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 1_000_000 / 10_000


def assign_variant(experiment_key: str, unit_id: str, variants: Sequence[Variant]) -> str:
    total = sum(v.weight for v in variants)
    point = bucket(experiment_key, unit_id) / 100 * total
    cumulative = 0.0
    for variant in variants:
        cumulative += variant.weight
        if point < cumulative:
            return variant.name
    return variants[-1].name


@dataclass(frozen=True)
class VariantResult:
    name: str
    exposures: int
    conversions: int
    rate: float
    lift: float | None
    p_value: float | None
    probability_to_beat_control: float | None


@dataclass(frozen=True)
class Decision:
    action: str  # continue | ship | keep_control | halt_srm
    winner: str | None
    reason: str


def evaluate(
    variants: Sequence[Variant],
    exposures: dict[str, int],
    conversions: dict[str, int],
    *,
    min_sample_per_variant: int,
    confidence: float = 0.95,
    srm_threshold: float = 0.001,
) -> tuple[list[VariantResult], float, Decision]:
    """Compute per-variant results and the automated decision.

    Rules (evaluated in order):

    1. **SRM guard** - if assignment counts deviate from the configured weights
       (p < 0.001), halt: the data is untrustworthy.
    2. Wait until every variant has ``min_sample_per_variant`` exposures (avoids
       "peeking" at tiny, noisy samples).
    3. **Ship** the best treatment if it beats control with Bayesian probability
       >= ``confidence`` *and* the frequentist test agrees (p < 1 - confidence).
    4. **Keep control** if no treatment has even a (1 - confidence) chance of winning.
    5. Otherwise keep collecting data.
    """
    control = variants[0].name
    n_c, c_c = exposures.get(control, 0), conversions.get(control, 0)
    results = []
    for variant in variants:
        n, c = exposures.get(variant.name, 0), conversions.get(variant.name, 0)
        rate = c / n if n else 0.0
        if variant.name == control:
            results.append(VariantResult(variant.name, n, c, round(rate, 5), None, None, None))
            continue
        control_rate = c_c / n_c if n_c else 0.0
        _, p_value = two_proportion_z_test(c_c, n_c, c, n)
        results.append(
            VariantResult(
                name=variant.name,
                exposures=n,
                conversions=c,
                rate=round(rate, 5),
                lift=round(rate / control_rate - 1, 4) if control_rate else None,
                p_value=round(p_value, 5),
                probability_to_beat_control=round(probability_to_beat(c_c, n_c, c, n), 4),
            )
        )

    srm_p = sample_ratio_mismatch_p(
        [exposures.get(v.name, 0) for v in variants], [v.weight for v in variants]
    )
    if sum(exposures.values()) >= 100 and srm_p < srm_threshold:
        return results, srm_p, Decision("halt_srm", None, f"sample ratio mismatch (p={srm_p:.2e})")
    if any(r.exposures < min_sample_per_variant for r in results):
        return results, srm_p, Decision("continue", None, "collecting minimum sample")
    treatments = [r for r in results if r.name != control]

    def win_probability(result: VariantResult) -> float:
        # Explicit None checks: a p-value or probability of exactly 0.0 is meaningful.
        chance = result.probability_to_beat_control
        return 0.0 if chance is None else chance

    best = max(treatments, key=win_probability)
    best_p = 1.0 if best.p_value is None else best.p_value
    if win_probability(best) >= confidence and best_p < 1 - confidence:
        return (
            results,
            srm_p,
            Decision(
                "ship",
                best.name,
                f"{best.name} beats control with "
                f"{best.probability_to_beat_control:.1%} probability",
            ),
        )
    if all(win_probability(r) <= 1 - confidence for r in treatments):
        return (
            results,
            srm_p,
            Decision("keep_control", control, "no variant is likely to beat control"),
        )
    return results, srm_p, Decision("continue", None, "not yet conclusive")
