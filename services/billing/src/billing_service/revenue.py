"""SaaS revenue metrics: MRR, ARR, MRR movements, NRR/GRR and AI gross margin.

Definitions used here (the conventional ones):

* **MRR** - monthly recurring revenue: subscription fees plus normalised usage.
* **ARR** - annual recurring revenue = 12 x MRR.
* **MRR movements** between two months, per account:
  new (first paid month), expansion (up), contraction (down, still paying),
  churn (dropped to zero), reactivation (paid again after churning).
* **NRR** - net revenue retention: what last period's customers pay now,
  expansion included. Above 100% means the business grows even with zero new
  customers - the hallmark of strong usage-based SaaS.
* **GRR** - gross revenue retention: like NRR but ignoring expansion (<= 100%).
* **Gross margin** - for an AI product the main variable cost is model
  inference, so it is tracked explicitly against revenue.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class MrrMovements:
    starting_mrr_cents: int
    new_cents: int
    expansion_cents: int
    contraction_cents: int
    churned_cents: int
    reactivation_cents: int
    ending_mrr_cents: int

    @property
    def net_new_cents(self) -> int:
        return (
            self.new_cents
            + self.expansion_cents
            + self.reactivation_cents
            - self.contraction_cents
            - self.churned_cents
        )


def mrr_movements(
    previous: Mapping[str, int],
    current: Mapping[str, int],
    *,
    previously_paying: frozenset[str] = frozenset(),
) -> MrrMovements:
    """Classify the change in MRR between two months, account by account.

    ``previously_paying`` holds accounts that paid at some point *before* the
    previous month; if such an account had zero MRR last month and pays now, it
    is a reactivation rather than a new customer.
    """
    new = expansion = contraction = churned = reactivation = 0
    for account in set(previous) | set(current):
        before = previous.get(account, 0)
        after = current.get(account, 0)
        if before == 0 and after > 0:
            if account in previously_paying:
                reactivation += after
            else:
                new += after
        elif before > 0 and after == 0:
            churned += before
        elif after > before:
            expansion += after - before
        elif after < before:
            contraction += before - after
    movements = MrrMovements(
        starting_mrr_cents=sum(previous.values()),
        new_cents=new,
        expansion_cents=expansion,
        contraction_cents=contraction,
        churned_cents=churned,
        reactivation_cents=reactivation,
        ending_mrr_cents=sum(current.values()),
    )
    assert movements.starting_mrr_cents + movements.net_new_cents == movements.ending_mrr_cents
    return movements


def net_revenue_retention(start: Mapping[str, int], end: Mapping[str, int]) -> float | None:
    base = {account: mrr for account, mrr in start.items() if mrr > 0}
    if not base:
        return None
    return sum(end.get(account, 0) for account in base) / sum(base.values())


def gross_revenue_retention(start: Mapping[str, int], end: Mapping[str, int]) -> float | None:
    base = {account: mrr for account, mrr in start.items() if mrr > 0}
    if not base:
        return None
    return sum(min(end.get(account, 0), mrr) for account, mrr in base.items()) / sum(base.values())


def arr_cents(mrr_cents: int) -> int:
    return 12 * mrr_cents


@dataclass(frozen=True)
class InferenceCostRates:
    """Provider list prices in US cents per million tokens (configurable)."""

    input_cents_per_mtok: int = 500  # $5 / MTok
    output_cents_per_mtok: int = 2_500  # $25 / MTok
    embedding_cents_per_mtok: int = 6  # $0.06 / MTok


def inference_cost_cents(
    input_tokens: int, output_tokens: int, embedding_tokens: int, rates: InferenceCostRates
) -> float:
    return (
        input_tokens * rates.input_cents_per_mtok
        + output_tokens * rates.output_cents_per_mtok
        + embedding_tokens * rates.embedding_cents_per_mtok
    ) / 1_000_000


def gross_margin(revenue_cents: float, cost_cents: float) -> float | None:
    if revenue_cents <= 0:
        return None
    return (revenue_cents - cost_cents) / revenue_cents
