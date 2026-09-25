"""Service-to-service HTTP client with the fault-tolerance defaults baked in.

* Mints a short-lived internal token *on behalf of* the current principal, so
  downstream services enforce the same tenant and permissions (zero trust).
* Propagates ``traceparent`` and ``x-request-id``.
* Timeouts on every call; retries (with jittered backoff) only for idempotent
  requests; a circuit breaker per downstream service.
* Converts downstream problem responses back into the same domain errors.
"""

from __future__ import annotations

from typing import Any

import httpx
from opentelemetry.trace import SpanKind, Tracer

from platform_core.context import Principal, current_principal, current_request_id
from platform_core.errors import (
    AuthenticationRequired,
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
    QuotaExceeded,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from platform_core.ids import new_id
from platform_core.observability.tracing import inject_trace_headers
from platform_core.retry import CircuitBreaker, RetryableError, RetryPolicy, retry_async
from platform_core.security import INTERNAL_TOKEN_HEADER, TokenService

_ERRORS: dict[int, type[DomainError]] = {
    401: AuthenticationRequired,
    402: QuotaExceeded,
    403: PermissionDenied,
    404: NotFound,
    409: Conflict,
    422: ValidationFailed,
    429: RateLimited,
}

_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}


class ServiceClient:
    def __init__(
        self,
        name: str,
        *,
        tokens: TokenService,
        base_url: str = "http://service",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        retry_policy: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.name = name
        self._tokens = tokens
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)
        self._retry = retry_policy or RetryPolicy(max_attempts=3, base_delay=0.2, max_delay=2.0)
        self._breaker = breaker or CircuitBreaker(
            f"http:{name}", failure_threshold=5, reset_timeout=15.0
        )
        self._tracer = tracer

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        principal: Principal | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        acting_as = principal or current_principal()
        if acting_as is None:
            raise AuthenticationRequired("service call without a principal")
        token = self._tokens.issue(acting_as, ttl_seconds=120)
        method = method.upper()

        async def attempt() -> httpx.Response:
            self._breaker.before_call()
            headers = {
                INTERNAL_TOKEN_HEADER: token,
                "x-request-id": current_request_id() or new_id("req"),
            }
            if idempotency_key:
                headers["idempotency-key"] = idempotency_key
            if self._tracer is None:
                return await self._send(method, path, json, params, headers)
            with self._tracer.start_as_current_span(
                f"{method} {self.name}",
                kind=SpanKind.CLIENT,
                attributes={"peer.service": self.name, "url.path": path},
            ) as span:
                response = await self._send(method, path, json, params, headers)
                span.set_attribute("http.response.status_code", response.status_code)
                return response

        policy = self._retry
        if method not in _IDEMPOTENT_METHODS and idempotency_key is None:
            policy = RetryPolicy(max_attempts=1)
        try:
            return await retry_async(attempt, policy)
        except RetryableError as exc:
            raise UpstreamUnavailable(f"{self.name} unavailable: {exc}") from exc

    async def _send(
        self,
        method: str,
        path: str,
        json: Any,
        params: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> httpx.Response:
        inject_trace_headers(headers)
        try:
            response = await self._client.request(
                method, path, json=json, params=params, headers=headers
            )
        except httpx.TransportError as exc:
            self._breaker.record_failure()
            raise RetryableError(f"transport error: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            self._breaker.record_failure()
            retry_after = response.headers.get("retry-after")
            raise RetryableError(
                f"status {response.status_code}",
                retry_after=float(retry_after) if retry_after else None,
            )
        self._breaker.record_success()
        return response

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        """Request and decode JSON, raising domain errors for 4xx responses."""
        response = await self.request(method, path, **kwargs)
        raise_for_problem(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def raise_for_problem(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = body.get("detail") or response.text or f"HTTP {response.status_code}"
    code = body.get("code")
    error_cls = _ERRORS.get(response.status_code, DomainError)
    error = error_cls(str(detail), code=code)
    error.status_code = response.status_code
    raise error
