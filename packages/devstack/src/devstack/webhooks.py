"""An in-memory webhook receiver used by the demo and the end-to-end tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from platform_core.clock import Clock
from platform_core.security import SignatureInvalid, verify_payload_signature


@dataclass(frozen=True)
class ReceivedWebhook:
    url: str
    headers: dict[str, str]
    body: bytes

    @property
    def event(self) -> dict[str, Any]:
        payload = json.loads(self.body)
        assert isinstance(payload, dict)
        return payload


class WebhookReceiver:
    """Plays the customer's server: records deliveries, answers with scripted statuses."""

    def __init__(self, clock: Clock, statuses: list[int] | None = None) -> None:
        self._clock = clock
        self._statuses: Iterator[int] = iter(statuses or [])
        self.received: list[ReceivedWebhook] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.received.append(
            ReceivedWebhook(str(request.url), dict(request.headers), request.content)
        )
        return httpx.Response(next(self._statuses, 200))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def verify(self, webhook: ReceivedWebhook, secret: str) -> bool:
        """What a customer's endpoint must do before trusting a delivery."""
        try:
            verify_payload_signature(
                webhook.headers["x-webhook-signature"], secret, webhook.body, now=self._clock.time()
            )
        except (SignatureInvalid, KeyError):
            return False
        return True
