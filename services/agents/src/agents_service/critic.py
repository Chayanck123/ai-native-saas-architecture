"""The critic: a quality gate that decides whether an answer can ship.

It scores *groundedness* - is each claim supported by the retrieved passages? -
and the orchestrator routes low-scoring answers to a human reviewer
(human-in-the-loop) instead of presenting them as fact. Those reviews, plus
end-user thumbs up/down, become labelled data (see the feedback export).

Two implementations:

* :class:`GroundednessCritic` - deterministic and free (term overlap between each
  answer sentence and the sources). The default.
* :class:`LLMCritic` - asks the LLM to grade the answer with a structured output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from agents_service.llm import LLMClient
from agents_service.prompts import CRITIC_SCHEMA, CRITIC_SYSTEM
from agents_service.text import content_terms, sentences, strip_citations
from agents_service.tools import Source

NO_ANSWER_MARKERS = (
    "does not appear to cover",
    "does not cover",
    "could not find",
    "couldn't find",
    "no information",
)


@dataclass(frozen=True)
class Review:
    score: float
    issues: list[str] = field(default_factory=list)
    method: str = "heuristic"
    knowledge_gap: bool = False


class Critic(Protocol):
    async def review(self, question: str, answer: str, sources: list[Source]) -> Review: ...


def is_knowledge_gap(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in NO_ANSWER_MARKERS)


class GroundednessCritic:
    def __init__(self, min_support: float = 0.5) -> None:
        self._min_support = min_support

    async def review(self, question: str, answer: str, sources: list[Source]) -> Review:
        if is_knowledge_gap(answer):
            return Review(
                0.0, ["The knowledge base has no answer to this question"], knowledge_gap=True
            )
        known = set(content_terms(" ".join(s.text for s in sources)))
        claims = [s for s in sentences(strip_citations(answer)) if content_terms(s)]
        if not claims:
            return Review(0.0, ["The answer is empty"])
        issues = []
        for claim in claims:
            terms = set(content_terms(claim))
            if len(terms & known) < self._min_support * len(terms):
                issues.append(f"Not supported by the sources: {claim[:160]}")
        score = 1.0 - len(issues) / len(claims)
        if sources and "[" not in answer:
            score *= 0.8
            issues.append("The answer does not cite its sources")
        return Review(round(score, 3), issues)


class LLMCritic:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def review(self, question: str, answer: str, sources: list[Source]) -> Review:
        if is_knowledge_gap(answer):
            return Review(
                0.0,
                ["The knowledge base has no answer to this question"],
                method="llm",
                knowledge_gap=True,
            )
        passages = "\n".join(f"[{s.number}] {s.text}" for s in sources) or "(none)"
        prompt = (
            f"<question>{question}</question>\n<sources>{passages}</sources>\n"
            f"<answer>{answer}</answer>"
        )
        response = await self._llm.complete(
            system=CRITIC_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            json_schema=CRITIC_SCHEMA,
            max_tokens=2048,
        )
        verdict = json.loads(response.text or "{}")
        score = max(0.0, min(1.0, float(verdict.get("score", 0.0))))
        return Review(round(score, 3), [str(i) for i in verdict.get("issues", [])], method="llm")
