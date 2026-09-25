"""Agents HTTP API (``/v1/agents/*`` through the gateway)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field

from agents_service.models import AgentRun
from agents_service.service import AgentService
from platform_core.context import Principal
from platform_core.rbac import Permission
from platform_core.service import require

router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _service(request: Request) -> AgentService:
    service = request.app.state.agents
    assert isinstance(service, AgentService)
    return service


class RunIn(BaseModel):
    question: str = Field(min_length=1, max_length=4_000)


class CitationOut(BaseModel):
    number: int
    chunk_id: str
    document_id: str
    document_title: str


class RunOut(BaseModel):
    id: str
    status: str
    question: str
    plan: list[str]
    answer: str | None
    citations: list[CitationOut]
    critic_score: float | None
    critic_issues: list[str]
    knowledge_gap: bool
    model: str | None
    usage: dict[str, int]
    error: str | None
    review: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    steps: list[dict[str, Any]] | None = None

    @classmethod
    def of(cls, run: AgentRun, *, include_steps: bool = False) -> RunOut:
        return cls(
            id=run.id,
            status=run.status,
            question=run.question,
            plan=run.plan or [],
            answer=run.answer,
            citations=[CitationOut(**c) for c in run.citations or []],
            critic_score=run.critic_score,
            critic_issues=run.critic_issues or [],
            knowledge_gap=run.knowledge_gap,
            model=run.model,
            usage={
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "llm_calls": run.llm_calls,
                "tool_calls": run.tool_calls,
            },
            error=run.error,
            review=run.review,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            steps=run.steps if include_steps else None,
        )


class RunPage(BaseModel):
    data: list[RunOut]
    total: int
    limit: int
    offset: int


class FeedbackIn(BaseModel):
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=2_000)
    corrected_answer: str | None = Field(default=None, max_length=10_000)


class ReviewIn(BaseModel):
    approve: bool
    corrected_answer: str | None = Field(default=None, max_length=10_000)


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=RunOut)
async def create_run(
    body: RunIn,
    request: Request,
    response: Response,
    _: Principal = require(Permission.AGENTS_RUN),
) -> RunOut:
    """Ask the research agent a question. Runs asynchronously: poll the run or subscribe
    to the ``agent.run.completed`` webhook. Returns 402 when the plan's credits are used up."""
    run = await _service(request).create_run(body.question)
    response.headers["Location"] = f"/v1/agents/runs/{run.id}"
    return RunOut.of(run)


@router.get("/runs", response_model=RunPage)
async def list_runs(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: Principal = require(Permission.AGENTS_READ),
) -> RunPage:
    runs, total = await _service(request).list_runs(
        status=status_filter, limit=limit, offset=offset
    )
    return RunPage(data=[RunOut.of(r) for r in runs], total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: str,
    request: Request,
    steps: bool = Query(default=False),
    _: Principal = require(Permission.AGENTS_READ),
) -> RunOut:
    return RunOut.of(await _service(request).get_run(run_id), include_steps=steps)


@router.post("/runs/{run_id}/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    run_id: str,
    body: FeedbackIn,
    request: Request,
    _: Principal = require(Permission.FEEDBACK_WRITE),
) -> dict[str, Any]:
    feedback = await _service(request).submit_feedback(
        run_id, rating=body.rating, comment=body.comment, corrected_answer=body.corrected_answer
    )
    return {"id": feedback.id, "run_id": run_id, "rating": feedback.rating}


@router.post("/runs/{run_id}/review", response_model=RunOut)
async def review_run(
    run_id: str,
    body: ReviewIn,
    request: Request,
    _: Principal = require(Permission.MEMBERS_MANAGE),
) -> RunOut:
    """Human-in-the-loop: approve, correct or reject an answer the critic held back."""
    run = await _service(request).review_run(
        run_id, approve=body.approve, corrected_answer=body.corrected_answer
    )
    return RunOut.of(run)


@router.get("/dataset")
async def export_dataset(
    request: Request, _: Principal = require(Permission.MEMBERS_MANAGE)
) -> Response:
    """Export labelled Q&A pairs (JSON Lines) for evals or fine-tuning."""
    rows = await _service(request).export_dataset()
    body = "".join(json.dumps(row) + "\n" for row in rows)
    return Response(body, media_type="application/x-ndjson")
