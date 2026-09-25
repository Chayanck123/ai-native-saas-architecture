"""Knowledge use cases: upload -> (async) index -> hybrid search -> learn from feedback."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.chunking import chunk_text
from knowledge_service.embeddings import Embedder, cosine
from knowledge_service.models import Chunk, ChunkFeedback, Document
from knowledge_service.retrieval import (
    BM25,
    feedback_multiplier,
    feedback_quality,
    reciprocal_rank_fusion,
)
from knowledge_service.settings import KnowledgeSettings
from knowledge_service.text import terms
from platform_core.catalog import Events, Meters
from platform_core.context import PrincipalKind, current_principal, require_tenant_id
from platform_core.errors import NotFound, ValidationFailed
from platform_core.ids import new_id
from platform_core.retry import PermanentError
from platform_core.service import ServiceRuntime

SearchMode = Literal["hybrid", "dense", "lexical"]

# Upper bound on chunks scanned per query when no vector index is available
# (SQLite / local development). PostgreSQL uses the HNSW and GIN indexes instead.
IN_PROCESS_SCAN_LIMIT = 50_000


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    document_id: str
    document_title: str
    text: str
    score: float
    dense_rank: int | None
    lexical_rank: int | None
    feedback_quality: float


@dataclass(frozen=True)
class SearchResult:
    query: str
    mode: SearchMode
    hits: list[SearchHit]
    took_ms: float


class KnowledgeService:
    def __init__(
        self, runtime: ServiceRuntime, embedder: Embedder, settings: KnowledgeSettings
    ) -> None:
        self.runtime = runtime
        self.embedder = embedder
        self.settings = settings

    # ------------------------------------------------------------------ setup
    async def install_indexes(self) -> None:
        """PostgreSQL only: ANN (HNSW) and full-text (GIN) indexes."""
        if not self.runtime.db.is_postgres:
            return
        dim = self.embedder.dimensions
        async with self.runtime.db.migration() as conn:
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_{dim} ON chunks "
                    f"USING hnsw ((embedding::vector({dim})) vector_cosine_ops)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS chunks_text_fts ON chunks "
                    "USING gin (to_tsvector('english', text))"
                )
            )

    # ----------------------------------------------------------------- upload
    async def upload(
        self,
        *,
        title: str,
        content: str,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Document, bool]:
        """Store a document and announce it; indexing happens asynchronously.

        Returns ``(document, deduplicated)``: identical content already uploaded by
        this tenant is returned instead of being embedded (and billed) twice.
        """
        tenant_id = require_tenant_id()
        if not content.strip():
            raise ValidationFailed("document content is empty")
        if len(content) > self.settings.max_document_chars:
            raise ValidationFailed(
                f"document exceeds {self.settings.max_document_chars} characters"
            )
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        principal = current_principal()
        async with self.runtime.unit_of_work() as uow:
            existing = (
                await uow.session.execute(
                    select(Document).where(
                        Document.tenant_id == tenant_id,
                        Document.content_hash == content_hash,
                        Document.status != "failed",
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing, True
            document = Document(
                id=new_id("doc"),
                tenant_id=tenant_id,
                title=title,
                source=source,
                content=content,
                content_hash=content_hash,
                status="pending",
                metadata_=metadata or {},
                chunk_count=0,
                created_by=principal.subject if principal else None,
                created_at=self.runtime.clock.now(),
            )
            uow.session.add(document)
            await uow.publish(
                self.runtime.event(
                    Events.DOCUMENT_UPLOADED,
                    {"document_id": document.id, "title": title, "chars": len(content)},
                    subject=document.id,
                )
            )
        return document, False

    # ------------------------------------------------------------------ index
    async def index(self, document_id: str) -> dict[str, Any]:
        """Chunk + embed + store. Runs in a worker; safe to retry and to repeat."""
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            document = await self._get(session, tenant_id, document_id)
            content, content_hash = document.content, document.content_hash

        started = time.perf_counter()
        drafts = chunk_text(
            content,
            target_tokens=self.settings.chunk_target_tokens,
            overlap_sentences=self.settings.chunk_overlap_sentences,
        )
        if not drafts:
            await self._mark_failed(document_id, "document produced no indexable text")
            raise PermanentError(f"document {document_id} produced no indexable text")
        # Transient provider failures raise RetryableError -> the task is retried with
        # backoff. Permanent ones (e.g. input rejected) fail the document for good.
        try:
            batch = await self.embedder.embed([d.text for d in drafts], input_type="document")
        except PermanentError as exc:
            await self._mark_failed(document_id, str(exc))
            raise

        now = self.runtime.clock.now()
        async with self.runtime.unit_of_work() as uow:
            document = await self._get(uow.session, tenant_id, document_id)
            await uow.session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            for draft, vector in zip(drafts, batch.vectors, strict=True):
                uow.session.add(
                    Chunk(
                        id=f"{document_id}:{draft.ordinal}",
                        tenant_id=tenant_id,
                        document_id=document_id,
                        ordinal=draft.ordinal,
                        text=draft.text,
                        token_count=draft.token_count,
                        embedding=vector,
                        created_at=now,
                    )
                )
            document.status = "indexed"
            document.chunk_count = len(drafts)
            document.embedding_model = batch.model
            document.error = None
            document.indexed_at = now
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            await uow.publish(
                self.runtime.event(
                    Events.DOCUMENT_INDEXED,
                    {
                        "document_id": document_id,
                        "chunks": len(drafts),
                        "embedding_model": batch.model,
                        "duration_ms": duration_ms,
                    },
                    subject=document_id,
                )
            )
            await uow.publish(
                self.runtime.event(
                    Events.USAGE_RECORDED,
                    {
                        "meter": Meters.EMBEDDING_TOKENS,
                        "quantity": batch.tokens,
                        "idempotency_key": f"{document_id}:embed:{content_hash[:16]}",
                        "resource_id": document_id,
                    },
                    subject=document_id,
                )
            )
        return {"chunks": len(drafts), "embedding_tokens": batch.tokens}

    async def _mark_failed(self, document_id: str, error: str) -> None:
        tenant_id = require_tenant_id()
        async with self.runtime.unit_of_work() as uow:
            document = await self._get(uow.session, tenant_id, document_id)
            document.status = "failed"
            document.error = error
            await uow.publish(
                self.runtime.event(
                    Events.DOCUMENT_FAILED,
                    {"document_id": document_id, "error": error},
                    subject=document_id,
                )
            )

    # ---------------------------------------------------------------- queries
    async def get(self, document_id: str) -> Document:
        async with self.runtime.db.session() as session:
            return await self._get(session, require_tenant_id(), document_id)

    async def list_documents(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[Document], int]:
        tenant_id = require_tenant_id()
        query = select(Document).where(Document.tenant_id == tenant_id)
        count = select(func.count()).select_from(Document).where(Document.tenant_id == tenant_id)
        if status:
            query = query.where(Document.status == status)
            count = count.where(Document.status == status)
        query = query.order_by(Document.created_at.desc(), Document.id.desc())
        async with self.runtime.db.session() as session:
            rows = (await session.execute(query.limit(limit).offset(offset))).scalars().all()
            total = int((await session.execute(count)).scalar_one())
        return list(rows), total

    async def delete(self, document_id: str) -> None:
        tenant_id = require_tenant_id()
        async with self.runtime.unit_of_work() as uow:
            document = await self._get(uow.session, tenant_id, document_id)
            await uow.session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            await uow.session.delete(document)
            await uow.publish(
                self.runtime.event(
                    Events.DOCUMENT_DELETED, {"document_id": document_id}, subject=document_id
                )
            )

    async def _get(self, session: AsyncSession, tenant_id: str, document_id: str) -> Document:
        document = (
            await session.execute(
                select(Document).where(Document.tenant_id == tenant_id, Document.id == document_id)
            )
        ).scalar_one_or_none()
        if document is None:
            raise NotFound(f"document '{document_id}' not found")
        return document

    # ----------------------------------------------------------------- search
    async def search(
        self, query: str, *, top_k: int = 5, mode: SearchMode = "hybrid"
    ) -> SearchResult:
        tenant_id = require_tenant_id()
        started = time.perf_counter()
        candidates = max(20, top_k * 4)
        query_terms = terms(query)

        dense: list[str] = []
        if mode in ("hybrid", "dense"):
            query_vector = (await self.embedder.embed([query], input_type="query")).vectors[0]
            dense = await self._dense_candidates(tenant_id, query_vector, candidates)
        lexical: list[str] = []
        if mode in ("hybrid", "lexical") and query_terms:
            lexical = await self._lexical_candidates(tenant_id, query_terms, candidates)

        rankings = {name: ids for name, ids in (("dense", dense), ("lexical", lexical)) if ids}
        fused = reciprocal_rank_fusion(rankings, k=self.settings.rrf_k)
        hits: list[SearchHit] = []
        if fused:
            async with self.runtime.db.session() as session:
                feedback = await self._feedback(session, tenant_id, list(fused))
                weight = self.settings.feedback_weight
                scored = {
                    chunk_id: score * feedback_multiplier(*feedback.get(chunk_id, (0, 0)), weight)
                    for chunk_id, score in fused.items()
                }
                top = sorted(scored, key=lambda cid: (-scored[cid], cid))[:top_k]
                rows = (
                    await session.execute(
                        select(Chunk, Document.title)
                        .join(Document, Document.id == Chunk.document_id)
                        .where(Chunk.tenant_id == tenant_id, Chunk.id.in_(top))
                    )
                ).all()
            by_id = {chunk.id: (chunk, title) for chunk, title in rows}
            dense_rank = {cid: i for i, cid in enumerate(dense, start=1)}
            lexical_rank = {cid: i for i, cid in enumerate(lexical, start=1)}
            for chunk_id in top:
                if chunk_id not in by_id:
                    continue
                chunk, title = by_id[chunk_id]
                hits.append(
                    SearchHit(
                        chunk_id=chunk_id,
                        document_id=chunk.document_id,
                        document_title=title,
                        text=chunk.text,
                        score=round(scored[chunk_id], 6),
                        dense_rank=dense_rank.get(chunk_id),
                        lexical_rank=lexical_rank.get(chunk_id),
                        feedback_quality=round(
                            feedback_quality(*feedback.get(chunk_id, (0, 0))), 4
                        ),
                    )
                )

        took_ms = round((time.perf_counter() - started) * 1000, 2)
        await self._record_search(query, mode, len(hits))
        return SearchResult(query=query, mode=mode, hits=hits, took_ms=took_ms)

    async def _dense_candidates(
        self, tenant_id: str, query_vector: list[float], limit: int
    ) -> list[str]:
        async with self.runtime.db.session() as session:
            if self.runtime.db.is_postgres:
                dim = int(self.embedder.dimensions)  # an integer from config, never user input
                literal = "[" + ",".join(f"{v:.7f}" for v in query_vector) + "]"
                rows = await session.execute(
                    text(
                        "SELECT id FROM chunks WHERE tenant_id = :tenant "  # noqa: S608
                        f"ORDER BY (embedding::vector({dim})) <=> CAST(:q AS vector({dim})) "
                        "LIMIT :limit"
                    ),
                    {"tenant": tenant_id, "q": literal, "limit": limit},
                )
                return [row[0] for row in rows]
            rows = await session.execute(
                select(Chunk.id, Chunk.embedding)
                .where(Chunk.tenant_id == tenant_id)
                .limit(IN_PROCESS_SCAN_LIMIT)
            )
            scored = [(cid, cosine(query_vector, vector)) for cid, vector in rows]
        ranked = sorted((item for item in scored if item[1] > 0), key=lambda i: (-i[1], i[0]))
        return [cid for cid, _ in ranked[:limit]]

    async def _lexical_candidates(
        self, tenant_id: str, query_terms: list[str], limit: int
    ) -> list[str]:
        async with self.runtime.db.session() as session:
            if self.runtime.db.is_postgres:
                ts_query = " | ".join(sorted(set(query_terms)))
                rows = await session.execute(
                    text(
                        "SELECT id FROM chunks WHERE tenant_id = :tenant "
                        "AND to_tsvector('english', text) @@ to_tsquery('english', :q) "
                        "ORDER BY ts_rank_cd(to_tsvector('english', text), "
                        "to_tsquery('english', :q)) DESC, id LIMIT :limit"
                    ),
                    {"tenant": tenant_id, "q": ts_query, "limit": limit},
                )
                return [row[0] for row in rows]
            rows = await session.execute(
                select(Chunk.id, Chunk.text)
                .where(Chunk.tenant_id == tenant_id)
                .limit(IN_PROCESS_SCAN_LIMIT)
            )
            index = BM25({cid: terms(body) for cid, body in rows})
        return [cid for cid, _ in index.search(query_terms, limit)]

    async def _feedback(
        self, session: AsyncSession, tenant_id: str, chunk_ids: list[str]
    ) -> dict[str, tuple[int, int]]:
        rows = await session.execute(
            select(ChunkFeedback.chunk_id, ChunkFeedback.upvotes, ChunkFeedback.downvotes).where(
                ChunkFeedback.tenant_id == tenant_id, ChunkFeedback.chunk_id.in_(chunk_ids)
            )
        )
        return {cid: (up, down) for cid, up, down in rows}

    async def _record_search(self, query: str, mode: str, results: int) -> None:
        principal = current_principal()
        if principal is None or principal.kind is PrincipalKind.SERVICE:
            return  # searches made by our own agents are metered as part of the agent run
        await self.runtime.publish(
            self.runtime.event(
                Events.SEARCH_PERFORMED,
                {
                    "mode": mode,
                    "results": results,
                    "query_chars": len(query),
                    "user_id": principal.subject,
                },
            )
        )
        await self.runtime.publish(
            self.runtime.event(
                Events.USAGE_RECORDED,
                {"meter": Meters.SEARCHES, "quantity": 1, "idempotency_key": new_id("search")},
            )
        )

    # --------------------------------------------------------------- feedback
    async def apply_feedback(self, chunk_ids: list[str], rating: int) -> dict[str, Any]:
        """Fold a thumbs-up/down on an answer into the chunks that grounded it."""
        tenant_id = require_tenant_id()
        if rating not in (1, -1) or not chunk_ids:
            return {"updated": 0}
        now = self.runtime.clock.now()
        async with self.runtime.db.session() as session:
            existing = {
                row.chunk_id: row
                for row in (
                    await session.execute(
                        select(ChunkFeedback).where(
                            ChunkFeedback.tenant_id == tenant_id,
                            ChunkFeedback.chunk_id.in_(chunk_ids),
                        )
                    )
                ).scalars()
            }
            for chunk_id in dict.fromkeys(chunk_ids):
                row = existing.get(chunk_id)
                if row is None:
                    row = ChunkFeedback(
                        chunk_id=chunk_id,
                        tenant_id=tenant_id,
                        upvotes=0,
                        downvotes=0,
                        updated_at=now,
                    )
                    session.add(row)
                if rating > 0:
                    row.upvotes += 1
                else:
                    row.downvotes += 1
                row.updated_at = now
        return {"updated": len(set(chunk_ids))}
