from __future__ import annotations

from collections.abc import Sequence

from growth_service.app import GrowthServiceApp, build_growth
from growth_service.settings import GrowthSettings
from platform_core.cli import main as run_cli
from platform_core.infrastructure import Infrastructure


def build() -> GrowthServiceApp:
    settings = GrowthSettings.from_env()
    return build_growth(settings, Infrastructure.from_settings(settings.core))


def main(argv: Sequence[str] | None = None) -> None:
    run_cli("growth", build, argv)


if __name__ == "__main__":
    main()
