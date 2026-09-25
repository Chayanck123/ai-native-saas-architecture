"""Tools the agent can call, and the registry that executes them safely.

Every tool runs *as the run's tenant* (it can only reach that tenant's data),
is time-boxed, traced, and never crashes the run: a failure is handed back to
the model as an ``is_error`` tool result so it can adapt.
"""

from __future__ import annotations

import ast
import asyncio
import json
import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry.trace import Status, StatusCode, Tracer

from agents_service.llm import ToolCall, ToolSpec
from agents_service.text import cited_numbers
from platform_core.clock import Clock


@dataclass(frozen=True)
class Source:
    number: int
    chunk_id: str
    document_id: str
    document_title: str
    text: str


class SourceRegistry:
    """Gives each retrieved passage a stable citation number for the whole run."""

    def __init__(self) -> None:
        self._by_chunk: dict[str, Source] = {}

    def register(self, hit: dict[str, Any]) -> Source:
        chunk_id = str(hit["chunk_id"])
        if chunk_id not in self._by_chunk:
            self._by_chunk[chunk_id] = Source(
                number=len(self._by_chunk) + 1,
                chunk_id=chunk_id,
                document_id=str(hit.get("document_id", "")),
                document_title=str(hit.get("document_title", "")),
                text=str(hit.get("text", "")),
            )
        return self._by_chunk[chunk_id]

    @property
    def all(self) -> list[Source]:
        return sorted(self._by_chunk.values(), key=lambda s: s.number)

    def cited_in(self, answer: str) -> list[Source]:
        numbers = cited_numbers(answer)
        return [s for s in self.all if s.number in numbers]


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def run(self, arguments: dict[str, Any]) -> ToolResult: ...


SearchFunction = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


class KnowledgeSearchTool:
    def __init__(self, search: SearchFunction, sources: SourceRegistry) -> None:
        self._search = search
        self._sources = sources

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_knowledge_base",
            description=(
                "Search this workspace's private knowledge base (the documents its members "
                "uploaded) with hybrid semantic + keyword search. Returns the most relevant "
                "passages, each with a source number to cite as [n]."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for, phrased as a question or keywords",
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "How many passages to return (default 5)",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult("query must not be empty", is_error=True)
        top_k = max(1, min(10, int(arguments.get("top_k", 5))))
        hits = await self._search(query, top_k)
        results = []
        for hit in hits:
            source = self._sources.register(hit)
            results.append(
                {
                    "source": source.number,
                    "document_title": source.document_title,
                    "text": source.text,
                }
            )
        return ToolResult(json.dumps({"results": results}))


_OPERATORS: dict[type, Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_eval(expression: str) -> float:
    """Evaluate arithmetic without ``eval``: only numbers and + - * / // % ** ( )."""
    if len(expression) > 200:
        raise ValueError("expression too long")

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("exponent too large")
            return float(_OPERATORS[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
            return float(_OPERATORS[type(node.op)](visit(node.operand)))
        raise ValueError(f"unsupported expression element: {type(node).__name__}")

    return visit(ast.parse(expression, mode="eval"))


class CalculatorTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calculator",
            description="Evaluate an arithmetic expression, e.g. '(1200 * 12) / 3'.",
            input_schema={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
                "additionalProperties": False,
            },
        )

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        expression = str(arguments.get("expression", ""))
        try:
            value = safe_eval(expression)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            return ToolResult(f"cannot evaluate '{expression}': {exc}", is_error=True)
        result: float | int = int(value) if value.is_integer() else round(value, 6)
        return ToolResult(json.dumps({"expression": expression, "result": result}))


class CurrentDateTool:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_current_date",
            description="Today's date (UTC) in ISO format.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        now = self._clock.now()
        return ToolResult(
            json.dumps({"date": now.date().isoformat(), "weekday": now.strftime("%A")})
        )


class ToolRegistry:
    def __init__(self, tools: list[Tool], *, tracer: Tracer, timeout: float = 30.0) -> None:
        self._tools = {tool.spec.name: tool for tool in tools}
        self._tracer = tracer
        self._timeout = timeout

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(f"unknown tool '{call.name}'", is_error=True)
        with self._tracer.start_as_current_span(
            f"execute_tool {call.name}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": call.name,
                "gen_ai.tool.call.id": call.id,
            },
        ) as span:
            try:
                result = await asyncio.wait_for(tool.run(call.input), timeout=self._timeout)
            except Exception as exc:  # a failing tool must not crash the whole run
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                return ToolResult(f"tool '{call.name}' failed: {exc}", is_error=True)
            if result.is_error:
                span.set_status(Status(StatusCode.ERROR, result.content[:200]))
            return result
