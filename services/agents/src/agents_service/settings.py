from __future__ import annotations

from dataclasses import dataclass

from platform_core.config import (
    ConfigurationError,
    CoreSettings,
    env_bool,
    env_float,
    env_int,
    env_opt,
    env_str,
)


@dataclass(frozen=True)
class AgentsSettings:
    core: CoreSettings
    llm_provider: str = "offline"  # "anthropic" | "offline"
    model: str | None = None  # Anthropic model id (AGENTS_MODEL); needed for "anthropic"
    effort: str = "high"  # low | medium | high | xhigh | max
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    critic: str = "heuristic"  # "heuristic" | "llm"
    critic_threshold: float = 0.6
    planner: bool = True
    max_steps: int = 6
    token_budget: int = 150_000
    estimated_run_credits: float = 15.0
    entitlement_fail_open: bool = True
    knowledge_url: str = "http://knowledge:8000"
    billing_url: str = "http://billing:8000"

    @classmethod
    def from_env(cls) -> AgentsSettings:
        api_key = env_opt("ANTHROPIC_API_KEY")
        settings = cls(
            core=CoreSettings.from_env("agents"),
            llm_provider=env_str("AGENTS_LLM_PROVIDER", "anthropic" if api_key else "offline"),
            model=env_opt("AGENTS_MODEL"),
            effort=env_str("AGENTS_EFFORT", "high"),
            anthropic_api_key=api_key,
            anthropic_base_url=env_opt("AGENTS_ANTHROPIC_BASE_URL"),
            critic=env_str("AGENTS_CRITIC", "heuristic"),
            critic_threshold=env_float("AGENTS_CRITIC_THRESHOLD", 0.6),
            planner=env_bool("AGENTS_PLANNER", True),
            max_steps=env_int("AGENTS_MAX_STEPS", 6),
            token_budget=env_int("AGENTS_TOKEN_BUDGET", 150_000),
            estimated_run_credits=env_float("AGENTS_ESTIMATED_RUN_CREDITS", 15.0),
            entitlement_fail_open=env_bool("AGENTS_ENTITLEMENT_FAIL_OPEN", True),
            knowledge_url=env_str("KNOWLEDGE_URL", "http://knowledge:8000"),
            billing_url=env_str("BILLING_URL", "http://billing:8000"),
        )
        if settings.llm_provider not in {"anthropic", "offline"}:
            raise ConfigurationError(f"unknown AGENTS_LLM_PROVIDER '{settings.llm_provider}'")
        if settings.critic not in {"heuristic", "llm"}:
            raise ConfigurationError(f"unknown AGENTS_CRITIC '{settings.critic}'")
        return settings
