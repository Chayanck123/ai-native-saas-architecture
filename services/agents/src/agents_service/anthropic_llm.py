"""The Anthropic API via its official Python SDK.

Request defaults follow Anthropic's current guidance:

* the model named by ``AGENTS_MODEL`` with **adaptive thinking** - the model
  decides how much to think - and ``output_config.effort`` as the cost/quality dial;
* **server-side refusal fallbacks** (``fallbacks="default"``, beta
  ``server-side-fallback-2026-07-01``): if a safety classifier declines a request,
  the API re-runs it on Anthropic's recommended fallback model in the same call;
* **automatic prompt caching** (top-level ``cache_control``), which makes the
  repeated system prompt + tool definitions of an agent loop much cheaper;
* **structured outputs** (``output_config.format``) when a JSON schema is given;
* ``stop_reason`` is checked for ``"refusal"`` before content is read.

SDK errors are mapped onto the platform's retry vocabulary: 429/5xx/connection
problems are retryable (the task queue backs off), other 4xx are permanent.
"""

from __future__ import annotations

from typing import Any

import anthropic

from agents_service.llm import LLMResponse, LLMUsage, Message, ModelRefusal, ToolCall, ToolSpec
from platform_core.retry import PermanentError, RetryableError

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    def __init__(
        self,
        *,
        model: str,
        effort: str = "high",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 300.0,
        max_retries: int = 2,
        http_client: Any = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "anthropic"

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
        output_config: dict[str, Any] = {"effort": self._effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            "cache_control": {"type": "ephemeral"},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if tools:
            request["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
            if disable_tools:
                # History may hold tool_use blocks, so tools stay declared; "none" forbids calls.
                request["tool_choice"] = {"type": "none"}
        try:
            response = await self._client.beta.messages.create(**request)
        except anthropic.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after")
            raise RetryableError(
                "Anthropic API rate limit reached",
                retry_after=float(retry_after) if retry_after else None,
            ) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:  # includes 529 overloaded
                raise RetryableError(f"Anthropic API error {exc.status_code}") from exc
            raise PermanentError(
                f"the Anthropic API rejected the request ({exc.status_code}): {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:  # also covers timeouts
            raise RetryableError(f"cannot reach the Anthropic API: {exc}") from exc

        if response.stop_reason == "refusal":
            details = response.stop_details
            raise ModelRefusal(
                getattr(details, "category", None), getattr(details, "explanation", None)
            )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        usage = response.usage
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=str(response.stop_reason),
            usage=LLMUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            ),
            model=response.model,
            content=[
                block.model_dump(mode="json", exclude_none=True) for block in response.content
            ],
        )
