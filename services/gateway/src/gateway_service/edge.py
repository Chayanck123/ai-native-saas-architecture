"""Edge protections applied to every public request (after authentication).

**Rate limiting** - a token bucket per tenant sized by plan (per client IP for
anonymous calls such as signup). Rejections carry ``Retry-After``; every response
carries ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining``.

**Idempotency keys** - clients may send ``Idempotency-Key`` on any POST, PUT,
PATCH or DELETE. The first request runs and its response is stored for 24 hours;
a retry with the same key and body gets the stored response back
(``Idempotent-Replayed: true``) instead of creating a second document, agent run
or charge. Reusing a key with a different body is rejected (422), and a retry
that arrives while the original is still running gets 409.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from platform_core.context import current_principal
from platform_core.errors import Conflict, DomainError, RateLimited, ValidationFailed
from platform_core.idempotency import IdempotencyState
from platform_core.ratelimit import RateLimit
from platform_core.service import ServiceRuntime, domain_error_response

PlanResolver = Callable[[str], Awaitable[str]]
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_EXEMPT = {"/healthz", "/readyz", "/metrics"}
_REPLAYED_HEADERS = {"content-type", "location"}


class EdgeMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        runtime: ServiceRuntime,
        plan_limits: dict[str, int],
        anonymous_limit: int,
        resolve_plan: PlanResolver,
    ) -> None:
        self.app = app
        self.runtime = runtime
        self.plan_limits = plan_limits
        self.anonymous_limit = anonymous_limit
        self.resolve_plan = resolve_plan

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _EXEMPT:
            await self.app(scope, receive, send)
            return
        try:
            limit_headers = await self._rate_limit(scope)
        except DomainError as exc:
            await domain_error_response(exc)(scope, receive, send)
            return

        async def send_with_limits(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in limit_headers.items():
                    headers[key] = value
            await send(message)

        headers = Headers(scope=scope)
        key = headers.get("idempotency-key")
        principal = current_principal()
        if key and scope["method"] in _MUTATING and principal is not None:
            await self._idempotent(
                scope, receive, send_with_limits, key, principal.tenant_id or principal.subject
            )
        else:
            await self.app(scope, receive, send_with_limits)

    # ---------------------------------------------------------------- limits
    async def _rate_limit(self, scope: Scope) -> dict[str, str]:
        principal = current_principal()
        if principal is not None and principal.tenant_id:
            plan = await self.resolve_plan(principal.tenant_id)
            per_minute = self.plan_limits.get(plan, self.plan_limits["free"])
            bucket, label = f"tenant:{principal.tenant_id}", plan
        else:
            client = scope.get("client") or ("unknown", 0)
            forwarded = Headers(scope=scope).get("x-forwarded-for", "")
            ip = forwarded.split(",")[0].strip() or client[0]
            per_minute, bucket, label = self.anonymous_limit, f"ip:{ip}", "anonymous"
        decision = await self.runtime.infra.rate_limiter.acquire(
            bucket, RateLimit.per_minute(per_minute)
        )
        if not decision.allowed:
            self.runtime.metrics.rate_limited.labels(self.runtime.name, label).inc()
            raise RateLimited(
                f"rate limit of {per_minute} requests/minute exceeded",
                retry_after=round(decision.retry_after, 3),
                limit=per_minute,
            )
        return {
            "x-ratelimit-limit": str(decision.limit),
            "x-ratelimit-remaining": str(decision.remaining),
        }

    # ----------------------------------------------------------- idempotency
    async def _idempotent(
        self, scope: Scope, receive: Receive, send: Send, key: str, owner_scope: str
    ) -> None:
        if len(key) > 200:
            await domain_error_response(ValidationFailed("Idempotency-Key is too long"))(
                scope, receive, send
            )
            return
        body = await _read_body(receive)
        query = scope.get("query_string", b"").decode()
        request_line = f"{scope['method']} {scope['path']}?{query}\n".encode()
        fingerprint = hashlib.sha256(request_line + body).hexdigest()
        store_key = f"http:{owner_scope}:{key}"
        store = self.runtime.infra.idempotency
        lock = await store.begin(store_key, fingerprint=fingerprint, lease_seconds=60)

        if lock.state is IdempotencyState.COMPLETED:
            if lock.fingerprint != fingerprint:
                error: DomainError = ValidationFailed(
                    "this Idempotency-Key was already used with a different request",
                    code="idempotency_key_reused",
                )
                await domain_error_response(error)(scope, receive, send)
                return
            self.runtime.metrics.idempotent_replays.labels(self.runtime.name).inc()
            await _replay(lock.response or {}, send)
            return
        if lock.state is IdempotencyState.IN_PROGRESS:
            conflict = Conflict(
                "a request with this Idempotency-Key is still being processed",
                code="idempotency_key_in_progress",
            )
            await domain_error_response(conflict)(scope, receive, send)
            return

        assert lock.owner is not None
        captured: dict[str, Any] = {"status": 500, "headers": [], "body": b""}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = [
                    (k.decode(), v.decode())
                    for k, v in message.get("headers", [])
                    if k.decode().lower() in _REPLAYED_HEADERS
                ]
            elif message["type"] == "http.response.body":
                captured["body"] += message.get("body", b"")
            await send(message)

        try:
            await self.app(scope, _replay_receive(body), capture)
        except Exception:
            await store.release(store_key, lock.owner)
            raise
        if captured["status"] >= 500:
            await store.release(store_key, lock.owner)  # let the client retry
        else:
            await store.complete(
                store_key,
                lock.owner,
                response={
                    "status": captured["status"],
                    "headers": captured["headers"],
                    "body": base64.b64encode(captured["body"]).decode(),
                },
            )


async def _read_body(receive: Receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return body


def _replay_receive(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _replay(stored: dict[str, Any], send: Send) -> None:
    headers = [(k.encode(), v.encode()) for k, v in stored.get("headers", [])]
    headers.append((b"idempotent-replayed", b"true"))
    await send(
        {
            "type": "http.response.start",
            "status": int(stored.get("status", 200)),
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": base64.b64decode(stored.get("body", ""))})
