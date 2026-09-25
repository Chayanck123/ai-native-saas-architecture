from __future__ import annotations

from collections.abc import Sequence

from gateway_service.app import GatewayServiceApp, build_gateway
from gateway_service.settings import GatewaySettings
from platform_core.cli import main as run_cli
from platform_core.infrastructure import Infrastructure


def build() -> GatewayServiceApp:
    settings = GatewaySettings.from_env()
    return build_gateway(settings, Infrastructure.from_settings(settings.core))


def main(argv: Sequence[str] | None = None) -> None:
    run_cli("gateway", build, argv)


if __name__ == "__main__":
    main()
