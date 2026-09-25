"""Lambda: receive Stripe webhooks, verify them, and publish billing events.

API Gateway (HTTP API) ``POST /webhooks/stripe`` -> this handler -> event bus ->
billing service. The Stripe event id becomes the CloudEvent id, so Stripe's own
retries (it redelivers until it gets a 2xx) are de-duplicated downstream.
"""

from __future__ import annotations

import json
from typing import Any

from platform_core.catalog import Events
from platform_core.events import Event
from platform_core.security import SignatureInvalid, verify_payload_signature
from serverless_functions.common import (
    ApiGatewayEvent,
    ApiGatewayResponse,
    get_publisher,
    header,
    request_body,
    respond,
    secret_setting,
)

SOURCE = "/functions/stripe-webhook"
SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}


def _plan(subscription: dict[str, Any]) -> str | None:
    """Our plan key, from the subscription metadata or the price's lookup key."""
    metadata = subscription.get("metadata") or {}
    if metadata.get("plan"):
        return str(metadata["plan"])
    for item in (subscription.get("items") or {}).get("data", []):
        lookup_key = (item.get("price") or {}).get("lookup_key") or ""
        if lookup_key:
            return str(lookup_key).split("_")[0]  # e.g. "pro_monthly" -> "pro"
    return None


def translate(stripe_event: dict[str, Any]) -> Event | None:
    kind = stripe_event.get("type", "")
    obj = (stripe_event.get("data") or {}).get("object") or {}
    tenant_id = (obj.get("metadata") or {}).get("tenant_id")
    if not tenant_id:
        return None  # not created by our platform (e.g. manual objects in the dashboard)
    event_id = f"stripe:{stripe_event['id']}"
    if kind in SUBSCRIPTION_EVENTS:
        status = "canceled" if kind.endswith(".deleted") else obj.get("status", "active")
        data = {
            "provider": "stripe",
            "customer_id": obj.get("customer"),
            "subscription_id": obj.get("id"),
            "status": status,
            "plan": _plan(obj),
            "seats": obj.get("quantity"),
        }
        return Event.create(
            Events.PROVIDER_SUBSCRIPTION_UPDATED,
            source=SOURCE,
            data=data,
            tenant_id=tenant_id,
            event_id=event_id,
            subject=obj.get("id"),
        )
    if kind == "invoice.paid":
        return Event.create(
            Events.PROVIDER_INVOICE_PAID,
            source=SOURCE,
            data={
                "invoice_id": obj.get("id"),
                "amount_paid_cents": obj.get("amount_paid", 0),
                "currency": obj.get("currency"),
            },
            tenant_id=tenant_id,
            event_id=event_id,
        )
    if kind == "invoice.payment_failed":
        return Event.create(
            Events.PROVIDER_PAYMENT_FAILED,
            source=SOURCE,
            data={"invoice_id": obj.get("id"), "attempt_count": obj.get("attempt_count", 0)},
            tenant_id=tenant_id,
            event_id=event_id,
        )
    return None


def handler(event: ApiGatewayEvent, context: Any = None) -> ApiGatewayResponse:
    body = request_body(event)
    signature = header(event, "stripe-signature")
    try:
        secret = secret_setting("STRIPE_WEBHOOK_SECRET")
    except KeyError:
        secret = ""
    if not signature or not secret:
        return respond(400, {"error": "missing signature"})
    try:
        verify_payload_signature(signature, secret, body, tolerance_seconds=300)
    except SignatureInvalid as exc:
        return respond(400, {"error": f"invalid signature: {exc}"})
    try:
        stripe_event = json.loads(body)
    except json.JSONDecodeError:
        return respond(400, {"error": "invalid JSON"})
    translated = translate(stripe_event)
    if translated is not None:
        get_publisher().publish(translated)
    # Always 2xx for verified events, even ignored ones, so Stripe stops retrying.
    return respond(200, {"received": True, "published": translated is not None})
