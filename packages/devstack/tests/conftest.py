from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from devstack.stack import DevStack, build_devstack
from devstack.webhooks import WebhookReceiver
from platform_core.clock import ManualClock
from platform_core.testing import SpanRecorder

HANDBOOK = """# Acme Cloud handbook

## Refund policy
Customers can request a full refund within 30 days of purchase. After 30 days, refunds are
prorated for annual plans only. Refunds are paid to the original payment method.

## Security
All customer data is encrypted at rest with AES-256 and in transit with TLS 1.3.
Error ERR-4521 means the SSO certificate has expired; upload a new certificate in the admin
console to fix it.
"""


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(datetime(2026, 6, 1, 9, 0, tzinfo=UTC))


@pytest.fixture
def receiver(clock: ManualClock) -> WebhookReceiver:
    return WebhookReceiver(clock)


@pytest.fixture
def spans() -> SpanRecorder:
    return SpanRecorder()


@pytest.fixture
async def stack(
    clock: ManualClock, receiver: WebhookReceiver, spans: SpanRecorder
) -> AsyncIterator[DevStack]:
    built = build_devstack(
        clock=clock, webhook_transport=receiver.transport, span_exporter=spans.exporter
    )
    await built.start()
    yield built
    await built.stop()


async def signup(
    stack: DevStack, email: str, workspace: str = "Acme", **extra: Any
) -> dict[str, Any]:
    async with stack.client() as client:
        response = await client.post(
            "/v1/auth/signup",
            json={
                "email": email,
                "password": "correct-horse-battery",
                "workspace_name": workspace,
                **extra,
            },
        )
    assert response.status_code == 201, response.text
    await stack.drain()
    body = response.json()
    assert isinstance(body, dict)
    return body


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def client_for(stack: DevStack, token: str) -> httpx.AsyncClient:
    return stack.client(**bearer(token))
