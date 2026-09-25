from __future__ import annotations

from collections.abc import Sequence

from agents_service.app import AgentsServiceApp, build_agents
from agents_service.settings import AgentsSettings
from platform_core.cli import main as run_cli
from platform_core.infrastructure import Infrastructure


def build() -> AgentsServiceApp:
    settings = AgentsSettings.from_env()
    return build_agents(settings, Infrastructure.from_settings(settings.core))


def main(argv: Sequence[str] | None = None) -> None:
    run_cli("agents", build, argv)


if __name__ == "__main__":
    main()
