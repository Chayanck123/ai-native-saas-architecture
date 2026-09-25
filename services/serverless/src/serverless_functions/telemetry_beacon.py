"""Lambda: public telemetry beacon for the marketing site and web app.

Browsers call ``POST /t`` (often via ``navigator.sendBeacon`` on page unload),
with anonymous visitor ids and UTM parameters - the top of the acquisition
funnel. The function validates and caps the batch, drops fields that could carry
personal data, and publishes it to the event bus for the growth service.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from platform_core.catalog import Events
from platform_core.events import Event
from serverless_functions.common import (
    ApiGatewayEvent,
    ApiGatewayResponse,
    get_publisher,
    request_body,
    respond,
)

SOURCE = "/functions/telemetry-beacon"
MAX_BYTES = 64 * 1024
MAX_EVENTS = 50
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ALLOWED_PROPERTIES = {
    "path",
    "title",
    "referrer",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "variant",
    "experiment",
    "value",
    "plan",
}


def _cors() -> dict[str, str]:
    return {
        "access-control-allow-origin": os.environ.get("BEACON_ALLOWED_ORIGIN", "*"),
        "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers": "content-type",
    }


def sanitize(item: dict[str, Any], received_at: datetime) -> dict[str, Any] | None:
    name = str(item.get("name", ""))
    anonymous_id = str(item.get("anonymous_id", ""))[:64]
    if not _NAME.match(name) or not anonymous_id:
        return None
    properties = {
        k: str(v)[:500]
        for k, v in (item.get("properties") or {}).items()
        if k in _ALLOWED_PROPERTIES
    }
    occurred_at = received_at
    try:
        if item.get("occurred_at"):
            occurred_at = min(datetime.fromisoformat(str(item["occurred_at"])), received_at)
    except (TypeError, ValueError):
        pass
    event_id = str(item.get("id") or "")[:64] or None
    return {
        "id": event_id,
        "name": name,
        "anonymous_id": anonymous_id,
        "occurred_at": occurred_at.isoformat(),
        "properties": properties,
    }


def handler(event: ApiGatewayEvent, context: Any = None) -> ApiGatewayResponse:
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "POST")
    if method == "OPTIONS":
        return respond(204, {}, _cors())
    body = request_body(event)
    if len(body) > MAX_BYTES:
        return respond(413, {"error": "payload too large"}, _cors())
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "invalid JSON"}, _cors())
    items = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or len(items) > MAX_EVENTS:
        return respond(400, {"error": f"send 1-{MAX_EVENTS} events"}, _cors())
    received_at = datetime.now(UTC)
    clean = [e for e in (sanitize(i, received_at) for i in items if isinstance(i, dict)) if e]
    if clean:
        get_publisher().publish(
            Event.create(
                Events.TELEMETRY_BATCH_RECEIVED,
                source=SOURCE,
                data={"events": clean},
            )
        )
    return respond(202, {"accepted": len(clean), "rejected": len(items) - len(clean)}, _cors())
