"""Role-based access control.

Roles are coarse, human-friendly bundles of fine-grained permissions. Code always
checks *permissions* (``documents:write``), never role names, so roles can be
reshaped without touching business logic.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from platform_core.errors import PermissionDenied, ValidationFailed


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"
    SERVICE = "service"  # machine identities: API keys and internal services


class Permission(StrEnum):
    WORKSPACE_READ = "workspace:read"
    WORKSPACE_MANAGE = "workspace:manage"
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    API_KEYS_MANAGE = "api_keys:manage"
    WEBHOOKS_MANAGE = "webhooks:manage"
    BILLING_READ = "billing:read"
    BILLING_MANAGE = "billing:manage"
    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_WRITE = "documents:write"
    SEARCH_QUERY = "search:query"
    AGENTS_RUN = "agents:run"
    AGENTS_READ = "agents:read"
    FEEDBACK_WRITE = "feedback:write"
    TELEMETRY_WRITE = "telemetry:write"
    # Platform-staff permissions (the SaaS operator, not a customer tenant).
    ANALYTICS_READ = "analytics:read"
    EXPERIMENTS_MANAGE = "experiments:manage"
    PLATFORM_ADMIN = "platform:admin"


_VIEWER = frozenset(
    {
        Permission.WORKSPACE_READ,
        Permission.MEMBERS_READ,
        Permission.DOCUMENTS_READ,
        Permission.SEARCH_QUERY,
        Permission.AGENTS_READ,
    }
)
_MEMBER = _VIEWER | {
    Permission.DOCUMENTS_WRITE,
    Permission.AGENTS_RUN,
    Permission.FEEDBACK_WRITE,
    Permission.TELEMETRY_WRITE,
}
_ADMIN = _MEMBER | {
    Permission.MEMBERS_MANAGE,
    Permission.API_KEYS_MANAGE,
    Permission.WEBHOOKS_MANAGE,
    Permission.BILLING_READ,
}
_OWNER = _ADMIN | {Permission.BILLING_MANAGE, Permission.WORKSPACE_MANAGE}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_VIEWER),
    Role.MEMBER: frozenset(_MEMBER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_OWNER),
    # A service identity has no implicit permissions: its scopes are granted explicitly.
    Role.SERVICE: frozenset(),
}

STAFF_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.ANALYTICS_READ, Permission.EXPERIMENTS_MANAGE, Permission.PLATFORM_ADMIN}
)

# What an internal service may do while acting for one tenant (never staff powers).
TENANT_PERMISSIONS: frozenset[Permission] = frozenset(_OWNER)

_RANK: dict[Role, int] = {Role.VIEWER: 1, Role.MEMBER: 2, Role.ADMIN: 3, Role.OWNER: 4}


def permissions_for(role: Role, *, staff: bool = False) -> frozenset[Permission]:
    perms = ROLE_PERMISSIONS[role]
    return perms | STAFF_PERMISSIONS if staff else perms


def can_manage_role(actor: Role, target: Role) -> bool:
    """Whether ``actor`` may grant or revoke ``target``.

    Owners can manage everyone (including other owners); admins can manage roles
    strictly below their own. This prevents privilege escalation by admins.
    """
    if actor not in _RANK or target not in _RANK:
        return False
    if actor is Role.OWNER:
        return True
    return _RANK[actor] > _RANK[target]


def ensure_can_manage_role(actor: Role, target: Role) -> None:
    if not can_manage_role(actor, target):
        raise PermissionDenied(f"role '{actor}' cannot manage role '{target}'")


def parse_permissions(values: Iterable[str]) -> frozenset[Permission]:
    parsed: set[Permission] = set()
    for value in values:
        try:
            parsed.add(Permission(value))
        except ValueError as exc:
            raise ValidationFailed(f"unknown permission '{value}'") from exc
    return frozenset(parsed)


def delegate_scopes(
    requested: Iterable[str], grantor_permissions: frozenset[Permission]
) -> frozenset[Permission]:
    """Scopes for an API key must be a subset of what its creator holds."""
    scopes = parse_permissions(requested)
    excess = scopes - grantor_permissions
    if excess:
        names = ", ".join(sorted(excess))
        raise PermissionDenied(f"cannot delegate permissions you do not hold: {names}")
    return scopes
