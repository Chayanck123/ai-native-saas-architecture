from __future__ import annotations

from dataclasses import dataclass

from platform_core.config import CoreSettings, env_float, env_int, env_str


@dataclass(frozen=True)
class GrowthSettings:
    core: CoreSettings
    gross_margin: float = 0.75  # used for LTV; billing reports the measured inference margin
    pql_threshold: int = 60
    activation_event: str = "agent_run_succeeded"
    activation_window_days: int = 7

    @classmethod
    def from_env(cls) -> GrowthSettings:
        return cls(
            core=CoreSettings.from_env("growth"),
            gross_margin=env_float("GROWTH_GROSS_MARGIN", 0.75),
            pql_threshold=env_int("GROWTH_PQL_THRESHOLD", 60),
            activation_event=env_str("GROWTH_ACTIVATION_EVENT", "agent_run_succeeded"),
            activation_window_days=env_int("GROWTH_ACTIVATION_WINDOW_DAYS", 7),
        )
