"""Knowledge HTTP API (reached through the gateway at ``/v1/documents`` and ``/v1/search``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field

from knowledge_service.models import Document
from knowledge_service.service import KnowledgeService, SearchHit
from platform_core.context import Principal
from platform_core.rbac import Permission
from platform_core.service import require

router = APIRouter(prefix="/v1", tags=["knowledge"])


def _service(request: Request) -> KnowledgeService:
    service = request.app.state.knowledge
    assert isinstance(service, KnowledgeService)
    return service


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    source: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentOut(BaseModel):
    id: str
    title: str
    source: str | None
    status: str
    chunk_count: int
    embedding_model: str | None
    error: str | None
    metadata: dict[str, Any]
    created_at: datetime
    indexed_at: datetime | None
    deduplicated: bool = False

    @classmethod
    def of(cls, document: Document, *, deduplicated: bool = False) -> DocumentOut:
        return cls(
            id=document.id,
            title=document.title,
            source=document.source,
            status=document.status,
            chunk_count=document.chunk_count,
            embedding_model=document.embedding_model,
            error=document.error,
            metadata=document.metadata_,
            created_at=document.created_at,
            indexed_at=document.indexed_at,
            deduplicated=deduplicated,
        )


class DocumentPage(BaseModel):
    data: list[DocumentOut]
    total: int
    limit: int
    offset: int


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    mode: Literal["hybrid", "dense", "lexical"] = "hybrid"


class SearchHitOut(BaseModel):
    chunk_id: str
    document_id: str
    document_title: str
    text: str
    score: float
    dense_rank: int | None
    lexical_rank: int | None
    feedback_quality: float

    @classmethod
    def of(cls, hit: SearchHit) -> SearchHitOut:
        return cls(**hit.__dict__)


class SearchOut(BaseModel):
    query: str
    mode: str
    results: list[SearchHitOut]
    took_ms: float


@router.post("/documents", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentOut)
async def upload_document(
    body: DocumentIn,
    request: Request,
    response: Response,
    _: Principal = require(Permission.DOCUMENTS_WRITE),
) -> DocumentOut:
    """Upload a document. It is indexed asynchronously; poll its ``status`` or subscribe
    to the ``document.indexed`` webhook."""
    document, deduplicated = await _service(request).upload(
        title=body.title, content=body.content, source=body.source, metadata=body.metadata
    )
    response.headers["Location"] = f"/v1/documents/{document.id}"
    if deduplicated:
        response.status_code = status.HTTP_200_OK
    return DocumentOut.of(document, deduplicated=deduplicated)


@router.get("/documents", response_model=DocumentPage)
async def list_documents(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Principal = require(Permission.DOCUMENTS_READ),
) -> DocumentPage:
    documents, total = await _service(request).list_documents(
        status=status_filter, limit=limit, offset=offset
    )
    return DocumentPage(
        data=[DocumentOut.of(d) for d in documents], total=total, limit=limit, offset=offset
    )


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: str, request: Request, _: Principal = require(Permission.DOCUMENTS_READ)
) -> DocumentOut:
    return DocumentOut.of(await _service(request).get(document_id))


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: str, request: Request, _: Principal = require(Permission.DOCUMENTS_WRITE)
) -> Response:
    await _service(request).delete(document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/search", response_model=SearchOut)
async def search(
    body: SearchIn, request: Request, _: Principal = require(Permission.SEARCH_QUERY)
) -> SearchOut:
    """Hybrid semantic + keyword search over the tenant's knowledge base."""
    result = await _service(request).search(body.query, top_k=body.top_k, mode=body.mode)
    return SearchOut(
        query=result.query,
        mode=result.mode,
        results=[SearchHitOut.of(h) for h in result.hits],
        took_ms=result.took_ms,
    )
