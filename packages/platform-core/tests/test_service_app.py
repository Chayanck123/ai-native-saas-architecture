from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel
from sqlalchemy import MetaData

from platform_core.config import CoreSettings
from platform_core.context import Principal, current_tenant_id
from platform_core.errors import Conflict, RateLimited
from platform_core.infrastructure import Infrastructure
from platform_core.rbac import Permission, Role
from platform_core.service import ServiceRuntime, create_service_app, require
from platform_core.testing import asgi_client, internal_headers, tenant_principal

router = APIRouter()


class ThingIn(BaseModel):
    name: str


@router.get("/v1/things")
async def list_things(principal: Principal = require(Permission.DOCUMENTS_READ)) -> dict[str, str]:
    return {"tenant": principal.tenant_id or "", "context_tenant": current_tenant_id() or ""}


@router.post("/v1/things")
async def create_thing(
    body: ThingIn, principal: Principal = require(Permission.DOCUMENTS_WRITE)
) -> None:
    raise Conflict(f"thing '{body.name}' already exists", thing_id="thg_1")


@router.get("/v1/limited")
async def limited() -> None:
    raise RateLimited("slow down", retry_after=2.2)


@router.get("/v1/boom")
async def boom() -> None:
    raise RuntimeError("kaboom")


@pytest.fixture
async def service(infra: Infrastructure) -> AsyncIterator[tuple[FastAPI, ServiceRuntime]]:
    settings = CoreSettings(service_name="demo", database_url="sqlite+aiosqlite:///:memory:")
    runtime = ServiceRuntime.create("demo", settings, infra, metadata=MetaData())
    await runtime.start()
    yield create_service_app(runtime, title="Demo", routers=[router]), runtime
    await runtime.stop()


@pytest.fixture
async def client(service: tuple[FastAPI, ServiceRuntime]) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(service[0]) as client:
        yield client


def auth(runtime: ServiceRuntime, role: Role) -> dict[str, str]:
    return internal_headers(runtime.internal_tokens, tenant_principal("ten_9", role=role))


async def test_requests_without_internal_token_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/things")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "authentication_required"

    response = await client.get("/v1/things", headers={"x-internal-token": "not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_tenant_context_and_rbac(
    client: httpx.AsyncClient, service: tuple[FastAPI, ServiceRuntime]
) -> None:
    runtime = service[1]
    response = await client.get("/v1/things", headers=auth(runtime, Role.VIEWER))
    assert response.status_code == 200
    assert response.json() == {"tenant": "ten_9", "context_tenant": "ten_9"}

    denied = await client.post("/v1/things", json={"name": "x"}, headers=auth(runtime, Role.VIEWER))
    assert denied.status_code == 403
    assert "documents:write" in denied.json()["detail"]

    conflict = await client.post(
        "/v1/things", json={"name": "x"}, headers=auth(runtime, Role.MEMBER)
    )
    assert conflict.status_code == 409
    assert conflict.json()["thing_id"] == "thg_1"


async def test_problem_responses_for_validation_rate_limits_and_crashes(
    client: httpx.AsyncClient, service: tuple[FastAPI, ServiceRuntime]
) -> None:
    runtime = service[1]
    invalid = await client.post("/v1/things", json={}, headers=auth(runtime, Role.OWNER))
    assert invalid.status_code == 422
    assert invalid.json()["errors"][0]["loc"] == ["body", "name"]

    limited = await client.get("/v1/limited")
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "3"

    crashed = await client.get("/v1/boom")
    assert crashed.status_code == 500
    assert crashed.json()["code"] == "internal_error"
    assert "kaboom" not in crashed.text  # internals are not leaked to clients
    assert crashed.headers["x-request-id"].startswith("req_")


async def test_trace_context_is_continued_from_incoming_traceparent(
    client: httpx.AsyncClient, service: tuple[FastAPI, ServiceRuntime]
) -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    response = await client.get(
        "/v1/things",
        headers={
            **auth(service[1], Role.OWNER),
            "traceparent": f"00-{trace_id}-00f067aa0ba902b7-01",
            "x-request-id": "req_from_client",
        },
    )
    assert response.headers["x-trace-id"] == trace_id
    assert response.headers["x-request-id"] == "req_from_client"


async def test_health_readiness_and_metrics_endpoints(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok", "service": "demo"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": True, "infrastructure": True}
    await client.get("/v1/things")
    metrics = (await client.get("/metrics")).text
    assert 'http_requests_total{method="GET",route="/v1/things",service="demo",status="401"}' in (
        metrics
    )
