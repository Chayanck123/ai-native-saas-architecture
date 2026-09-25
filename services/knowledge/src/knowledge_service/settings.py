from __future__ import annotations

from dataclasses import dataclass

from platform_core.config import (
    ConfigurationError,
    CoreSettings,
    env_float,
    env_int,
    env_opt,
    env_str,
)


@dataclass(frozen=True)
class KnowledgeSettings:
    core: CoreSettings
    embedder: str = "hashing"  # "hashing" (offline) or "voyage"
    embedding_dimensions: int = 384
    voyage_api_key: str | None = None
    voyage_model: str = "voyage-3.5"
    chunk_target_tokens: int = 180
    chunk_overlap_sentences: int = 1
    max_document_chars: int = 1_000_000
    rrf_k: int = 60
    feedback_weight: float = 0.2

    @classmethod
    def from_env(cls) -> KnowledgeSettings:
        embedder = env_str("KNOWLEDGE_EMBEDDER", "hashing")
        settings = cls(
            core=CoreSettings.from_env("knowledge"),
            embedder=embedder,
            embedding_dimensions=env_int(
                "KNOWLEDGE_EMBEDDING_DIMENSIONS", 1024 if embedder == "voyage" else 384
            ),
            voyage_api_key=env_opt("VOYAGE_API_KEY"),
            voyage_model=env_str("VOYAGE_MODEL", "voyage-3.5"),
            chunk_target_tokens=env_int("KNOWLEDGE_CHUNK_TOKENS", 180),
            chunk_overlap_sentences=env_int("KNOWLEDGE_CHUNK_OVERLAP", 1),
            max_document_chars=env_int("KNOWLEDGE_MAX_DOCUMENT_CHARS", 1_000_000),
            rrf_k=env_int("KNOWLEDGE_RRF_K", 60),
            feedback_weight=env_float("KNOWLEDGE_FEEDBACK_WEIGHT", 0.2),
        )
        if settings.embedder == "voyage" and not settings.voyage_api_key:
            raise ConfigurationError("KNOWLEDGE_EMBEDDER=voyage requires VOYAGE_API_KEY")
        if settings.embedder not in {"hashing", "voyage"}:
            raise ConfigurationError(f"unknown KNOWLEDGE_EMBEDDER '{settings.embedder}'")
        return settings
