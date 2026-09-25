"""Statistics for experimentation, implemented with the standard library only.

* **Two-proportion z-test** - is the difference in conversion rate real?
* **Bayesian probability to beat control** - Beta-Binomial posteriors compared by
  Monte Carlo; easier for humans to act on than a p-value ("B is better than A
  with 97% probability").
* **Sample ratio mismatch (SRM)** - a chi-square goodness-of-fit test on assignment
  counts. If a 50/50 test shows 5,000 vs 5,600 users, the *assignment* is broken
  and the results must not be trusted.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_proportion_z_test(
    conversions_a: int, total_a: int, conversions_b: int, total_b: int
) -> tuple[float, float]:
    """Return ``(z, two_sided_p_value)`` for H0: rate_a == rate_b."""
    if min(total_a, total_b) == 0:
        return 0.0, 1.0
    pooled = (conversions_a + conversions_b) / (total_a + total_b)
    variance = pooled * (1 - pooled) * (1 / total_a + 1 / total_b)
    if variance <= 0:
        return 0.0, 1.0
    z = (conversions_b / total_b - conversions_a / total_a) / math.sqrt(variance)
    return z, 2.0 * (1.0 - normal_cdf(abs(z)))


def _gamma_series(a: float, x: float) -> float:
    total = term = 1.0 / a
    n = a
    for _ in range(1000):
        n += 1
        term *= x / n
        total += term
        if abs(term) < abs(total) * 1e-15:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_continued_fraction(a: float, x: float) -> float:
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def regularized_gamma_q(a: float, x: float) -> float:
    """Upper regularised incomplete gamma Q(a, x) (Numerical Recipes 6.2)."""
    if x <= 0:
        return 1.0
    if x < a + 1:
        return 1.0 - _gamma_series(a, x)
    return _gamma_continued_fraction(a, x)


def chi_square_sf(statistic: float, degrees_of_freedom: int) -> float:
    """P(X >= statistic) for a chi-square distribution."""
    return regularized_gamma_q(degrees_of_freedom / 2.0, statistic / 2.0)


def sample_ratio_mismatch_p(observed: Sequence[int], weights: Sequence[float]) -> float:
    total = sum(observed)
    if total == 0 or len(observed) < 2:
        return 1.0
    weight_sum = sum(weights)
    statistic = 0.0
    for count, weight in zip(observed, weights, strict=True):
        expected = total * weight / weight_sum
        statistic += (count - expected) ** 2 / expected
    return chi_square_sf(statistic, len(observed) - 1)


def probability_to_beat(
    conversions_control: int,
    total_control: int,
    conversions_variant: int,
    total_variant: int,
    *,
    draws: int = 20_000,
    seed: int = 7,
) -> float:
    """P(rate_variant > rate_control) under uniform Beta(1, 1) priors."""
    rng = random.Random(seed)
    alpha_c, beta_c = 1 + conversions_control, 1 + total_control - conversions_control
    alpha_v, beta_v = 1 + conversions_variant, 1 + total_variant - conversions_variant
    wins = sum(
        rng.betavariate(alpha_v, beta_v) > rng.betavariate(alpha_c, beta_c) for _ in range(draws)
    )
    return wins / draws
