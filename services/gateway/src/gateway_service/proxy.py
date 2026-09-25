"""Reverse proxy from the public API to the internal services.

The gateway authenticates the caller once, then forwards the request with a
short-lived *internal* token describing the caller (tenant, role, permissions).
Internal services trust only that token, so they stay unreachable with a user's
credentials alone, and each service still enforces its own permissions.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import Request, Response
from opentelemetry.trace import SpanKind

from gateway_service.settings import ROUTES, GatewaySettings
from platform_core.context import current_request_id, require_principal
from platform_core.errors import NotFound, UpstreamUnavailable
from platform_core.ids import new_id
from platform_core.observability.tracing import inject_trace_headers
from platform_core.retry import CircuitBreaker, CircuitOpenError
from platform_core.security import INTERNAL_TOKEN_HEADER
from platform_core.service import ROUTE_OVERRIDE, ServiceRuntime

_FORWARD_REQUEST_HEADERS = ("content-type", "accept", "accept-language")
_FORWARD_RESPONSE_HEADERS = ("content-type", "location", "retry-after")


def match_route(path: str) -> tuple[str, str] | None:
    """Longest-prefix match of a public path: ``(prefix, owning service)``."""
    matches = [prefix for prefix in ROUTES if path == prefix or path.startswith(prefix + "/")]
    if not matches:
        return None
    prefix = max(matches, key=len)
    return prefix, ROUTES[prefix]


def route_for(path: str) -> str | None:
    matched = match_route(path)
    return matched[1] if matched else None


class Proxy:
    def __init__(
        self,
        runtime: ServiceRuntime,
        settings: GatewaySettings,
        transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        transports = transports or {}
        self.runtime = runtime
        self._clients = {
            name: httpx.AsyncClient(
                base_url=url,
                transport=transports.get(name),
                timeout=settings.upstream_timeout_seconds,
            )
            for name, url in settings.upstreams.items()
        }
        self._breakers = {
            name: CircuitBreaker(f"upstream:{name}", failure_threshold=10, reset_timeout=10.0)
            for name in settings.upstreams
        }
        self._openapi_cache: dict[str, Any] | None = None

    async def forward(self, request: Request) -> Response:
        principal = require_principal()
        matched = match_route(request.url.path)
        if matched is None:
            raise NotFound(f"no route for {request.url.path}")
        prefix, service = matched
        request.scope[ROUTE_OVERRIDE] = f"{prefix}/*"
        client, breaker = self._clients[service], self._breakers[service]
        headers = {
            INTERNAL_TOKEN_HEADER: self.runtime.internal_tokens.issue(principal, ttl_seconds=120),
            "x-request-id": current_request_id() or new_id("req"),
        }
        for name in _FORWARD_REQUEST_HEADERS:
            if name in request.headers:
                headers[name] = request.headers[name]
        body = await request.body()
        started = time.perf_counter()
        with self.runtime.tracer.start_as_current_span(
            f"proxy {service}",
            kind=SpanKind.CLIENT,
            attributes={"peer.service": service, "url.path": request.url.path},
        ) as span:
            inject_trace_headers(headers)
            try:
                breaker.before_call()
                upstream = await client.request(
                    request.method,
                    request.url.path,
                    params=request.query_params,
                    content=body,
                    headers=headers,
                )
            except CircuitOpenError as exc:
                raise UpstreamUnavailable(f"{service} is temporarily unavailable") from exc
            except httpx.TransportError as exc:
                breaker.record_failure()
                raise UpstreamUnavailable(f"{service} did not respond") from exc
            if upstream.status_code >= 500:
                breaker.record_failure()
            else:
                breaker.record_success()
            span.set_attribute("http.response.status_code", upstream.status_code)
            span.set_attribute(
                "upstream.duration_ms", round((time.perf_counter() - started) * 1000, 2)
            )
        response_headers = {
            name: upstream.headers[name]
            for name in _FORWARD_RESPONSE_HEADERS
            if name in upstream.headers
        }
        return Response(
            content=upstream.content, status_code=upstream.status_code, headers=response_headers
        )

    async def openapi(self, gateway_schema: dict[str, Any]) -> dict[str, Any]:
        """One OpenAPI document for the whole platform (gateway + every service)."""
        if self._openapi_cache is not None:
            return self._openapi_cache
        merged = {**gateway_schema, "paths": dict(gateway_schema.get("paths", {}))}
        components = merged.setdefault("components", {}).setdefault("schemas", {})
        complete = True
        for service, client in self._clients.items():
            try:
                schema = (await client.get("/openapi.json")).json()
            except (httpx.HTTPError, ValueError):
                complete = False
                continue
            for path, operations in schema.get("paths", {}).items():
                if route_for(path) == service:
                    merged["paths"][path] = operations
            components.update(schema.get("components", {}).get("schemas", {}))
        if complete:
            self._openapi_cache = merged
        return merged

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
