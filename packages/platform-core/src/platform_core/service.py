"""Service runtime (dependency container) and the FastAPI app factory.

Every microservice is assembled the same way::

    runtime = ServiceRuntime.create("knowledge", settings, infra, metadata=Base.metadata)
    app = create_service_app(runtime, title="Knowledge API", routers=[router])

which gives it, for free: request ids, distributed tracing, RED metrics,
structured access logs, internal-token authentication, RFC 9457 problem
responses, ``/healthz``, ``/readyz`` and ``/metrics``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from platform_core.clock import Clock
from platform_core.config import CoreSettings
from platform_core.context import (
    Principal,
    current_request_id,
    require_principal,
    use_principal,
    use_request_id,
)
from platform_core.db import Database
from platform_core.errors import DomainError, PermissionDenied, RateLimited
from platform_core.events import Event
from platform_core.ids import new_id
from platform_core.infrastructure import Infrastructure
from platform_core.observability.metrics import ServiceMetrics
from platform_core.observability.tracing import build_tracer_provider, extract_context
from platform_core.outbox import Outbox, outbox_table
from platform_core.rbac import Permission
from platform_core.security import INTERNAL_TOKEN_HEADER, TokenService, internal_token_service
from platform_core.tasks import Task

Authenticator = Callable[[Headers], Awaitable[Principal | None]]
ReadinessCheck = Callable[[], Awaitable[bool]]


class UnitOfWork:
    """A database transaction plus the events it will publish once committed."""

    def __init__(self, session: AsyncSession, outbox: Outbox) -> None:
        self.session = session
        self._outbox = outbox
        self.events: list[Event] = []

    async def publish(self, event: Event) -> Event:
        await self._outbox.add(self.session, event)
        self.events.append(event)
        return event


@dataclass
class ServiceRuntime:
    name: str
    settings: CoreSettings
    infra: Infrastructure
    db: Database
    metadata: MetaData
    tenant_tables: tuple[str, ...]
    extensions: tuple[str, ...]
    tracer_provider: TracerProvider
    metrics: ServiceMetrics
    internal_tokens: TokenService
    outbox: Outbox
    logger: logging.Logger
    readiness_checks: dict[str, ReadinessCheck] = field(default_factory=dict)
    _started: bool = False

    @classmethod
    def create(
        cls,
        name: str,
        settings: CoreSettings,
        infra: Infrastructure,
        *,
        metadata: MetaData,
        tenant_tables: Iterable[str] = (),
        extensions: Iterable[str] = (),
        span_exporter: SpanExporter | None = None,
    ) -> ServiceRuntime:
        db = Database(settings.database_url)
        if "event_outbox" not in metadata.tables:
            outbox_table(metadata)
        runtime = cls(
            name=name,
            settings=settings,
            infra=infra,
            db=db,
            metadata=metadata,
            tenant_tables=tuple(tenant_tables),
            extensions=tuple(extensions),
            tracer_provider=build_tracer_provider(
                name,
                environment=settings.environment,
                otlp_endpoint=settings.otlp_endpoint,
                exporter=span_exporter,
            ),
            metrics=ServiceMetrics(name),
            internal_tokens=internal_token_service(settings.internal_jwt_secret),
            outbox=Outbox(name, db, metadata.tables["event_outbox"], infra.event_bus, infra.clock),
            logger=logging.getLogger(f"{name}_service"),
        )
        runtime.readiness_checks["database"] = db.ping
        runtime.readiness_checks["infrastructure"] = infra.ping
        return runtime

    @property
    def tracer(self) -> Tracer:
        return self.tracer_provider.get_tracer(f"{self.name}_service")

    @property
    def clock(self) -> Clock:
        return self.infra.clock

    async def start(self) -> None:
        if self._started:
            return
        await self.db.create_all(
            self.metadata, tenant_tables=self.tenant_tables, extensions=self.extensions
        )
        self._started = True

    async def stop(self) -> None:
        self.tracer_provider.force_flush()
        await self.db.dispose()
        self._started = False

    # -- persistence + events --------------------------------------------------
    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[UnitOfWork]:
        async with self.db.session() as session:
            uow = UnitOfWork(session, self.outbox)
            yield uow
        # Committed: publish right away (the relay loop covers crashes).
        if uow.events:
            try:
                await self.outbox.relay(event.id for event in uow.events)
            except Exception:
                self.logger.exception("outbox publish failed; relay will retry")
            for event in uow.events:
                self.metrics.events_published.labels(self.name, event.type).inc()

    def event(
        self,
        type: str,
        data: dict[str, Any] | None = None,
        *,
        subject: str | None = None,
        tenant_id: str | None = None,
        event_id: str | None = None,
    ) -> Event:
        return Event.create(
            type,
            source=f"/services/{self.name}",
            data=data,
            subject=subject,
            tenant_id=tenant_id,
            event_id=event_id,
            time=self.clock.now(),
        )

    async def publish(self, event: Event) -> Event:
        """Publish an event that is *not* tied to a database write."""
        await self.infra.event_bus.publish(event)
        self.metrics.events_published.labels(self.name, event.type).inc()
        return event

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        queue: str | None = None,
        tenant_id: str | None = None,
        idempotency_key: str | None = None,
        delay_seconds: float = 0.0,
    ) -> Task:
        from platform_core.context import current_tenant_id
        from platform_core.observability.tracing import current_traceparent

        task = Task(
            name=name,
            queue=queue or self.name,
            payload=payload or {},
            tenant_id=tenant_id if tenant_id is not None else current_tenant_id(),
            idempotency_key=idempotency_key,
            traceparent=current_traceparent(),
            created_at=self.clock.now(),
        )
        await self.infra.task_queue.enqueue(task, delay_seconds=delay_seconds)
        return task


# ---------------------------------------------------------------------------- #
# Problem responses (RFC 9457)
# ---------------------------------------------------------------------------- #
def problem(
    status: int, code: str, detail: str, *, headers: dict[str, str] | None = None, **extra: Any
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://errors.ainative.dev/{code}",
        "title": code.replace("_", " ").capitalize(),
        "status": status,
        "code": code,
        "detail": detail,
    }
    request_id = current_request_id()
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return JSONResponse(
        body, status_code=status, headers=headers, media_type="application/problem+json"
    )


def domain_error_response(exc: DomainError) -> JSONResponse:
    headers = None
    if isinstance(exc, RateLimited) and "retry_after" in exc.details:
        headers = {"Retry-After": str(max(1, int(float(exc.details["retry_after"]) + 0.999)))}
    return problem(exc.status_code, exc.code, exc.message, headers=headers, **exc.details)


def install_error_handlers(app: FastAPI) -> None:
    async def on_domain_error(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, DomainError)
        return domain_error_response(exc)

    async def on_validation_error(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        errors = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return problem(422, "validation_failed", "request validation failed", errors=errors)

    async def on_http_error(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        return problem(exc.status_code, "http_error", str(exc.detail))

    app.add_exception_handler(DomainError, on_domain_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_error)


# ---------------------------------------------------------------------------- #
# Request middleware: request id, tracing, auth context, metrics, access log
# ---------------------------------------------------------------------------- #
def internal_token_authenticator(tokens: TokenService) -> Authenticator:
    async def authenticate(headers: Headers) -> Principal | None:
        token = headers.get(INTERNAL_TOKEN_HEADER)
        return tokens.verify(token) if token else None

    return authenticate


_UNTRACED_PATHS = {"/healthz", "/readyz", "/metrics"}
ROUTE_OVERRIDE = "ainative.route"


class RequestContextMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware) so context vars reach handlers."""

    def __init__(
        self, app: ASGIApp, runtime: ServiceRuntime, authenticator: Authenticator | None = None
    ) -> None:
        self.app = app
        self.runtime = runtime
        self.authenticate = authenticator or internal_token_authenticator(runtime.internal_tokens)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in _UNTRACED_PATHS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get("x-request-id") or new_id("req")
        method = scope["method"]
        state = {"status": 500, "started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["started"] = True
                response_headers = MutableHeaders(scope=message)
                response_headers["x-request-id"] = request_id
                trace_id = trace.get_current_span().get_span_context().trace_id
                if trace_id:
                    response_headers["x-trace-id"] = f"{trace_id:032x}"
            await send(message)

        started = time.perf_counter()
        runtime = self.runtime
        with (
            use_request_id(request_id),
            runtime.tracer.start_as_current_span(
                f"{method} {scope['path']}",
                context=extract_context(headers),
                kind=SpanKind.SERVER,
                attributes={"http.request.method": method, "url.path": scope["path"]},
            ) as span,
        ):
            try:
                principal = await self.authenticate(headers)
            except DomainError as exc:
                await domain_error_response(exc)(scope, receive, send_wrapper)
                self._finish(span, scope, method, int(state["status"]), started)
                return

            with use_principal(principal) if principal else nullcontext():
                if principal is not None:
                    span.set_attribute("enduser.id", principal.subject)
                    if principal.tenant_id:
                        span.set_attribute("tenant.id", principal.tenant_id)
                try:
                    await self.app(scope, receive, send_wrapper)
                except Exception as exc:
                    span.record_exception(exc)
                    runtime.logger.exception("unhandled error")
                    if not state["started"]:
                        await problem(500, "internal_error", "internal server error")(
                            scope, receive, send_wrapper
                        )
                finally:
                    self._finish(span, scope, method, int(state["status"]), started)

    def _finish(
        self, span: trace.Span, scope: Scope, method: str, status: int, started: float
    ) -> None:
        # Handlers may name a more meaningful low-cardinality route (e.g. a proxy prefix).
        route_obj = scope.get("route")
        route = scope.get(ROUTE_OVERRIDE) or getattr(route_obj, "path", None) or "unmatched"
        elapsed = time.perf_counter() - started
        span.update_name(f"{method} {route}")
        span.set_attribute("http.route", route)
        span.set_attribute("http.response.status_code", status)
        if status >= 500:
            span.set_status(Status(StatusCode.ERROR))
        metrics = self.runtime.metrics
        metrics.http_requests.labels(self.runtime.name, method, route, str(status)).inc()
        metrics.http_latency.labels(self.runtime.name, method, route).observe(elapsed)
        self.runtime.logger.info(
            "request",
            extra={
                "http_method": method,
                "http_route": route,
                "http_status": status,
                "duration_ms": round(elapsed * 1000, 2),
            },
        )


# ---------------------------------------------------------------------------- #
# Authorization dependencies
# ---------------------------------------------------------------------------- #
def require(permission: Permission, *, tenant: bool = True) -> Any:
    """FastAPI dependency: the caller must hold ``permission`` (and act for a tenant)."""

    async def dependency() -> Principal:
        principal = require_principal()
        principal.require(permission)
        if tenant and not principal.tenant_id:
            raise PermissionDenied("this operation requires a tenant context")
        return principal

    return Depends(dependency)


def require_authenticated() -> Any:
    async def dependency() -> Principal:
        return require_principal()

    return Depends(dependency)


# ---------------------------------------------------------------------------- #
# App factory
# ---------------------------------------------------------------------------- #
def create_service_app(
    runtime: ServiceRuntime,
    *,
    title: str,
    description: str = "",
    routers: Sequence[APIRouter] = (),
    authenticator: Authenticator | None = None,
    version: str = "1.0.0",
    on_startup: Sequence[Callable[[], Awaitable[None]]] = (),
    on_shutdown: Sequence[Callable[[], Awaitable[None]]] = (),
    middleware: Sequence[tuple[Any, dict[str, Any]]] = (),
) -> FastAPI:
    """Build a service's FastAPI app.

    ``middleware`` runs *inside* the request-context middleware (so it already sees
    the authenticated principal), in the order given (first = outermost).
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        for hook in on_startup:
            await hook()
        yield
        for hook in on_shutdown:
            await hook()
        await runtime.stop()

    app = FastAPI(title=title, description=description, version=version, lifespan=lifespan)
    app.state.runtime = runtime
    install_error_handlers(app)
    for middleware_class, options in reversed(middleware):
        app.add_middleware(middleware_class, **options)
    app.add_middleware(RequestContextMiddleware, runtime=runtime, authenticator=authenticator)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": runtime.name}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        results = {name: await check() for name, check in runtime.readiness_checks.items()}
        ready = all(results.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": results},
            status_code=200 if ready else 503,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(runtime.metrics.render(), media_type="text/plain; version=0.0.4")

    for router in routers:
        app.include_router(router)
    return app


def get_runtime(request: Request) -> ServiceRuntime:
    runtime = request.app.state.runtime
    assert isinstance(runtime, ServiceRuntime)
    return runtime
