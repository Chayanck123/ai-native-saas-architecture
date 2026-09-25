"""The Anthropic API adapter, exercised against a mocked HTTP transport (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from agents_service.anthropic_llm import FALLBACK_BETA, AnthropicLLM
from agents_service.app import make_llm
from agents_service.llm import ModelRefusal, ToolSpec
from agents_service.settings import AgentsSettings
from platform_core.config import ConfigurationError, CoreSettings
from platform_core.retry import PermanentError, RetryableError

SEARCH = ToolSpec("search_knowledge_base", "Search", {"type": "object", "properties": {}})


def message(
    content: list[dict[str, Any]], stop_reason: str = "end_turn", **extra: Any
) -> dict[str, Any]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "test-model",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 0,
        },
        **extra,
    }


def llm_with(handler: Callable[[httpx2.Request], httpx2.Response]) -> AnthropicLLM:
    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        base_url="https://api.anthropic.test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicLLM(model="test-model", effort="medium", client=client)


async def test_request_uses_current_best_practice_defaults() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(
            200,
            json=message(
                [
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": "Let me search."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "search_knowledge_base",
                        "input": {"query": "refund"},
                    },
                ],
                stop_reason="tool_use",
            ),
        )

    response = await llm_with(handler).complete(
        system="You are helpful.",
        messages=[{"role": "user", "content": "Refunds?"}],
        tools=[SEARCH],
    )
    body = seen["body"]
    assert body["model"] == "test-model"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    assert body["fallbacks"] == "default"
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][0]["name"] == "search_knowledge_base"
    assert FALLBACK_BETA in seen["beta"]

    assert response.stop_reason == "tool_use"
    assert response.text == "Let me search."
    assert response.tool_calls[0].input == {"query": "refund"}
    assert response.usage.input_tokens == 120
    assert response.usage.cache_read_input_tokens == 50
    # The raw blocks (thinking signature included) are preserved for the next turn.
    assert response.content[0] == {"type": "thinking", "thinking": "", "signature": "sig"}


async def test_structured_output_and_tools_disabled() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(json.loads(request.content))
        return httpx2.Response(200, json=message([{"type": "text", "text": '{"steps": ["a"]}'}]))

    llm = llm_with(handler)
    schema = {"type": "object", "properties": {"steps": {"type": "array"}}}
    response = await llm.complete(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        json_schema=schema,
        tools=[SEARCH],
        disable_tools=True,
    )
    assert seen["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert seen["tool_choice"] == {"type": "none"}
    assert json.loads(response.text) == {"steps": ["a"]}


async def test_refusals_are_surfaced_before_reading_content() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json=message(
                [],
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
            ),
        )

    with pytest.raises(ModelRefusal) as refusal:
        await llm_with(handler).complete(system="s", messages=[{"role": "user", "content": "q"}])
    assert refusal.value.category == "cyber"


@pytest.mark.parametrize(
    ("status", "headers", "error", "retry_after"),
    [
        (429, {"retry-after": "7"}, RetryableError, 7.0),
        (529, {}, RetryableError, None),
        (500, {}, RetryableError, None),
        (400, {}, PermanentError, None),
    ],
)
async def test_api_errors_map_to_retry_semantics(
    status: int, headers: dict[str, str], error: type[Exception], retry_after: float | None
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status,
            headers=headers,
            json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        )

    with pytest.raises(error) as excinfo:
        await llm_with(handler).complete(system="s", messages=[{"role": "user", "content": "q"}])
    if isinstance(excinfo.value, RetryableError):
        assert excinfo.value.retry_after == retry_after


async def test_connection_errors_are_retryable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("network unreachable")

    with pytest.raises(RetryableError):
        await llm_with(handler).complete(system="s", messages=[{"role": "user", "content": "q"}])


def test_the_anthropic_provider_requires_an_explicit_model() -> None:
    core = CoreSettings(service_name="agents", database_url="sqlite+aiosqlite://")
    with pytest.raises(ConfigurationError, match="AGENTS_MODEL"):
        make_llm(AgentsSettings(core=core, llm_provider="anthropic", anthropic_api_key="key"))
    configured = AgentsSettings(
        core=core, llm_provider="anthropic", model="test-model", anthropic_api_key="key"
    )
    assert isinstance(make_llm(configured), AnthropicLLM)
