from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator

import pytest

from platform_core.catalog import Events
from platform_core.security import sign_payload
from serverless_functions import scheduler, stripe_webhook, telemetry_beacon
from serverless_functions.common import CollectingPublisher, secret_setting, set_publisher

SECRET = "whsec_test_secret"


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectingPublisher]:
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    secret_setting.cache_clear()
    publisher = CollectingPublisher()
    set_publisher(publisher)
    yield publisher
    set_publisher(None)


def stripe_request(
    payload: dict[str, object],
    *,
    secret: str = SECRET,
    timestamp: int | None = None,
    base64_body: bool = False,
) -> dict[str, object]:
    body = json.dumps(payload).encode()
    signature = sign_payload(secret, body, timestamp or int(time.time()))
    return {
        "version": "2.0",
        "routeKey": "POST /webhooks/stripe",
        "headers": {"Stripe-Signature": signature, "content-type": "application/json"},
        "body": base64.b64encode(body).decode() if base64_body else body.decode(),
        "isBase64Encoded": base64_body,
    }


SUBSCRIPTION = {
    "id": "evt_1Pro",
    "type": "customer.subscription.updated",
    "data": {
        "object": {
            "id": "sub_123",
            "customer": "cus_42",
            "status": "active",
            "quantity": 5,
            "metadata": {"tenant_id": "ten_acme"},
            "items": {"data": [{"price": {"lookup_key": "pro_monthly"}}]},
        }
    },
}


def test_verified_subscription_update_is_published(published: CollectingPublisher) -> None:
    response = stripe_webhook.handler(stripe_request(SUBSCRIPTION, base64_body=True))
    assert response["statusCode"] == 200
    [event] = published.events
    assert event.type == Events.PROVIDER_SUBSCRIPTION_UPDATED
    assert event.id == "stripe:evt_1Pro"  # Stripe retries de-duplicate downstream
    assert event.tenantid == "ten_acme"
    assert event.data == {
        "provider": "stripe",
        "customer_id": "cus_42",
        "subscription_id": "sub_123",
        "status": "active",
        "plan": "pro",
        "seats": 5,
    }


def test_deleted_subscription_means_canceled(published: CollectingPublisher) -> None:
    deleted = {**SUBSCRIPTION, "id": "evt_del", "type": "customer.subscription.deleted"}
    stripe_webhook.handler(stripe_request(deleted))
    assert published.events[0].data["status"] == "canceled"


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"secret": "whsec_attacker"},
        {"timestamp": int(time.time()) - 3600},  # replayed an hour later
    ],
)
def test_forged_or_replayed_webhooks_are_rejected(
    published: CollectingPublisher, request_kwargs: dict[str, object]
) -> None:
    response = stripe_webhook.handler(stripe_request(SUBSCRIPTION, **request_kwargs))  # type: ignore[arg-type]
    assert response["statusCode"] == 400
    assert published.events == []


def test_unrelated_events_are_acknowledged_but_ignored(published: CollectingPublisher) -> None:
    response = stripe_webhook.handler(
        stripe_request({"id": "evt_x", "type": "charge.refunded", "data": {"object": {}}})
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["published"] is False


def test_beacon_sanitises_and_publishes(published: CollectingPublisher) -> None:
    body = {
        "events": [
            {
                "id": "e1",
                "name": "page_viewed",
                "anonymous_id": "anon_1",
                "properties": {"path": "/pricing", "utm_source": "newsletter", "email": "a@b.co"},
            },
            {"name": "Not Valid", "anonymous_id": "anon_1"},
            {"name": "page_viewed"},  # no anonymous id
        ]
    }
    response = telemetry_beacon.handler(
        {"body": json.dumps(body), "requestContext": {"http": {"method": "POST"}}}
    )
    assert response["statusCode"] == 202
    assert json.loads(response["body"]) == {"accepted": 1, "rejected": 2}
    assert response["headers"]["access-control-allow-origin"] == "*"
    [event] = published.events
    [clean] = event.data["events"]
    assert clean["properties"] == {"path": "/pricing", "utm_source": "newsletter"}  # no PII


def test_beacon_rejects_oversized_or_malformed_payloads(published: CollectingPublisher) -> None:
    too_big = {"body": "x" * (telemetry_beacon.MAX_BYTES + 1)}
    assert telemetry_beacon.handler(too_big)["statusCode"] == 413
    assert telemetry_beacon.handler({"body": "not json"})["statusCode"] == 400
    assert telemetry_beacon.handler({"body": json.dumps({"events": []})})["statusCode"] == 400
    preflight = {"requestContext": {"http": {"method": "OPTIONS"}}}
    assert telemetry_beacon.handler(preflight)["statusCode"] == 204
    assert published.events == []


def test_scheduler_publishes_one_event_per_tick(published: CollectingPublisher) -> None:
    tick = {"id": "eb-123", "detail-type": "Scheduled Event", "detail": {"job": "pql_scoring"}}
    result = scheduler.handler(tick)
    assert result["event_id"] == "schedule:pql_scoring:eb-123"
    assert published.events[0].type == Events.SCHEDULED_JOB_REQUESTED
    with pytest.raises(ValueError):
        scheduler.handler({"detail": {"job": "rm_rf"}})


def test_secrets_come_from_env_or_secrets_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_setting.cache_clear()
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL_SECRET_ARN", raising=False)
    with pytest.raises(KeyError):
        secret_setting("REDIS_URL")
    monkeypatch.setenv("REDIS_URL", "redis://local:6379/0")
    secret_setting.cache_clear()
    assert secret_setting("REDIS_URL") == "redis://local:6379/0"
    secret_setting.cache_clear()
