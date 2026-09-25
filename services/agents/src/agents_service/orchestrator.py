"""Agentic orchestration: Planner -> Actor (tool-use loop) -> Critic.

1. **Plan** - decompose the question into a few concrete steps (structured output).
2. **Act** - the model calls tools (knowledge search, calculator, date) in a loop.
   Parallel tool calls in one turn are executed concurrently and their results
   returned together. The loop is bounded by a step limit and a token budget.
3. **Criticise** - score groundedness. A weak answer gets one revision round with
   the critic's feedback; if it is still weak (or the knowledge base simply has no
   answer) the run is routed to a human reviewer instead of being shipped.

Every call is traced, and all token usage lands in a :class:`UsageMeter`, which
the service turns into usage-based billing events.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import Tracer

from agents_service.critic import Critic, Review
from agents_service.llm import (
    LLMClient,
    LLMResponse,
    Message,
    ModelRefusal,
    UsageMeter,
    metered,
    metering,
)
from agents_service.prompts import ACTOR_SYSTEM, PLAN_SCHEMA, PLANNER_SYSTEM, revision_request
from agents_service.tools import Source, SourceRegistry, ToolRegistry, ToolResult


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int = 6
    token_budget: int = 150_000
    max_tokens_per_call: int = 16_000
    critic_threshold: float = 0.6
    max_revisions: int = 1
    planner: bool = True


class BudgetExceeded(Exception):
    pass


@dataclass
class RunOutcome:
    status: str  # succeeded | needs_review | refused
    answer: str
    plan: list[str]
    citations: list[Source]
    review: Review | None
    usage: UsageMeter
    steps: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    error: str | None = None

    @property
    def knowledge_gap(self) -> bool:
        return bool(self.review and self.review.knowledge_gap)


def _preview(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


class Orchestrator:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        sources: SourceRegistry,
        critic: Critic,
        *,
        tracer: Tracer,
        config: AgentConfig | None = None,
    ) -> None:
        self._llm = metered(llm)
        self._tools = tools
        self._sources = sources
        self._critic = critic
        self._tracer = tracer
        self._config = config or AgentConfig()

    async def run(self, question: str) -> RunOutcome:
        steps: list[dict[str, Any]] = []
        with (
            metering() as usage,
            self._tracer.start_as_current_span(
                "invoke_agent research_assistant",
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "research_assistant",
                    "gen_ai.request.model": self._llm.model,
                },
            ) as span,
        ):
            outcome = RunOutcome("succeeded", "", [], [], None, usage, steps)
            try:
                if self._config.planner:
                    outcome.plan = await self._plan(question, steps, usage)
                messages: list[Message] = [
                    {"role": "user", "content": self._task_prompt(question, outcome.plan)}
                ]
                outcome.answer = await self._act(messages, steps, usage, outcome)
                review = await self._criticise(question, outcome.answer, steps)
                revisions = 0
                while (
                    review.score < self._config.critic_threshold
                    and not review.knowledge_gap
                    and revisions < self._config.max_revisions
                ):
                    revisions += 1
                    steps.append({"type": "revision", "attempt": revisions})
                    messages.append({"role": "user", "content": revision_request(review.issues)})
                    outcome.answer = await self._act(messages, steps, usage, outcome)
                    review = await self._criticise(question, outcome.answer, steps)
                outcome.review = review
                if review.score < self._config.critic_threshold:
                    outcome.status = "needs_review"
            except ModelRefusal as exc:
                outcome.status, outcome.error = "refused", str(exc)
                steps.append({"type": "refusal", "category": exc.category})
            except BudgetExceeded as exc:
                outcome.status, outcome.error = "needs_review", str(exc)
                steps.append({"type": "budget_exceeded", "detail": str(exc)})
            outcome.citations = self._sources.cited_in(outcome.answer)
            span.set_attributes(
                {
                    "agent.status": outcome.status,
                    "agent.tool_calls": outcome.tool_calls,
                    "gen_ai.usage.input_tokens": usage.input_tokens,
                    "gen_ai.usage.output_tokens": usage.output_tokens,
                }
            )
            return outcome

    # ----------------------------------------------------------------- phases
    async def _plan(
        self, question: str, steps: list[dict[str, Any]], usage: UsageMeter
    ) -> list[str]:
        self._check_budget(usage)
        with self._tracer.start_as_current_span("agent.plan"):
            response = await self._llm.complete(
                system=PLANNER_SYSTEM,
                messages=[{"role": "user", "content": f"Question: {question}"}],
                json_schema=PLAN_SCHEMA,
                max_tokens=4_000,
            )
        try:
            plan = [str(step) for step in json.loads(response.text).get("steps", [])][:5]
        except (json.JSONDecodeError, AttributeError):
            plan = []  # planning is advisory; a malformed plan must not fail the run
        steps.append({"type": "plan", "steps": plan, **self._usage(response)})
        return plan

    async def _act(
        self,
        messages: list[Message],
        steps: list[dict[str, Any]],
        usage: UsageMeter,
        outcome: RunOutcome,
    ) -> str:
        specs = self._tools.specs()
        for _ in range(self._config.max_steps):
            self._check_budget(usage)
            response = await self._llm.complete(
                system=ACTOR_SYSTEM,
                messages=messages,
                tools=specs,
                max_tokens=self._config.max_tokens_per_call,
            )
            messages.append({"role": "assistant", "content": response.content})
            steps.append(
                {
                    "type": "llm",
                    "stop_reason": response.stop_reason,
                    "text": _preview(response.text),
                    **self._usage(response),
                }
            )
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "tool_use" and response.tool_calls:
                results = await asyncio.gather(
                    *(self._tools.execute(call) for call in response.tool_calls)
                )
                outcome.tool_calls += len(results)
                blocks: list[dict[str, Any]] = []
                for call, result in zip(response.tool_calls, results, strict=True):
                    steps.append(
                        {
                            "type": "tool_call",
                            "name": call.name,
                            "input": call.input,
                            "is_error": result.is_error,
                            "output": _preview(result.content, 300),
                        }
                    )
                    blocks.append(self._tool_result_block(call.id, result))
                messages.append({"role": "user", "content": blocks})  # all results, one turn
                continue
            return response.text.strip()

        # Step limit reached: ask for the best answer with what was gathered so far.
        self._check_budget(usage)
        messages.append(
            {"role": "user", "content": "Stop using tools now and answer with what you have."}
        )
        response = await self._llm.complete(
            system=ACTOR_SYSTEM,
            messages=messages,
            tools=specs,
            disable_tools=True,
            max_tokens=self._config.max_tokens_per_call,
        )
        messages.append({"role": "assistant", "content": response.content})
        steps.append(
            {
                "type": "llm",
                "stop_reason": response.stop_reason,
                "forced_final": True,
                "text": _preview(response.text),
                **self._usage(response),
            }
        )
        return response.text.strip()

    async def _criticise(self, question: str, answer: str, steps: list[dict[str, Any]]) -> Review:
        with self._tracer.start_as_current_span("agent.critic") as span:
            review = await self._critic.review(question, answer, self._sources.all)
            span.set_attribute("agent.critic.score", review.score)
        steps.append(
            {
                "type": "critique",
                "score": review.score,
                "issues": review.issues[:5],
                "method": review.method,
                "knowledge_gap": review.knowledge_gap,
            }
        )
        return review

    # ---------------------------------------------------------------- helpers
    def _check_budget(self, usage: UsageMeter) -> None:
        if usage.total_tokens >= self._config.token_budget:
            raise BudgetExceeded(
                f"token budget of {self._config.token_budget} exhausted "
                f"({usage.total_tokens} tokens used)"
            )

    @staticmethod
    def _task_prompt(question: str, plan: list[str]) -> str:
        if not plan:
            return f"Question: {question}"
        numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(plan, start=1))
        return f"<plan>\n{numbered}\n</plan>\n\nQuestion: {question}"

    @staticmethod
    def _tool_result_block(tool_use_id: str, result: ToolResult) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": result.content,
        }
        if result.is_error:
            block["is_error"] = True
        return block

    @staticmethod
    def _usage(response: LLMResponse) -> dict[str, int]:
        return {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
