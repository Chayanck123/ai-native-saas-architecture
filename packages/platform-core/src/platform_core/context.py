"""Request-scoped context: who is calling and on behalf of which tenant.

The context travels implicitly (``contextvars``) through async code, and explicitly
across process boundaries: in the internal JWT for HTTP calls and in the
``tenantid`` attribute of events and tasks. Anything tenant-scoped reads the
tenant from here instead of trusting a request parameter.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum

from platform_core.errors import AuthenticationRequired, PermissionDenied, TenantContextMissing
from platform_core.rbac import Permission, Role


class PrincipalKind(StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SERVICE = "service"
    SYSTEM = "system"


@dataclass(frozen=True)
class Principal:
    subject: str
    kind: PrincipalKind
    tenant_id: str | None
    role: Role
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    plan: str = "free"
    email: str | None = None
    is_staff: bool = False

    def has(self, permission: Permission) -> bool:
        return self.kind is PrincipalKind.SYSTEM or permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.has(permission):
            raise PermissionDenied(f"missing permission '{permission}'")

    @classmethod
    def system(cls, service: str) -> Principal:
        """A trusted, cross-tenant identity for background jobs (never issued to clients)."""
        return cls(
            subject=f"system:{service}",
            kind=PrincipalKind.SYSTEM,
            tenant_id=None,
            role=Role.SERVICE,
        )

    @classmethod
    def service(
        cls, service: str, tenant_id: str, permissions: frozenset[Permission], plan: str = "free"
    ) -> Principal:
        """A service acting for one tenant, e.g. a worker processing that tenant's task."""
        return cls(
            subject=f"service:{service}",
            kind=PrincipalKind.SERVICE,
            tenant_id=tenant_id,
            role=Role.SERVICE,
            permissions=permissions,
            plan=plan,
        )


_principal: ContextVar[Principal | None] = ContextVar("principal", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_principal() -> Principal | None:
    return _principal.get()


def require_principal() -> Principal:
    principal = _principal.get()
    if principal is None:
        raise AuthenticationRequired("authentication required")
    return principal


def current_tenant_id() -> str | None:
    return _tenant_id.get()


def require_tenant_id() -> str:
    tenant_id = _tenant_id.get()
    if not tenant_id:
        raise TenantContextMissing("no tenant in context")
    return tenant_id


def is_system_context() -> bool:
    principal = _principal.get()
    return principal is not None and principal.kind is PrincipalKind.SYSTEM


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def use_principal(principal: Principal) -> Iterator[Principal]:
    p_token = _principal.set(principal)
    t_token = _tenant_id.set(principal.tenant_id)
    try:
        yield principal
    finally:
        _tenant_id.reset(t_token)
        _principal.reset(p_token)


@contextmanager
def use_tenant(tenant_id: str | None) -> Iterator[None]:
    token = _tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _tenant_id.reset(token)


@contextmanager
def use_request_id(request_id: str | None) -> Iterator[None]:
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)
