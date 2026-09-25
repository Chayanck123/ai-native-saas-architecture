"""Outbound webhooks: API-first integrations push, not just pull.

Tenants register HTTPS endpoints for the events they care about. Each delivery:

* is a CloudEvent (``application/cloudevents+json``);
* is signed: ``X-Webhook-Signature: t=<unix>,v1=<hmac-sha256(secret, "t.body")>`` so
  receivers can verify authenticity and reject replays;
* carries ``X-Webhook-Id`` so receivers can de-duplicate;
* runs as an idempotent background task with exponential backoff; endpoints that
  keep failing are automatically disabled so they stop consuming worker capacity.

Registration rejects URLs pointing at private networks (SSRF protection) unless
explicitly allowed for local development.
"""

from __future__ import annotations

import ipaddress
import secrets
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from gateway_service.models import WebhookDelivery, WebhookEndpoint
from gateway_service.settings import GatewaySettings
from platform_core.catalog import WEBHOOK_EVENT_TYPES
from platform_core.context import require_tenant_id
from platform_core.errors import NotFound, ValidationFailed
from platform_core.events import Event
from platform_core.ids import new_id
from platform_core.retry import PermanentError, RetryableError
from platform_core.security import sign_payload
from platform_core.service import ServiceRuntime

_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}


def validate_webhook_url(url: str, *, allow_private: bool) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ({"https", "http"} if allow_private else {"https"}):
        raise ValidationFailed("webhook URLs must use https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValidationFailed("webhook URL has no host")
    if allow_private:
        return url
    if host in _BLOCKED_HOSTS or host.endswith((".local", ".internal", ".localhost")):
        raise ValidationFailed("webhook URL points at a private host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url  # a DNS name; production also pins the resolved IP at delivery time
    if not address.is_global:
        raise ValidationFailed("webhook URL points at a private network address")
    return url


class WebhookService:
    def __init__(
        self,
        runtime: ServiceRuntime,
        settings: GatewaySettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.runtime = runtime
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.webhook_timeout_seconds, transport=transport, follow_redirects=False
        )

    async def create_endpoint(self, url: str, event_types: list[str]) -> dict[str, Any]:
        tenant_id = require_tenant_id()
        validate_webhook_url(url, allow_private=self.settings.webhook_allow_private_urls)
        unknown = set(event_types) - set(WEBHOOK_EVENT_TYPES)
        if unknown or not event_types:
            raise ValidationFailed(
                "unsupported event types",
                unsupported=sorted(unknown),
                supported=list(WEBHOOK_EVENT_TYPES),
            )
        secret = f"whsec_{secrets.token_urlsafe(32)}"
        endpoint = WebhookEndpoint(
            id=new_id("whk"),
            tenant_id=tenant_id,
            url=url,
            secret=secret,
            event_types=sorted(set(event_types)),
            active=True,
            consecutive_failures=0,
            created_at=self.runtime.clock.now(),
        )
        async with self.runtime.db.session() as session:
            session.add(endpoint)
        # The signing secret is only ever shown once.
        return {**self._out(endpoint), "secret": secret}

    async def list_endpoints(self) -> list[dict[str, Any]]:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            rows = (
                await session.execute(
                    select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == tenant_id)
                )
            ).scalars()
            return [self._out(e) for e in rows]

    async def delete_endpoint(self, endpoint_id: str) -> None:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            endpoint = (
                await session.execute(
                    select(WebhookEndpoint).where(
                        WebhookEndpoint.tenant_id == tenant_id, WebhookEndpoint.id == endpoint_id
                    )
                )
            ).scalar_one_or_none()
            if endpoint is None:
                raise NotFound(f"webhook '{endpoint_id}' not found")
            await session.delete(endpoint)

    async def list_deliveries(self, endpoint_id: str, limit: int = 50) -> list[dict[str, Any]]:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            rows = (
                await session.execute(
                    select(WebhookDelivery)
                    .where(
                        WebhookDelivery.tenant_id == tenant_id,
                        WebhookDelivery.endpoint_id == endpoint_id,
                    )
                    .order_by(WebhookDelivery.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
            return [
                {
                    "id": d.id,
                    "event_id": d.event_id,
                    "event_type": d.event_type,
                    "status": d.status,
                    "attempts": d.attempts,
                    "last_status_code": d.last_status_code,
                    "last_error": d.last_error,
                    "created_at": d.created_at.isoformat(),
                    "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
                }
                for d in rows
            ]

    @staticmethod
    def _out(endpoint: WebhookEndpoint) -> dict[str, Any]:
        return {
            "id": endpoint.id,
            "url": endpoint.url,
            "event_types": endpoint.event_types,
            "active": endpoint.active,
            "created_at": endpoint.created_at.isoformat(),
        }

    # ------------------------------------------------------------ delivery
    async def fan_out(self, event: Event) -> dict[str, Any]:
        """Create one delivery per subscribed endpoint and enqueue it (idempotent)."""
        tenant_id = require_tenant_id()
        created = []
        async with self.runtime.db.session() as session:
            endpoints = (
                (
                    await session.execute(
                        select(WebhookEndpoint).where(
                            WebhookEndpoint.tenant_id == tenant_id, WebhookEndpoint.active.is_(True)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for endpoint in endpoints:
                if event.type not in endpoint.event_types:
                    continue
                existing = (
                    await session.execute(
                        select(WebhookDelivery.id).where(
                            WebhookDelivery.endpoint_id == endpoint.id,
                            WebhookDelivery.event_id == event.id,
                        )
                    )
                ).first()
                if existing is not None:
                    continue
                delivery = WebhookDelivery(
                    id=new_id("dlv"),
                    tenant_id=tenant_id,
                    endpoint_id=endpoint.id,
                    event_id=event.id,
                    event_type=event.type,
                    payload=event.model_dump_json(),
                    status="pending",
                    attempts=0,
                    created_at=self.runtime.clock.now(),
                )
                session.add(delivery)
                created.append(delivery.id)
        for delivery_id in created:
            await self.runtime.enqueue(
                "gateway.deliver_webhook", {"delivery_id": delivery_id}, idempotency_key=delivery_id
            )
        return {"deliveries": len(created)}

    async def deliver(self, delivery_id: str) -> dict[str, Any]:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            delivery = (
                await session.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.tenant_id == tenant_id, WebhookDelivery.id == delivery_id
                    )
                )
            ).scalar_one_or_none()
            if delivery is None or delivery.status == "succeeded":
                return {"skipped": True}
            endpoint = await session.get(WebhookEndpoint, delivery.endpoint_id)
            if endpoint is None or not endpoint.active:
                delivery.status = "failed"
                delivery.last_error = "endpoint disabled or deleted"
                return {"skipped": True}
            delivery.attempts += 1
            url, secret, payload = endpoint.url, endpoint.secret, delivery.payload

        body = payload.encode()
        headers = {
            "content-type": "application/cloudevents+json",
            "user-agent": "ainative-webhooks/1.0",
            "x-webhook-id": delivery_id,
            "x-webhook-signature": sign_payload(secret, body, int(self.runtime.clock.time())),
        }
        error: str | None = None
        status_code: int | None = None
        try:
            response = await self._client.post(url, content=body, headers=headers)
            status_code = response.status_code
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"

        succeeded = status_code is not None and 200 <= status_code < 300
        async with self.runtime.db.session() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            endpoint = (
                await session.get(WebhookEndpoint, delivery.endpoint_id) if delivery else None
            )
            assert delivery is not None and endpoint is not None
            delivery.last_status_code = status_code
            delivery.last_error = error
            if succeeded:
                delivery.status = "succeeded"
                delivery.delivered_at = self.runtime.clock.now()
                endpoint.consecutive_failures = 0
            else:
                endpoint.consecutive_failures += 1
                if endpoint.consecutive_failures >= self.settings.webhook_disable_after_failures:
                    endpoint.active = False
                    endpoint.disabled_at = self.runtime.clock.now()
        if succeeded:
            return {"status_code": status_code}
        if status_code is not None and 400 <= status_code < 500 and status_code not in (408, 429):
            await self.mark_failed(delivery_id)
            raise PermanentError(f"receiver rejected the webhook with HTTP {status_code}")
        raise RetryableError(error or f"receiver returned HTTP {status_code}")

    async def mark_failed(self, delivery_id: str) -> None:
        async with self.runtime.db.session() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            if delivery is not None:
                delivery.status = "failed"

    async def aclose(self) -> None:
        await self._client.aclose()
