from __future__ import annotations

import pytest

from platform_core.errors import PermissionDenied, ValidationFailed
from platform_core.rbac import (
    STAFF_PERMISSIONS,
    Permission,
    Role,
    can_manage_role,
    delegate_scopes,
    permissions_for,
)


def test_roles_are_strictly_nested() -> None:
    viewer, member, admin, owner = (
        permissions_for(r) for r in (Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER)
    )
    assert viewer < member < admin < owner
    assert Permission.DOCUMENTS_WRITE not in viewer
    assert Permission.BILLING_MANAGE in owner
    assert Permission.BILLING_MANAGE not in admin


def test_staff_permissions_are_separate_from_tenant_roles() -> None:
    assert not STAFF_PERMISSIONS & permissions_for(Role.OWNER)
    assert permissions_for(Role.VIEWER, staff=True) >= STAFF_PERMISSIONS


@pytest.mark.parametrize(
    ("actor", "target", "allowed"),
    [
        (Role.OWNER, Role.OWNER, True),
        (Role.OWNER, Role.ADMIN, True),
        (Role.ADMIN, Role.MEMBER, True),
        (Role.ADMIN, Role.ADMIN, False),  # no peer escalation
        (Role.ADMIN, Role.OWNER, False),
        (Role.MEMBER, Role.VIEWER, True),
        (Role.VIEWER, Role.VIEWER, False),
        (Role.SERVICE, Role.VIEWER, False),
    ],
)
def test_can_manage_role(actor: Role, target: Role, allowed: bool) -> None:
    assert can_manage_role(actor, target) is allowed


def test_api_key_scopes_cannot_exceed_creator() -> None:
    admin = permissions_for(Role.ADMIN)
    scopes = delegate_scopes(["documents:write", "search:query"], admin)
    assert scopes == {Permission.DOCUMENTS_WRITE, Permission.SEARCH_QUERY}
    with pytest.raises(PermissionDenied, match="billing:manage"):
        delegate_scopes(["billing:manage"], admin)
    with pytest.raises(ValidationFailed):
        delegate_scopes(["does:not-exist"], admin)
