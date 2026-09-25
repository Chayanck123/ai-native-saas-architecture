"""Orchestrator behaviour with a scripted model (no network, fully deterministic)."""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider

from agents_service.critic import GroundednessCritic
from agents_service.llm import ModelRefusal
from agents_service.orchestrator import AgentConfig, Orchestrator
from agents_service.tools import (
    CalculatorTool,
    KnowledgeSearchTool,
    SourceRegistry,
    ToolRegistry,
    safe_eval,
)

from .conftest import ScriptedLLM, text, tool_use

TRACER = TracerProvider().get_tracer("test")
HIT = {
    "chunk_id": "doc_1:0",
    "document_id": "doc_1",
    "document_title": "Handbook",
    "text": "Refunds are available within 30 days of purchase.",
}


def make(llm: ScriptedLLM, **config: object) -> Orchestrator:
    sources = SourceRegistry()

    async def search(query: str, top_k: int) -> list[dict[str, object]]:
        return [HIT]

    tools = ToolRegistry([KnowledgeSearchTool(search, sources), CalculatorTool()], tracer=TRACER)
    return Orchestrator(
        llm,
        tools,
        sources,
        GroundednessCritic(),
        tracer=TRACER,
        config=AgentConfig(planner=False, **config),
    )  # type: ignore[arg-type]


async def test_parallel_tool_calls_are_answered_in_one_turn() -> None:
    llm = ScriptedLLM(
        [
            tool_use(
                ("t1", "search_knowledge_base", {"query": "refund window"}),
                ("t2", "calculator", {"expression": "30 * 2"}),
            ),
            text("Refunds are available within 30 days of purchase [1]."),
        ]
    )
    outcome = await make(llm).run("How long is the refund window?")
    assert outcome.status == "succeeded"
    assert outcome.tool_calls == 2
    second_request = llm.requests[1]["messages"]
    results = second_request[-1]["content"]
    assert [block["tool_use_id"] for block in results] == ["t1", "t2"]
    assert json.loads(results[1]["content"])["result"] == 60
    assert [c.chunk_id for c in outcome.citations] == ["doc_1:0"]


async def test_unsupported_answer_gets_one_revision_then_ships() -> None:
    llm = ScriptedLLM(
        [
            tool_use(("t1", "search_knowledge_base", {"query": "refunds"})),
            text("Refunds are instant and we also sell insurance to pets everywhere."),
            text("Refunds are available within 30 days of purchase [1]."),
        ]
    )
    outcome = await make(llm).run("Refund policy?")
    assert outcome.status == "succeeded"
    assert [s["type"] for s in outcome.steps].count("revision") == 1
    assert "reviewer" in llm.requests[-1]["messages"][-1]["content"]


async def test_answer_still_unsupported_after_revision_needs_review() -> None:
    llm = ScriptedLLM(
        [
            tool_use(("t1", "search_knowledge_base", {"query": "refunds"})),
            text("Our CEO personally approves every refund on Tuesdays."),
            text("Our CEO personally approves every refund on Tuesdays."),
        ]
    )
    outcome = await make(llm).run("Refund policy?")
    assert outcome.status == "needs_review"
    assert outcome.review is not None and outcome.review.score < 0.6


async def test_step_limit_forces_a_final_answer_without_tools() -> None:
    search = ("t", "search_knowledge_base", {"query": "again"})
    llm = ScriptedLLM(
        [
            tool_use(search),
            tool_use(search),
            text("Refunds are available within 30 days of purchase [1]."),
        ]
    )
    outcome = await make(llm, max_steps=2).run("Refund policy?")
    assert outcome.status == "succeeded"
    assert llm.requests[-1]["disable_tools"] is True


async def test_token_budget_stops_runaway_loops() -> None:
    search = ("t", "search_knowledge_base", {"query": "again"})
    llm = ScriptedLLM([tool_use(search, tokens=600) for _ in range(5)])
    outcome = await make(llm, token_budget=2_000).run("Loop forever")
    assert outcome.status == "needs_review"
    assert outcome.error is not None and "budget" in outcome.error
    assert outcome.usage.total_tokens >= 2_000


async def test_model_refusal_is_reported_not_retried() -> None:
    llm = ScriptedLLM([ModelRefusal("cyber", "declined")])
    outcome = await make(llm).run("Something the model declines")
    assert outcome.status == "refused"
    assert outcome.steps[-1] == {"type": "refusal", "category": "cyber"}


async def test_failing_tool_is_reported_to_the_model_not_raised() -> None:
    llm = ScriptedLLM(
        [
            tool_use(("t1", "calculator", {"expression": "__import__('os').system('ls')"})),
            text("I could not compute that."),
        ]
    )
    outcome = await make(llm, max_revisions=0).run("Compute")
    [call] = [s for s in outcome.steps if s["type"] == "tool_call"]
    assert call["is_error"] is True
    assert llm.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_safe_eval_allows_arithmetic_only() -> None:
    assert safe_eval("(1200 * 12) / 3") == 4800
    assert safe_eval("-2 ** 3 + 10 % 4") == -6
    for hostile in ["__import__('os')", "open('x')", "[1] * 10", "2 ** 1000", "a + 1"]:
        with pytest.raises(ValueError):
            safe_eval(hostile)
