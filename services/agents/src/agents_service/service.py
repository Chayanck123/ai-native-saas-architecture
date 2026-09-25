"""Agent-run use cases: request, execute, review (human-in-the-loop) and learn."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from agents_service.critic import Critic, GroundednessCritic, LLMCritic
from agents_service.llm import LLMClient, metered
from agents_service.models import AgentRun, RunFeedback
from agents_service.orchestrator import AgentConfig, Orchestrator, RunOutcome
from agents_service.settings import AgentsSettings
from agents_service.tools import (
    CalculatorTool,
    CurrentDateTool,
    KnowledgeSearchTool,
    SourceRegistry,
    ToolRegistry,
)
from platform_core.catalog import Events, Meters
from platform_core.clients import ServiceClient
from platform_core.context import require_principal, require_tenant_id
from platform_core.errors import Conflict, NotFound, QuotaExceeded, UpstreamUnavailable
from platform_core.ids import new_id
from platform_core.retry import PermanentError
from platform_core.service import ServiceRuntime

TERMINAL = {"succeeded", "needs_review", "rejected", "refused", "failed"}
RATEABLE = {"succeeded", "needs_review"}


class AgentService:
    def __init__(
        self,
        runtime: ServiceRuntime,
        settings: AgentsSettings,
        llm: LLMClient,
        *,
        knowledge: ServiceClient,
        billing: ServiceClient,
    ) -> None:
        self.runtime = runtime
        self.settings = settings
        self.llm = llm
        self.knowledge = knowledge
        self.billing = billing
        self.config = AgentConfig(
            max_steps=settings.max_steps,
            token_budget=settings.token_budget,
            critic_threshold=settings.critic_threshold,
            planner=settings.planner,
        )
        self._fail_open = runtime.metrics.counter(
            "entitlement_fail_open_total",
            "Runs admitted without an entitlement check because billing was unreachable",
            [],
        )

    def _critic(self) -> Critic:
        if self.settings.critic == "llm":
            return LLMCritic(metered(self.llm))
        return GroundednessCritic()

    # ------------------------------------------------------------------ create
    async def create_run(self, question: str) -> AgentRun:
        principal = require_principal()
        tenant_id = require_tenant_id()
        await self._ensure_entitled()
        async with self.runtime.unit_of_work() as uow:
            run = AgentRun(
                id=new_id("run"),
                tenant_id=tenant_id,
                created_by=principal.subject,
                question=question,
                status="queued",
                plan=[],
                steps=[],
                citations=[],
                critic_issues=[],
                knowledge_gap=False,
                input_tokens=0,
                output_tokens=0,
                llm_calls=0,
                tool_calls=0,
                created_at=self.runtime.clock.now(),
            )
            uow.session.add(run)
            await uow.publish(
                self.runtime.event(
                    Events.AGENT_RUN_REQUESTED,
                    {
                        "run_id": run.id,
                        "created_by": principal.subject,
                        "question_chars": len(question),
                    },
                    subject=run.id,
                )
            )
        return run

    async def _ensure_entitled(self) -> None:
        """Ask billing whether this tenant may spend a run's worth of credits.

        If billing is unreachable we *fail open* (and count it): blocking a paying
        customer because an internal service is down is worse than a small overage.
        """
        try:
            entitlement = await self.billing.call(
                "GET",
                "/v1/billing/entitlements",
                params={"credits": self.settings.estimated_run_credits},
            )
        except UpstreamUnavailable:
            if not self.settings.entitlement_fail_open:
                raise
            self._fail_open.labels(self.runtime.name).inc()
            self.runtime.logger.warning("billing unavailable; admitting run (fail-open)")
            return
        if not entitlement["allowed"]:
            raise QuotaExceeded(
                "your plan's included credits are used up - upgrade to keep running agents",
                reason=entitlement["reason"],
                plan=entitlement["plan"],
                upgrade_to=entitlement["upgrade_to"],
            )

    # ----------------------------------------------------------------- execute
    async def _search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        data = await self.knowledge.call(
            "POST", "/v1/search", json={"query": query, "top_k": top_k, "mode": "hybrid"}
        )
        results: list[dict[str, Any]] = data["results"]
        return results

    async def execute_run(self, run_id: str) -> dict[str, Any]:
        tenant_id = require_tenant_id()
        now = self.runtime.clock.now()
        async with self.runtime.db.session() as session:
            run = await self._get(session, tenant_id, run_id)
            if run.status in TERMINAL:
                return {"skipped": True, "status": run.status}
            run.status = "running"
            run.started_at = run.started_at or now
            question = run.question

        sources = SourceRegistry()
        tools = ToolRegistry(
            [
                KnowledgeSearchTool(self._search, sources),
                CalculatorTool(),
                CurrentDateTool(self.runtime.clock),
            ],
            tracer=self.runtime.tracer,
        )
        orchestrator = Orchestrator(
            self.llm, tools, sources, self._critic(), tracer=self.runtime.tracer, config=self.config
        )
        try:
            outcome = await orchestrator.run(question)
        except PermanentError as exc:
            # The request itself is bad (e.g. rejected by the provider): retrying won't
            # help, so record the failure on the run where the user can see it.
            await self._finish_failed(run_id, str(exc))
            return {"status": "failed"}
        await self._finish(run_id, outcome)
        return {"status": outcome.status, "tokens": outcome.usage.total_tokens}

    async def _finish(self, run_id: str, outcome: RunOutcome) -> None:
        tenant_id = require_tenant_id()
        now = self.runtime.clock.now()
        usage = outcome.usage
        input_tokens = (
            usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens
        )
        async with self.runtime.unit_of_work() as uow:
            run = await self._get(uow.session, tenant_id, run_id)
            run.status = outcome.status
            run.plan = outcome.plan
            run.steps = outcome.steps
            run.answer = outcome.answer
            run.citations = [
                {
                    "number": s.number,
                    "chunk_id": s.chunk_id,
                    "document_id": s.document_id,
                    "document_title": s.document_title,
                }
                for s in outcome.citations
            ]
            run.critic_score = outcome.review.score if outcome.review else None
            run.critic_issues = outcome.review.issues if outcome.review else []
            run.knowledge_gap = outcome.knowledge_gap
            run.model = self.llm.model
            run.input_tokens = input_tokens
            run.output_tokens = usage.output_tokens
            run.llm_calls = usage.calls
            run.tool_calls = outcome.tool_calls
            run.error = outcome.error
            run.completed_at = now
            duration_ms = int((now - (run.started_at or now)).total_seconds() * 1000)
            await uow.publish(
                self.runtime.event(
                    Events.AGENT_RUN_COMPLETED,
                    {
                        "run_id": run_id,
                        "status": outcome.status,
                        "created_by": run.created_by,
                        "critic_score": run.critic_score,
                        "knowledge_gap": outcome.knowledge_gap,
                        "citations": len(run.citations),
                        "tool_calls": outcome.tool_calls,
                        "input_tokens": input_tokens,
                        "output_tokens": usage.output_tokens,
                        "duration_ms": duration_ms,
                    },
                    subject=run_id,
                )
            )
            # Usage-based billing: one event per meter, keyed by run so retries never
            # double-charge.
            for meter, quantity in (
                (Meters.LLM_INPUT_TOKENS, input_tokens),
                (Meters.LLM_OUTPUT_TOKENS, usage.output_tokens),
                (Meters.AGENT_RUNS, 1),
            ):
                if quantity:
                    await uow.publish(
                        self.runtime.event(
                            Events.USAGE_RECORDED,
                            {
                                "meter": meter,
                                "quantity": quantity,
                                "resource_id": run_id,
                                "idempotency_key": f"{run_id}:{meter}",
                            },
                            subject=run_id,
                        )
                    )

    async def _finish_failed(self, run_id: str, error: str) -> None:
        tenant_id = require_tenant_id()
        async with self.runtime.unit_of_work() as uow:
            run = await self._get(uow.session, tenant_id, run_id)
            run.status = "failed"
            run.error = error[:2000]
            run.completed_at = self.runtime.clock.now()
            await uow.publish(
                self.runtime.event(
                    Events.AGENT_RUN_COMPLETED,
                    {"run_id": run_id, "status": "failed", "created_by": run.created_by},
                    subject=run_id,
                )
            )

    # ------------------------------------------------------------------ queries
    async def _get(self, session: Any, tenant_id: str, run_id: str) -> AgentRun:
        run = (
            await session.execute(
                select(AgentRun).where(AgentRun.tenant_id == tenant_id, AgentRun.id == run_id)
            )
        ).scalar_one_or_none()
        if run is None:
            raise NotFound(f"agent run '{run_id}' not found")
        assert isinstance(run, AgentRun)
        return run

    async def get_run(self, run_id: str) -> AgentRun:
        async with self.runtime.db.session() as session:
            return await self._get(session, require_tenant_id(), run_id)

    async def list_runs(
        self, *, status: str | None, limit: int, offset: int
    ) -> tuple[list[AgentRun], int]:
        tenant_id = require_tenant_id()
        query = select(AgentRun).where(AgentRun.tenant_id == tenant_id)
        count = select(func.count()).select_from(AgentRun).where(AgentRun.tenant_id == tenant_id)
        if status:
            query = query.where(AgentRun.status == status)
            count = count.where(AgentRun.status == status)
        async with self.runtime.db.session() as session:
            runs = (
                (
                    await session.execute(
                        query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
            total = int((await session.execute(count)).scalar_one())
        return list(runs), total

    # --------------------------------------------------- human-in-the-loop review
    async def review_run(
        self, run_id: str, *, approve: bool, corrected_answer: str | None
    ) -> AgentRun:
        principal = require_principal()
        tenant_id = require_tenant_id()
        async with self.runtime.unit_of_work() as uow:
            run = await self._get(uow.session, tenant_id, run_id)
            if run.status != "needs_review":
                raise Conflict(f"run is '{run.status}', only 'needs_review' runs can be reviewed")
            run.status = "succeeded" if approve else "rejected"
            if corrected_answer:
                run.answer = corrected_answer
            run.review = {
                "reviewer": principal.subject,
                "approved": approve,
                "corrected": bool(corrected_answer),
                "at": self.runtime.clock.now().isoformat(),
            }
            await uow.publish(
                self.runtime.event(
                    Events.AGENT_RUN_REVIEWED,
                    {
                        "run_id": run_id,
                        "approved": approve,
                        "corrected": bool(corrected_answer),
                        "reviewer": principal.subject,
                    },
                    subject=run_id,
                )
            )
        return run

    # ------------------------------------------------------------ data flywheel
    async def submit_feedback(
        self, run_id: str, *, rating: int, comment: str | None, corrected_answer: str | None
    ) -> RunFeedback:
        principal = require_principal()
        tenant_id = require_tenant_id()
        try:
            async with self.runtime.unit_of_work() as uow:
                run = await self._get(uow.session, tenant_id, run_id)
                if run.status not in RATEABLE:
                    raise Conflict(f"cannot rate a run that is '{run.status}'")
                feedback = RunFeedback(
                    id=new_id("fb"),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    user_id=principal.subject,
                    rating=rating,
                    comment=comment,
                    corrected_answer=corrected_answer,
                    created_at=self.runtime.clock.now(),
                )
                uow.session.add(feedback)
                await uow.session.flush()
                await uow.publish(
                    self.runtime.event(
                        Events.AGENT_FEEDBACK_SUBMITTED,
                        {
                            "run_id": run_id,
                            "rating": rating,
                            "user_id": principal.subject,
                            "chunk_ids": [c["chunk_id"] for c in run.citations],
                            "has_correction": bool(corrected_answer),
                        },
                        subject=run_id,
                    )
                )
        except IntegrityError as exc:
            raise Conflict("you already rated this run") from exc
        return feedback

    async def export_dataset(self) -> list[dict[str, Any]]:
        """Labelled Q&A pairs from this tenant's own usage: positively rated answers and
        human-reviewed/corrected ones. Useful as a regression-eval set or for
        fine-tuning - and unique to this customer, which is what makes it defensible."""
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            rows = (
                await session.execute(
                    select(AgentRun, RunFeedback)
                    .outerjoin(RunFeedback, RunFeedback.run_id == AgentRun.id)
                    .where(AgentRun.tenant_id == tenant_id)
                    .order_by(AgentRun.created_at)
                )
            ).all()
        dataset: dict[str, dict[str, Any]] = {}
        for run, feedback in rows:
            reviewed = bool(run.review and run.review.get("approved"))
            positive = feedback is not None and feedback.rating > 0
            if not (reviewed or positive):
                continue
            answer = (
                feedback.corrected_answer if feedback and feedback.corrected_answer else run.answer
            )
            dataset[run.id] = {
                "run_id": run.id,
                "question": run.question,
                "answer": answer,
                "citations": run.citations,
                "label_source": "human_review" if reviewed else "user_feedback",
            }
        return list(dataset.values())
