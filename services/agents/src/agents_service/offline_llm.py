"""A deterministic, dependency-free stand-in for an LLM.

It lets the whole platform - agent loop, tool calls, citations, critic, billing -
run in tests, CI and local demos without an API key or network access. It is
*not* intelligent: it answers by extracting the retrieved sentences that best
overlap the question. Switch to the Anthropic API with ``AGENTS_LLM_PROVIDER=anthropic``.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from agents_service.llm import LLMResponse, LLMUsage, Message, ToolCall, ToolSpec
from agents_service.text import content_terms, sentences, strip_citations

_ARITHMETIC = re.compile(r"[\d.]+(?:\s*[-+*/%]\s*\(?\s*[\d.]+\s*\)?)+")
_NO_ANSWER = "The knowledge base does not appear to cover this question."


def _tokens(value: Any) -> int:
    return max(1, math.ceil(len(json.dumps(value, default=str)) / 4))


def _first_user_text(messages: list[Message]) -> str:
    for message in messages:
        if message["role"] == "user":
            content = message["content"]
            if isinstance(content, str):
                return content
            return " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    return ""


def _question(messages: list[Message]) -> str:
    text = _first_user_text(messages)
    match = re.search(r"Question:\s*(.+)", text, flags=re.S)
    return (match.group(1) if match else text).strip()


def _tool_outputs(messages: list[Message]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (search hits, calculator results) found in tool_result blocks."""
    names: dict[str, str] = {}
    hits: list[dict[str, Any]] = []
    calculations: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                names[block["id"]] = block["name"]
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                try:
                    payload = json.loads(block.get("content") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                name = names.get(block.get("tool_use_id", ""))
                if name == "search_knowledge_base":
                    hits.extend(payload.get("results", []))
                elif name == "calculator" and "result" in payload:
                    calculations.append(payload)
    return hits, calculations


def _revision_requested(messages: list[Message]) -> bool:
    last = messages[-1] if messages else {}
    return last.get("role") == "user" and "reviewer" in str(last.get("content", ""))


class OfflineLLM:
    def __init__(self, model: str = "offline-extractive-v1") -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "offline"

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_schema: dict[str, Any] | None = None,
        disable_tools: bool = False,
    ) -> LLMResponse:
        prompt_tokens = (
            _tokens(system) + _tokens(messages) + (_tokens([t.name for t in tools]) if tools else 0)
        )
        if json_schema is not None:
            payload = self._structured(json_schema, messages)
            text = json.dumps(payload)
            return self._reply(text, [], prompt_tokens)

        question = _question(messages)
        hits, calculations = _tool_outputs(messages)
        tool_names = {t.name for t in tools or []}
        already_used_tools = bool(hits or calculations) or any(
            isinstance(m["content"], list)
            and any(b.get("type") == "tool_result" for b in m["content"])
            for m in messages
        )
        if tool_names and not already_used_tools and not disable_tools:
            calls: list[ToolCall] = []
            if "search_knowledge_base" in tool_names:
                calls.append(
                    ToolCall(
                        "toolu_offline_search",
                        "search_knowledge_base",
                        {"query": question, "top_k": 5},
                    )
                )
            expression = _ARITHMETIC.search(question)
            if expression and "calculator" in tool_names:
                calls.append(
                    ToolCall(
                        "toolu_offline_calc",
                        "calculator",
                        {"expression": expression.group(0).strip()},
                    )
                )
            if calls:
                return self._reply("I'll look that up.", calls, prompt_tokens)

        return self._reply(
            self._answer(question, hits, calculations, strict=_revision_requested(messages)),
            [],
            prompt_tokens,
        )

    def _answer(
        self,
        question: str,
        hits: list[dict[str, Any]],
        calculations: list[dict[str, Any]],
        *,
        strict: bool,
    ) -> str:
        parts = [f"{c['expression']} = {c['result']}." for c in calculations]
        wanted = set(content_terms(question))
        scored: list[tuple[float, int, str]] = []
        for hit in hits:
            body = str(hit.get("text", ""))
            head, sep, rest = body.partition(": ")
            if sep and "." not in head and len(head) < 120:
                body = rest  # drop the "Heading > Subheading:" prefix added by the chunker
            for sentence in sentences(body):
                overlap = len(wanted & set(content_terms(sentence)))
                if overlap:
                    scored.append((overlap / (len(wanted) or 1), -int(hit["source"]), sentence))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        chosen: list[str] = []
        # Keep sentences that are nearly as relevant as the best one (stricter on revision).
        floor = scored[0][0] * (0.75 if strict else 0.5) if scored else 0.0
        for score, negative_source, sentence in scored:
            if len(chosen) == (2 if strict else 3) or score < floor:
                break
            cited = f"{sentence.rstrip('.')} [{-negative_source}]."
            if cited not in chosen:
                chosen.append(cited)
        if chosen:
            parts.append(" ".join(chosen))
        elif not calculations:
            parts.append(_NO_ANSWER)
        return " ".join(parts)

    def _structured(self, schema: dict[str, Any], messages: list[Message]) -> dict[str, Any]:
        properties = schema.get("properties", {})
        question = _question(messages)
        if "steps" in properties:
            steps = ["Search the knowledge base for passages relevant to the question"]
            if _ARITHMETIC.search(question):
                steps.append("Compute the arithmetic with the calculator")
            steps.append("Answer concisely, citing each source used")
            return {"steps": steps}
        if "score" in properties:
            text = _first_user_text(messages)
            answer = text.split("<answer>")[-1].split("</answer>")[0]
            sources = text.split("<sources>")[-1].split("</sources>")[0]
            claims = [s for s in sentences(strip_citations(answer)) if content_terms(s)]
            known = set(content_terms(sources))
            unsupported = [
                c
                for c in claims
                if len(set(content_terms(c)) & known) < 0.5 * len(set(content_terms(c)))
            ]
            score = 1 - len(unsupported) / len(claims) if claims else 0.0
            return {"score": round(score, 3), "issues": [f"Unsupported: {c}" for c in unsupported]}
        return {}

    def _reply(self, text: str, calls: list[ToolCall], prompt_tokens: int) -> LLMResponse:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        content += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ]
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            usage=LLMUsage(input_tokens=prompt_tokens, output_tokens=_tokens(content)),
            model=self._model,
            content=content,
        )
