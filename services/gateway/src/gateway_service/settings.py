from __future__ import annotations

from dataclasses import dataclass, field

from platform_core.config import (
    DEV_JWT_SECRET,
    ConfigurationError,
    CoreSettings,
    env_bool,
    env_int,
    env_str,
)

DEFAULT_UPSTREAMS = {
    "knowledge": "http://knowledge:8000",
    "agents": "http://agents:8000",
    "billing": "http://billing:8000",
    "growth": "http://growth:8000",
}

# Public path prefix -> internal service that owns it.
ROUTES: dict[str, str] = {
    "/v1/documents": "knowledge",
    "/v1/search": "knowledge",
    "/v1/agents": "agents",
    "/v1/billing": "billing",
    "/v1/telemetry": "growth",
    "/v1/analytics": "growth",
    "/v1/experiments": "growth",
}


@dataclass(frozen=True)
class GatewaySettings:
    core: CoreSettings
    jwt_secret: str = DEV_JWT_SECRET
    access_token_ttl_seconds: int = 3600
    invite_ttl_seconds: int = 7 * 24 * 3600
    staff_emails: frozenset[str] = frozenset()
    upstreams: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_UPSTREAMS))
    upstream_timeout_seconds: float = 30.0
    # Requests per minute by plan (mirrors the billing plan catalogue).
    plan_rate_limits: dict[str, int] = field(
        default_factory=lambda: {"free": 60, "pro": 600, "enterprise": 3000}
    )
    anonymous_rate_limit: int = 30  # per client IP, for signup/login
    webhook_allow_private_urls: bool = True
    webhook_timeout_seconds: float = 10.0
    webhook_disable_after_failures: int = 10
    api_key_cache_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> GatewaySettings:
        core = CoreSettings.from_env("gateway")
        settings = cls(
            core=core,
            jwt_secret=env_str("JWT_SECRET", DEV_JWT_SECRET),
            access_token_ttl_seconds=env_int("ACCESS_TOKEN_TTL_SECONDS", 3600),
            staff_emails=frozenset(
                e.strip().lower() for e in env_str("STAFF_EMAILS", "").split(",") if e.strip()
            ),
            upstreams={
                name: env_str(f"{name.upper()}_URL", url) for name, url in DEFAULT_UPSTREAMS.items()
            },
            anonymous_rate_limit=env_int("ANONYMOUS_RATE_LIMIT", 30),
            webhook_allow_private_urls=env_bool(
                "WEBHOOK_ALLOW_PRIVATE_URLS", not core.is_production
            ),
        )
        if core.is_production and settings.jwt_secret == DEV_JWT_SECRET:
            raise ConfigurationError("JWT_SECRET must be set outside local/test")
        return settings
