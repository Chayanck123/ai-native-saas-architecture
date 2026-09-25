from __future__ import annotations

from collections.abc import Sequence

from billing_service.app import BillingServiceApp, build_billing
from billing_service.settings import BillingSettings
from platform_core.cli import main as run_cli
from platform_core.infrastructure import Infrastructure


def build() -> BillingServiceApp:
    settings = BillingSettings.from_env()
    return build_billing(settings, Infrastructure.from_settings(settings.core))


def main(argv: Sequence[str] | None = None) -> None:
    run_cli("billing", build, argv)


if __name__ == "__main__":
    main()
