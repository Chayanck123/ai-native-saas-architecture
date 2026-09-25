"""Tables owned by the knowledge service (no other service reads them directly)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

from platform_core.db import JsonType, TenantScoped, UtcDateTime


class Base(DeclarativeBase):
    pass


class EmbeddingVector(TypeDecorator[list[float]]):
    """``vector`` (pgvector) on PostgreSQL, a JSON array elsewhere.

    The column is declared without a fixed dimension so the embedding model can
    be chosen by configuration; the HNSW index casts to the configured size.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: list[float] | None, dialect: Dialect) -> Any:
        return None if value is None else [float(x) for x in value]

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        return None if value is None else [float(x) for x in value]


class Document(TenantScoped, Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)  # pending | indexed | failed
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, default=dict)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    indexed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Chunk(TenantScoped, Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(EmbeddingVector())
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class ChunkFeedback(TenantScoped, Base):
    """Aggregated user feedback per chunk - the retrieval side of the data flywheel."""

    __tablename__ = "chunk_feedback"

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    upvotes: Mapped[int] = mapped_column(Integer, default=0)
    downvotes: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


TENANT_TABLES = ("documents", "chunks", "chunk_feedback")
