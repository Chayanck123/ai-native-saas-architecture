"""Serve the Lambda handlers over plain HTTP, the way API Gateway invokes them.

Used by Docker Compose (publishing to Redis) and by the devstack (publishing to
the in-memory bus). In AWS, API Gateway and EventBridge call the handlers
directly and this module is not deployed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response

from platform_core.events import Event
from serverless_functions import scheduler, stripe_webhook, telemetry_beacon
from serverless_functions.common import CollectingPublisher, get_publisher, set_publisher

LambdaHandler = Callable[[dict[str, Any], Any], dict[str, Any]]
AsyncPublish = Callable[[Event], Awaitable[None]]


def create_app(publish: AsyncPublish | None = None) -> FastAPI:
    """``publish=None`` publishes through the configured (Redis) publisher."""
    app = FastAPI(title="Lambda emulator", docs_url=None, redoc_url=None, openapi_url=None)

    async def invoke(handler: LambdaHandler, event: dict[str, Any]) -> dict[str, Any]:
        if publish is None:
            return handler(event, None)
        collected = CollectingPublisher()
        set_publisher(collected)
        try:
            result = handler(event, None)
        finally:
            set_publisher(None)
        for published in collected.events:
            await publish(published)
        return result

    async def api_gateway_event(request: Request) -> dict[str, Any]:
        return {
            "version": "2.0",
            "rawPath": request.url.path,
            "headers": dict(request.headers),
            "body": (await request.body()).decode(),
            "isBase64Encoded": False,
            "requestContext": {"http": {"method": request.method}},
        }

    def as_response(result: dict[str, Any]) -> Response:
        return Response(
            result.get("body", ""),
            status_code=int(result["statusCode"]),
            headers=result.get("headers") or {},
        )

    @app.post("/webhooks/stripe")
    async def stripe(request: Request) -> Response:
        return as_response(await invoke(stripe_webhook.handler, await api_gateway_event(request)))

    @app.api_route("/t", methods=["POST", "OPTIONS"])
    async def beacon(request: Request) -> Response:
        return as_response(await invoke(telemetry_beacon.handler, await api_gateway_event(request)))

    @app.post("/_dev/scheduler/{job}")
    async def schedule(job: str) -> Response:
        result = await invoke(scheduler.handler, {"detail": {"job": job}})
        return Response(json.dumps(result), media_type="application/json")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "functions"}

    return app


def main() -> None:
    get_publisher()  # fail fast if REDIS_URL is missing
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "9000")))  # noqa: S104


if __name__ == "__main__":
    main()
