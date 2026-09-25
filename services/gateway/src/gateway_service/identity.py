"""Identity and access: signup, login, members, roles and API keys."""

from __future__ import annotations

import hmac
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers

from gateway_service.models import ApiKey, Membership, Tenant, User
from gateway_service.settings import GatewaySettings
from platform_core.catalog import Events
from platform_core.clients import ServiceClient
from platform_core.context import (
    Principal,
    PrincipalKind,
    require_principal,
    require_tenant_id,
    use_principal,
    use_tenant,
)
from platform_core.errors import (
    AuthenticationRequired,
    Conflict,
    NotFound,
    PermissionDenied,
    QuotaExceeded,
    UpstreamUnavailable,
    ValidationFailed,
)
from platform_core.ids import new_id
from platform_core.rbac import (
    Role,
    delegate_scopes,
    ensure_can_manage_role,
    parse_permissions,
    permissions_for,
)
from platform_core.security import (
    TokenService,
    generate_api_key,
    hash_api_key,
    hash_password,
    parse_api_key,
    verify_password,
)
from platform_core.service import ServiceRuntime

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Verified against when the email is unknown, so response time does not reveal
# which emails have accounts (user enumeration).
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


@dataclass(frozen=True)
class IssuedToken:
    access_token: str
    expires_in: int
    user: User
    tenant: Tenant
    role: Role


def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if not _EMAIL.match(value) or len(value) > 320:
        raise ValidationFailed("invalid email address")
    return value


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "workspace"


class IdentityService:
    def __init__(
        self, runtime: ServiceRuntime, settings: GatewaySettings, billing: ServiceClient
    ) -> None:
        self.runtime = runtime
        self.settings = settings
        self.billing = billing
        self.tokens = TokenService(
            settings.jwt_secret, issuer="ainative-gateway", audience="ainative-api"
        )
        self.invites = TokenService(
            settings.jwt_secret, issuer="ainative-gateway", audience="ainative-invite"
        )
        self._system = Principal.system("gateway")
        self._key_cache: dict[str, tuple[float, Principal | None, str]] = {}
        self._plan_cache: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------ tokens
    def _principal(self, user: User, tenant: Tenant, role: Role) -> Principal:
        return Principal(
            subject=user.id,
            kind=PrincipalKind.USER,
            tenant_id=tenant.id,
            role=role,
            permissions=permissions_for(role, staff=user.is_staff),
            plan=tenant.plan,
            email=user.email,
            is_staff=user.is_staff,
        )

    def _issue(self, user: User, tenant: Tenant, role: Role) -> IssuedToken:
        ttl = self.settings.access_token_ttl_seconds
        token = self.tokens.issue(self._principal(user, tenant, role), ttl_seconds=ttl)
        return IssuedToken(token, ttl, user, tenant, role)

    # ------------------------------------------------------------ signup
    async def signup(
        self,
        *,
        email: str,
        password: str,
        workspace_name: str,
        name: str | None = None,
        anonymous_id: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> IssuedToken:
        """Self-serve signup: no sales call, no credit card - product-led growth."""
        email = normalize_email(email)
        password_hash = hash_password(password)
        now = self.runtime.clock.now()
        tenant_id, user_id = new_id("ten"), new_id("usr")
        user = User(
            id=user_id,
            email=email,
            name=name,
            password_hash=password_hash,
            is_staff=email in self.settings.staff_emails,
            created_at=now,
        )
        tenant = Tenant(
            id=tenant_id,
            name=workspace_name,
            slug=f"{slugify(workspace_name)}-{tenant_id[-6:]}",
            plan="free",
            created_at=now,
        )
        try:
            with use_principal(self._system), use_tenant(tenant_id):
                async with self.runtime.unit_of_work() as uow:
                    uow.session.add_all([user, tenant])
                    await uow.session.flush()
                    uow.session.add(
                        Membership(
                            id=new_id("mem"),
                            tenant_id=tenant_id,
                            user_id=user_id,
                            role=Role.OWNER.value,
                            created_at=now,
                        )
                    )
                    await uow.publish(
                        self.runtime.event(
                            Events.TENANT_CREATED,
                            {"name": workspace_name, "plan": "free", "owner_user_id": user_id},
                            tenant_id=tenant_id,
                            subject=tenant_id,
                        )
                    )
                    await uow.publish(
                        self.runtime.event(
                            Events.USER_SIGNED_UP,
                            {
                                "user_id": user_id,
                                "anonymous_id": anonymous_id,
                                "email_domain": email.split("@")[1],
                                "attribution": {k: v for k, v in (attribution or {}).items() if v},
                            },
                            tenant_id=tenant_id,
                            subject=user_id,
                        )
                    )
        except IntegrityError as exc:
            raise Conflict("an account with this email already exists") from exc
        self.runtime.metrics.business_events.labels(self.runtime.name, "signup").inc()
        return self._issue(user, tenant, Role.OWNER)

    # ------------------------------------------------------------- login
    async def login(
        self, *, email: str, password: str, tenant_id: str | None = None
    ) -> IssuedToken:
        email = email.strip().lower()
        with use_principal(self._system):
            async with self.runtime.unit_of_work() as uow:
                user = (
                    await uow.session.execute(select(User).where(User.email == email))
                ).scalar_one_or_none()
                if user is None or user.password_hash is None:
                    verify_password(password, _DUMMY_HASH)
                    raise AuthenticationRequired(
                        "invalid email or password", code="invalid_credentials"
                    )
                if not verify_password(password, user.password_hash):
                    raise AuthenticationRequired(
                        "invalid email or password", code="invalid_credentials"
                    )
                membership, tenant = await self._membership(uow.session, user.id, tenant_id)
                user.last_login_at = self.runtime.clock.now()
                await uow.publish(
                    self.runtime.event(
                        Events.USER_LOGGED_IN,
                        {"user_id": user.id},
                        tenant_id=tenant.id,
                        subject=user.id,
                    )
                )
        return self._issue(user, tenant, Role(membership.role))

    async def switch_workspace(self, tenant_id: str) -> IssuedToken:
        principal = require_principal()
        if principal.kind is not PrincipalKind.USER:
            raise PermissionDenied("only users can switch workspaces")
        with use_principal(self._system):
            async with self.runtime.db.session() as session:
                user = await session.get(User, principal.subject)
                if user is None:
                    raise AuthenticationRequired("user no longer exists")
                membership, tenant = await self._membership(session, user.id, tenant_id)
        return self._issue(user, tenant, Role(membership.role))

    async def _membership(
        self, session: AsyncSession, user_id: str, tenant_id: str | None
    ) -> tuple[Membership, Tenant]:
        query = (
            select(Membership, Tenant)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(Membership.user_id == user_id)
            .order_by(Membership.created_at)
        )
        if tenant_id:
            query = query.where(Membership.tenant_id == tenant_id)
        row = (await session.execute(query)).first()
        if row is None:
            raise PermissionDenied("you are not a member of this workspace")
        return row[0], row[1]

    async def me(self) -> dict[str, Any]:
        principal = require_principal()
        with use_principal(self._system):
            async with self.runtime.db.session() as session:
                tenant = await session.get(Tenant, principal.tenant_id or "")
        return {
            "subject": principal.subject,
            "kind": principal.kind.value,
            "email": principal.email,
            "role": principal.role.value,
            "permissions": sorted(p.value for p in principal.permissions),
            "is_staff": principal.is_staff,
            "tenant": {
                "id": tenant.id,
                "name": tenant.name,
                "slug": tenant.slug,
                "plan": tenant.plan,
            }
            if tenant
            else None,
        }

    # ----------------------------------------------------------- members
    async def list_members(self) -> list[dict[str, Any]]:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            rows = (
                await session.execute(
                    select(Membership, User)
                    .join(User, User.id == Membership.user_id)
                    .where(Membership.tenant_id == tenant_id)
                    .order_by(Membership.created_at)
                )
            ).all()
        return [
            {
                "user_id": u.id,
                "email": u.email,
                "name": u.name,
                "role": m.role,
                "joined_at": m.created_at.isoformat(),
                "pending": u.password_hash is None,
            }
            for m, u in rows
        ]

    async def _check_seat_entitlement(self, seats: int) -> None:
        try:
            entitlement = await self.billing.call(
                "GET", "/v1/billing/entitlements", params={"seats": seats}
            )
        except UpstreamUnavailable:
            self.runtime.logger.warning("billing unavailable; skipping seat check")
            return
        if not entitlement["allowed"]:
            raise QuotaExceeded(
                f"your plan allows {entitlement['max_seats']} seats - upgrade to add more",
                reason=entitlement["reason"],
                upgrade_to=entitlement["upgrade_to"],
            )

    async def invite_member(self, email: str, role: Role) -> dict[str, Any]:
        actor = require_principal()
        tenant_id = require_tenant_id()
        ensure_can_manage_role(actor.role, role)
        email = normalize_email(email)
        now = self.runtime.clock.now()
        async with self.runtime.db.session() as session:
            seats = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Membership)
                        .where(Membership.tenant_id == tenant_id)
                    )
                ).scalar_one()
            )
        await self._check_seat_entitlement(seats + 1)
        with use_principal(self._system), use_tenant(tenant_id):
            try:
                async with self.runtime.unit_of_work() as uow:
                    user = (
                        await uow.session.execute(select(User).where(User.email == email))
                    ).scalar_one_or_none()
                    if user is None:
                        user = User(
                            id=new_id("usr"),
                            email=email,
                            password_hash=None,
                            is_staff=email in self.settings.staff_emails,
                            created_at=now,
                        )
                        uow.session.add(user)
                        await uow.session.flush()
                    uow.session.add(
                        Membership(
                            id=new_id("mem"),
                            tenant_id=tenant_id,
                            user_id=user.id,
                            role=role.value,
                            created_at=now,
                        )
                    )
                    await uow.session.flush()
                    await uow.publish(
                        self.runtime.event(
                            Events.MEMBER_ADDED,
                            {"user_id": user.id, "role": role.value, "invited_by": actor.subject},
                            tenant_id=tenant_id,
                            subject=user.id,
                        )
                    )
            except IntegrityError as exc:
                raise Conflict(f"{email} is already a member of this workspace") from exc
        invitee = Principal(
            subject=user.id, kind=PrincipalKind.USER, tenant_id=tenant_id, role=role, email=email
        )
        invitation = self.invites.issue(invitee, ttl_seconds=self.settings.invite_ttl_seconds)
        # A real deployment e-mails this link; returning it keeps the flow API-first/testable.
        return {
            "user_id": user.id,
            "email": email,
            "role": role.value,
            "invitation_token": invitation if user.password_hash is None else None,
        }

    async def accept_invitation(self, token: str, password: str, name: str | None) -> IssuedToken:
        invitee = self.invites.verify(token)
        with use_principal(self._system):
            async with self.runtime.db.session() as session:
                user = await session.get(User, invitee.subject)
                if user is None:
                    raise NotFound("invitation is no longer valid")
                if user.password_hash is None:
                    user.password_hash = hash_password(password)
                    user.name = name or user.name
                membership, tenant = await self._membership(session, user.id, invitee.tenant_id)
        return self._issue(user, tenant, Role(membership.role))

    async def _owners(self, session: AsyncSession, tenant_id: str) -> int:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Membership)
                    .where(Membership.tenant_id == tenant_id, Membership.role == Role.OWNER.value)
                )
            ).scalar_one()
        )

    async def change_role(self, user_id: str, role: Role) -> dict[str, Any]:
        actor = require_principal()
        tenant_id = require_tenant_id()
        async with self.runtime.unit_of_work() as uow:
            membership = await self._member(uow.session, tenant_id, user_id)
            current = Role(membership.role)
            ensure_can_manage_role(actor.role, current)
            ensure_can_manage_role(actor.role, role)
            if (
                current is Role.OWNER
                and role is not Role.OWNER
                and await self._owners(uow.session, tenant_id) == 1
            ):
                raise Conflict("a workspace must keep at least one owner")
            membership.role = role.value
            await uow.publish(
                self.runtime.event(
                    Events.MEMBER_ROLE_CHANGED,
                    {"user_id": user_id, "role": role.value, "previous_role": current.value},
                    subject=user_id,
                )
            )
        return {"user_id": user_id, "role": role.value}

    async def remove_member(self, user_id: str) -> None:
        actor = require_principal()
        tenant_id = require_tenant_id()
        async with self.runtime.unit_of_work() as uow:
            membership = await self._member(uow.session, tenant_id, user_id)
            role = Role(membership.role)
            if user_id != actor.subject:
                ensure_can_manage_role(actor.role, role)
            if role is Role.OWNER and await self._owners(uow.session, tenant_id) == 1:
                raise Conflict("a workspace must keep at least one owner")
            await uow.session.delete(membership)
            await uow.publish(
                self.runtime.event(Events.MEMBER_REMOVED, {"user_id": user_id}, subject=user_id)
            )

    async def _member(self, session: AsyncSession, tenant_id: str, user_id: str) -> Membership:
        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.tenant_id == tenant_id, Membership.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise NotFound(f"user '{user_id}' is not a member of this workspace")
        return membership

    # ---------------------------------------------------------- API keys
    async def create_api_key(self, name: str, scopes: list[str]) -> dict[str, Any]:
        actor = require_principal()
        tenant_id = require_tenant_id()
        granted = delegate_scopes(scopes, actor.permissions)
        plaintext, key_id, key_hash = generate_api_key()
        now = self.runtime.clock.now()
        async with self.runtime.unit_of_work() as uow:
            uow.session.add(
                ApiKey(
                    id=key_id,
                    tenant_id=tenant_id,
                    name=name,
                    key_hash=key_hash,
                    scopes=sorted(p.value for p in granted),
                    created_by=actor.subject,
                    created_at=now,
                )
            )
            await uow.publish(
                self.runtime.event(
                    Events.API_KEY_CREATED,
                    {
                        "key_id": key_id,
                        "scopes": sorted(p.value for p in granted),
                        "created_by": actor.subject,
                    },
                    subject=key_id,
                )
            )
        return {
            "id": key_id,
            "name": name,
            "scopes": sorted(p.value for p in granted),
            "key": plaintext,
            "created_at": now.isoformat(),
        }

    async def list_api_keys(self) -> list[dict[str, Any]]:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            keys = (
                await session.execute(
                    select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at)
                )
            ).scalars()
            return [
                {
                    "id": k.id,
                    "name": k.name,
                    "scopes": k.scopes,
                    "created_at": k.created_at.isoformat(),
                    "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                    "revoked": k.revoked_at is not None,
                }
                for k in keys
            ]

    async def revoke_api_key(self, key_id: str) -> None:
        tenant_id = require_tenant_id()
        async with self.runtime.db.session() as session:
            key = (
                await session.execute(
                    select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.id == key_id)
                )
            ).scalar_one_or_none()
            if key is None:
                raise NotFound(f"API key '{key_id}' not found")
            key.revoked_at = self.runtime.clock.now()
        self._key_cache.pop(key_id, None)

    # --------------------------------------------------- authentication
    async def authenticate(self, headers: Headers) -> Principal | None:
        """Resolve the caller from ``Authorization: Bearer <jwt | api key>`` or ``X-API-Key``."""
        authorization = headers.get("authorization", "")
        credential = headers.get("x-api-key") or (
            authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        )
        if not credential:
            return None
        if credential.startswith("ank_"):
            return await self._authenticate_api_key(credential)
        return self.tokens.verify(credential)

    async def _authenticate_api_key(self, plaintext: str) -> Principal:
        key_id = parse_api_key(plaintext)
        if key_id is None:
            raise AuthenticationRequired("malformed API key", code="invalid_api_key")
        now = time.monotonic()
        cached = self._key_cache.get(key_id)
        if cached is None or cached[0] < now:
            principal = await self._load_api_key(key_id)
            cached = (now + self.settings.api_key_cache_seconds, principal[0], principal[1])
            self._key_cache[key_id] = cached
        _, principal_or_none, key_hash = cached
        if principal_or_none is None or not hmac.compare_digest(hash_api_key(plaintext), key_hash):
            raise AuthenticationRequired("invalid or revoked API key", code="invalid_api_key")
        return principal_or_none

    async def _load_api_key(self, key_id: str) -> tuple[Principal | None, str]:
        with use_principal(self._system):
            async with self.runtime.db.session() as session:
                row = (
                    await session.execute(
                        select(ApiKey, Tenant)
                        .join(Tenant, Tenant.id == ApiKey.tenant_id)
                        .where(ApiKey.id == key_id)
                    )
                ).first()
                if row is None or row[0].revoked_at is not None:
                    return None, ""
                key, tenant = row
                key.last_used_at = self.runtime.clock.now()
        principal = Principal(
            subject=f"key:{key.id}",
            kind=PrincipalKind.API_KEY,
            tenant_id=tenant.id,
            role=Role.SERVICE,
            permissions=parse_permissions(key.scopes),
            plan=tenant.plan,
        )
        return principal, key.key_hash

    # ------------------------------------------------------ tenant plans
    async def tenant_plan(self, tenant_id: str) -> str:
        """Current plan (cached briefly) - JWT claims can be stale after an upgrade."""
        now = time.monotonic()
        cached = self._plan_cache.get(tenant_id)
        if cached and cached[0] > now:
            return cached[1]
        with use_principal(self._system):
            async with self.runtime.db.session() as session:
                tenant = await session.get(Tenant, tenant_id)
        plan = tenant.plan if tenant else "free"
        self._plan_cache[tenant_id] = (now + 30.0, plan)
        return plan

    async def set_tenant_plan(self, tenant_id: str, plan: str) -> dict[str, Any]:
        with use_principal(self._system):
            async with self.runtime.db.session() as session:
                tenant = await session.get(Tenant, tenant_id)
                if tenant is None:
                    return {"updated": False}
                tenant.plan = plan
        self._plan_cache.pop(tenant_id, None)
        self._key_cache = {
            k: v for k, v in self._key_cache.items() if v[1] is None or v[1].tenant_id != tenant_id
        }
        return {"updated": True, "plan": plan}
