"""Provider-neutral LLM interface used by the orchestrator.

Conversations use the Anthropic Messages format (``role`` + content blocks such
as ``text``, ``tool_use`` and ``tool_result``) as the lingua franca, so the
agent loop is identical whether it talks to the Anthropic API or to the offline model.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

from platform_core.observability.metrics import ServiceMetrics
from platform_core.retry import CircuitBreaker

Message = dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: LLMUsage
    model: str
    # Raw assistant content blocks, appended to the history unchanged (this keeps
    # thinking-block signatures and tool_use ids intact across turns).
    content: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class UsageMeter:
    """Accumulates every LLM call made while it is active (planner, actor, critic)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    calls: int = 0

    def add(self, usage: LLMUsage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.calls += 1

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )


_meter: ContextVar[UsageMeter | None] = ContextVar("llm_usage_meter", default=None)


@contextmanager
def metering() -> Iterator[UsageMeter]:
    meter = UsageMeter()
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


class ModelRefusal(Exception):
    """The model declined the request (``stop_reason == "refusal"``)."""

    def __init__(self, category: str | None, explanation: str | None = None) -> None:
        super().__init__(f"model refused the request (category={category})")
        self.category = category
        self.explanation = explanation


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def provider(self) -> str: ...

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_schema: dict[str, Any] | None = None,
        disable_tools: bool = False,
    ) -> LLMResponse: ...


class InstrumentedLLM:
    """Decorator adding tracing (OpenTelemetry GenAI conventions), metrics and a
    circuit breaker around any :class:`LLMClient`."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        tracer: Tracer,
        metrics: ServiceMetrics,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._inner = inner
        self._tracer = tracer
        self._metrics = metrics
        self._breaker = breaker or CircuitBreaker(
            f"llm:{inner.provider}", failure_threshold=5, reset_timeout=30.0
        )

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

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
        operation = "chat"
        with self._tracer.start_as_current_span(
            f"{operation} {self.model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": operation,
                "gen_ai.system": self.provider,
                "gen_ai.request.model": self.model,
                "gen_ai.request.max_tokens": max_tokens,
            },
        ) as span:
            started = time.perf_counter()
            self._breaker.before_call()
            try:
                response = await self._inner.complete(
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    json_schema=json_schema,
                    disable_tools=disable_tools,
                )
            except ModelRefusal:
                self._breaker.record_success()  # the provider is healthy; it just said no
                span.set_attribute("gen_ai.response.finish_reasons", ["refusal"])
                raise
            except Exception as exc:
                self._breaker.record_failure()
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            self._breaker.record_success()
            elapsed = time.perf_counter() - started
            span.set_attributes(
                {
                    "gen_ai.response.model": response.model,
                    "gen_ai.response.finish_reasons": [response.stop_reason],
                    "gen_ai.usage.input_tokens": response.usage.input_tokens,
                    "gen_ai.usage.output_tokens": response.usage.output_tokens,
                }
            )
            service = self._metrics.service
            self._metrics.llm_latency.labels(service, response.model).observe(elapsed)
            self._metrics.llm_tokens.labels(service, response.model, "input").inc(
                response.usage.input_tokens
            )
            self._metrics.llm_tokens.labels(service, response.model, "output").inc(
                response.usage.output_tokens
            )
            return response


class MeteredLLM:
    """Adds every response's token usage to the active :class:`UsageMeter`."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

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
        response = await self._inner.complete(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            json_schema=json_schema,
            disable_tools=disable_tools,
        )
        meter = _meter.get()
        if meter is not None:
            meter.add(response.usage)
        return response


def metered(llm: LLMClient) -> LLMClient:
    """Wrap ``llm`` so its usage is metered (idempotent: never counts a call twice)."""
    return llm if isinstance(llm, MeteredLLM) else MeteredLLM(llm)
