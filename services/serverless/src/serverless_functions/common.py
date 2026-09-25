"""Shared plumbing for the AWS Lambda functions.

Why serverless for these three? Each is spiky and short-lived: payment-provider
webhooks arrive in bursts, marketing-site beacons follow traffic peaks, and
scheduled jobs run for seconds a day. Lambda scales them from zero to thousands
of concurrent executions and costs nothing while idle. The functions stay thin:
validate, translate into a CloudEvent, publish to the event bus - the heavy
lifting happens in the containerised services.
"""

from __future__ import annotations

import base64
import functools
import importlib
import json
import os
from typing import Any, Protocol

from redis import Redis

from platform_core.events import Event

ApiGatewayEvent = dict[str, Any]
ApiGatewayResponse = dict[str, Any]


class Publisher(Protocol):
    def publish(self, event: Event) -> None: ...


class RedisStreamPublisher:
    """Synchronous ``XADD`` onto the platform event stream (Lambda runs sync code)."""

    def __init__(self, redis: Redis, stream: str, maxlen: int = 100_000) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen

    def publish(self, event: Event) -> None:
        self._redis.xadd(
            self._stream, {"event": event.model_dump_json()}, maxlen=self._maxlen, approximate=True
        )


class CollectingPublisher:
    """Keeps events in memory (tests and the single-process devstack)."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


_publisher: Publisher | None = None


@functools.cache
def secret_setting(name: str) -> str:
    """A setting from the environment, or from AWS Secrets Manager.

    In AWS the function only receives ``<NAME>_SECRET_ARN``; the value is fetched
    once per container at cold start, so secrets never appear in the function's
    configuration. Locally, a plain ``<NAME>`` environment variable is used.
    """
    value = os.environ.get(name)
    if value:
        return value
    arn = os.environ.get(f"{name}_SECRET_ARN")
    if not arn:
        raise KeyError(f"set {name} or {name}_SECRET_ARN")
    boto3 = importlib.import_module("boto3")  # provided by the Lambda runtime
    payload = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    return str(json.loads(payload)[name])


def get_publisher() -> Publisher:
    """Created once per Lambda container (reused across warm invocations)."""
    global _publisher
    if _publisher is None:
        url = secret_setting("REDIS_URL")
        prefix = os.environ.get("REDIS_PREFIX", "ainative")
        _publisher = RedisStreamPublisher(Redis.from_url(url), f"{prefix}:events")
    return _publisher


def set_publisher(publisher: Publisher | None) -> None:
    global _publisher
    _publisher = publisher


def request_body(event: ApiGatewayEvent) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode() if isinstance(body, str) else bytes(body)


def header(event: ApiGatewayEvent, name: str) -> str | None:
    headers = event.get("headers") or {}
    lowered = {k.lower(): v for k, v in headers.items()}
    value = lowered.get(name.lower())
    return str(value) if value is not None else None


def respond(
    status: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> ApiGatewayResponse:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **(headers or {})},
        "body": json.dumps(body),
    }
