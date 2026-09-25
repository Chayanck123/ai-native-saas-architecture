from __future__ import annotations

from dataclasses import dataclass, field

from billing_service.revenue import InferenceCostRates
from platform_core.config import CoreSettings, env_int


@dataclass(frozen=True)
class BillingSettings:
    core: CoreSettings
    cost_rates: InferenceCostRates = field(default_factory=InferenceCostRates)

    @classmethod
    def from_env(cls) -> BillingSettings:
        return cls(
            core=CoreSettings.from_env("billing"),
            cost_rates=InferenceCostRates(
                input_cents_per_mtok=env_int("BILLING_COST_INPUT_CENTS_PER_MTOK", 500),
                output_cents_per_mtok=env_int("BILLING_COST_OUTPUT_CENTS_PER_MTOK", 2_500),
                embedding_cents_per_mtok=env_int("BILLING_COST_EMBEDDING_CENTS_PER_MTOK", 6),
            ),
        )
