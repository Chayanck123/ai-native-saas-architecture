"""Twelve-factor configuration: everything comes from environment variables.

Defaults are chosen so a developer can run the whole platform on a laptop with
no external services (SQLite + in-memory queues). Production refuses to start
with development secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any

DEV_INTERNAL_JWT_SECRET = "dev-internal-secret-change-me-0123456789abcdef"  # noqa: S105
DEV_JWT_SECRET = "dev-user-jwt-secret-change-me-0123456789abcdef"  # noqa: S105


def env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_opt(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoreSettings:
    """Settings every service shares."""

    service_name: str
    environment: str = "local"
    database_url: str = "sqlite+aiosqlite:///:memory:"
    redis_url: str | None = None
    redis_prefix: str = "ainative"
    internal_jwt_secret: str = DEV_INTERNAL_JWT_SECRET
    log_level: str = "INFO"
    log_json: bool = True
    otlp_endpoint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, service_name: str) -> CoreSettings:
        prefix = service_name.upper().replace("-", "_")
        settings = cls(
            service_name=service_name,
            environment=env_str("ENVIRONMENT", "local"),
            # A service-specific URL wins so that each service can own its own database.
            database_url=env_opt(f"{prefix}_DATABASE_URL")
            or env_str("DATABASE_URL", f"sqlite+aiosqlite:///./data/{service_name}.db"),
            redis_url=env_opt("REDIS_URL"),
            redis_prefix=env_str("REDIS_PREFIX", "ainative"),
            internal_jwt_secret=env_str("INTERNAL_JWT_SECRET", DEV_INTERNAL_JWT_SECRET),
            log_level=env_str("LOG_LEVEL", "INFO"),
            log_json=env_bool("LOG_JSON", True),
            otlp_endpoint=env_opt("OTEL_EXPORTER_OTLP_ENDPOINT"),
        )
        settings.validate()
        return settings

    @property
    def is_production(self) -> bool:
        return self.environment in {"production", "staging"}

    def with_overrides(self, **changes: Any) -> CoreSettings:
        return replace(self, **changes)

    def validate(self) -> None:
        if not self.is_production:
            return
        if self.internal_jwt_secret == DEV_INTERNAL_JWT_SECRET:
            raise ConfigurationError("INTERNAL_JWT_SECRET must be set outside local/test")
        if len(self.internal_jwt_secret) < 32:
            raise ConfigurationError("INTERNAL_JWT_SECRET must be at least 32 characters")
        if self.database_url.startswith("sqlite"):
            raise ConfigurationError("SQLite is for local development only; set DATABASE_URL")
        if not self.redis_url:
            raise ConfigurationError("REDIS_URL is required outside local/test")
