"""Helpers shared by the test suites of every service."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from platform_core.context import Principal, PrincipalKind
from platform_core.rbac import STAFF_PERMISSIONS, Role, permissions_for
from platform_core.security import INTERNAL_TOKEN_HEADER, TokenService


def tenant_principal(
    tenant_id: str = "ten_test",
    *,
    role: Role = Role.OWNER,
    subject: str = "usr_test",
    plan: str = "free",
    staff: bool = False,
) -> Principal:
    return Principal(
        subject=subject,
        kind=PrincipalKind.USER,
        tenant_id=tenant_id,
        role=role,
        permissions=permissions_for(role, staff=staff),
        plan=plan,
        email=f"{subject}@example.com",
        is_staff=staff,
    )


def staff_principal(subject: str = "usr_staff") -> Principal:
    return Principal(
        subject=subject,
        kind=PrincipalKind.USER,
        tenant_id=None,
        role=Role.VIEWER,
        permissions=STAFF_PERMISSIONS,
        email=f"{subject}@example.com",
        is_staff=True,
    )


def internal_headers(tokens: TokenService, principal: Principal) -> dict[str, str]:
    return {INTERNAL_TOKEN_HEADER: tokens.issue(principal, ttl_seconds=300)}


@asynccontextmanager
async def asgi_client(
    app: FastAPI, *, base_url: str = "http://test", headers: dict[str, str] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url, headers=headers) as client:
        yield client


class SpanRecorder:
    """Collects finished spans in memory so tests can assert on traces."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()

    def spans(self, name_prefix: str | None = None) -> Sequence[ReadableSpan]:
        spans = self.exporter.get_finished_spans()
        if name_prefix is None:
            return spans
        return [span for span in spans if span.name.startswith(name_prefix)]

    def clear(self) -> None:
        self.exporter.clear()
