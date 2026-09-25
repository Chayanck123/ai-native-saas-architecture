from __future__ import annotations

from collections.abc import Sequence

from knowledge_service.app import KnowledgeServiceApp, build_knowledge
from knowledge_service.settings import KnowledgeSettings
from platform_core.cli import main as run_cli
from platform_core.infrastructure import Infrastructure


def build() -> KnowledgeServiceApp:
    settings = KnowledgeSettings.from_env()
    return build_knowledge(settings, Infrastructure.from_settings(settings.core))


def main(argv: Sequence[str] | None = None) -> None:
    run_cli("knowledge", build, argv)


if __name__ == "__main__":
    main()
