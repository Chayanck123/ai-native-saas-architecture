"""Public API routes owned by the gateway, plus the proxy catch-all."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from gateway_service.identity import IdentityService, IssuedToken
from gateway_service.proxy import Proxy
from gateway_service.webhooks import WebhookService
from platform_core.catalog import WEBHOOK_EVENT_TYPES
from platform_core.context import Principal
from platform_core.rbac import Permission, Role
from platform_core.service import get_runtime, require, require_authenticated


def _identity(request: Request) -> IdentityService:
    service = request.app.state.identity
    assert isinstance(service, IdentityService)
    return service


def _webhooks(request: Request) -> WebhookService:
    service = request.app.state.webhooks
    assert isinstance(service, WebhookService)
    return service


# ---------------------------------------------------------------- schemas
class Attribution(BaseModel):
    utm_source: str | None = Field(default=None, max_length=100)
    utm_medium: str | None = Field(default=None, max_length=100)
    utm_campaign: str | None = Field(default=None, max_length=100)
    referrer: str | None = Field(default=None, max_length=500)


class SignupIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=200)
    workspace_name: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=200)
    anonymous_id: str | None = Field(default=None, max_length=64)
    attribution: Attribution | None = None


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)
    tenant_id: str | None = None


class SwitchIn(BaseModel):
    tenant_id: str


class AcceptInvitationIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=200)
    name: str | None = Field(default=None, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int
    user: dict[str, Any]
    tenant: dict[str, Any]
    role: str

    @classmethod
    def of(cls, issued: IssuedToken) -> TokenOut:
        return cls(
            access_token=issued.access_token,
            expires_in=issued.expires_in,
            user={
                "id": issued.user.id,
                "email": issued.user.email,
                "name": issued.user.name,
                "is_staff": issued.user.is_staff,
            },
            tenant={
                "id": issued.tenant.id,
                "name": issued.tenant.name,
                "slug": issued.tenant.slug,
                "plan": issued.tenant.plan,
            },
            role=issued.role.value,
        )


class InviteIn(BaseModel):
    email: str = Field(max_length=320)
    role: Role = Role.MEMBER


class RoleIn(BaseModel):
    role: Role


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1)


class WebhookIn(BaseModel):
    url: str = Field(max_length=2000)
    event_types: list[str] = Field(min_length=1, description=f"any of {list(WEBHOOK_EVENT_TYPES)}")


# ------------------------------------------------------------------- auth
auth = APIRouter(prefix="/v1/auth", tags=["auth"])


@auth.post("/signup", status_code=status.HTTP_201_CREATED, response_model=TokenOut)
async def signup(body: SignupIn, request: Request) -> TokenOut:
    """Create a workspace and its owner account. Free plan, no credit card."""
    issued = await _identity(request).signup(
        email=body.email,
        password=body.password,
        workspace_name=body.workspace_name,
        name=body.name,
        anonymous_id=body.anonymous_id,
        attribution=body.attribution.model_dump() if body.attribution else None,
    )
    return TokenOut.of(issued)


@auth.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request) -> TokenOut:
    return TokenOut.of(
        await _identity(request).login(
            email=body.email, password=body.password, tenant_id=body.tenant_id
        )
    )


@auth.post("/switch-workspace", response_model=TokenOut)
async def switch_workspace(
    body: SwitchIn, request: Request, _: Principal = require_authenticated()
) -> TokenOut:
    return TokenOut.of(await _identity(request).switch_workspace(body.tenant_id))


@auth.post("/accept-invitation", response_model=TokenOut)
async def accept_invitation(body: AcceptInvitationIn, request: Request) -> TokenOut:
    return TokenOut.of(
        await _identity(request).accept_invitation(body.token, body.password, body.name)
    )


# --------------------------------------------------------------- identity
me = APIRouter(prefix="/v1", tags=["workspace"])


@me.get("/me")
async def whoami(request: Request, _: Principal = require_authenticated()) -> dict[str, Any]:
    return await _identity(request).me()


@me.get("/members")
async def list_members(
    request: Request, _: Principal = require(Permission.MEMBERS_READ)
) -> list[dict[str, Any]]:
    return await _identity(request).list_members()


@me.post("/members", status_code=status.HTTP_201_CREATED)
async def invite_member(
    body: InviteIn, request: Request, _: Principal = require(Permission.MEMBERS_MANAGE)
) -> dict[str, Any]:
    """Invite a teammate. Returns 402 when the plan's seat limit is reached."""
    return await _identity(request).invite_member(body.email, body.role)


@me.patch("/members/{user_id}")
async def change_role(
    user_id: str,
    body: RoleIn,
    request: Request,
    _: Principal = require(Permission.MEMBERS_MANAGE),
) -> dict[str, Any]:
    return await _identity(request).change_role(user_id, body.role)


@me.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: str, request: Request, _: Principal = require(Permission.MEMBERS_MANAGE)
) -> Response:
    await _identity(request).remove_member(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@me.get("/api-keys")
async def list_api_keys(
    request: Request, _: Principal = require(Permission.API_KEYS_MANAGE)
) -> list[dict[str, Any]]:
    return await _identity(request).list_api_keys()


@me.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyIn, request: Request, _: Principal = require(Permission.API_KEYS_MANAGE)
) -> dict[str, Any]:
    """Create a scoped API key for server-to-server integrations. Shown only once."""
    return await _identity(request).create_api_key(body.name, body.scopes)


@me.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: str, request: Request, _: Principal = require(Permission.API_KEYS_MANAGE)
) -> Response:
    await _identity(request).revoke_api_key(key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@me.get("/webhooks")
async def list_webhooks(
    request: Request, _: Principal = require(Permission.WEBHOOKS_MANAGE)
) -> list[dict[str, Any]]:
    return await _webhooks(request).list_endpoints()


@me.post("/webhooks", status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookIn, request: Request, _: Principal = require(Permission.WEBHOOKS_MANAGE)
) -> dict[str, Any]:
    return await _webhooks(request).create_endpoint(body.url, body.event_types)


@me.delete("/webhooks/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    endpoint_id: str, request: Request, _: Principal = require(Permission.WEBHOOKS_MANAGE)
) -> Response:
    await _webhooks(request).delete_endpoint(endpoint_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@me.get("/webhooks/{endpoint_id}/deliveries")
async def webhook_deliveries(
    endpoint_id: str, request: Request, _: Principal = require(Permission.WEBHOOKS_MANAGE)
) -> list[dict[str, Any]]:
    return await _webhooks(request).list_deliveries(endpoint_id)


# ---------------------------------------------------------------- operations
ops = APIRouter(prefix="/v1/admin", tags=["operations"])
QUEUES = ("gateway", "knowledge", "agents", "billing", "growth")


@ops.get("/queues")
async def queue_health(
    request: Request, _: Principal = require(Permission.PLATFORM_ADMIN, tenant=False)
) -> dict[str, Any]:
    """Staff: depth of every service's task queue, including dead letters."""
    queue = get_runtime(request).infra.task_queue
    return {name: (await queue.stats(name)).__dict__ for name in QUEUES}


@ops.get("/queues/{queue_name}/dead-letters")
async def dead_letters(
    queue_name: str,
    request: Request,
    _: Principal = require(Permission.PLATFORM_ADMIN, tenant=False),
) -> list[dict[str, Any]]:
    letters = await get_runtime(request).infra.task_queue.dead_letters(queue_name)
    return [
        {
            "id": d.id,
            "task": d.task.name,
            "tenant_id": d.task.tenant_id,
            "attempts": d.task.attempt,
            "error": d.error,
            "failed_at": d.failed_at.isoformat(),
        }
        for d in letters
    ]


@ops.post("/queues/{queue_name}/dead-letters/{letter_id}/redrive")
async def redrive(
    queue_name: str,
    letter_id: str,
    request: Request,
    _: Principal = require(Permission.PLATFORM_ADMIN, tenant=False),
) -> dict[str, Any]:
    task = await get_runtime(request).infra.task_queue.redrive(queue_name, letter_id)
    return {"task_id": task.id, "name": task.name, "requeued": True}


# ------------------------------------------------------------------ proxy
proxy = APIRouter()


@proxy.get("/v1/openapi.json", include_in_schema=False)
async def platform_openapi(request: Request) -> dict[str, Any]:
    router_proxy = request.app.state.proxy
    assert isinstance(router_proxy, Proxy)
    return await router_proxy.openapi(request.app.openapi())


@proxy.api_route(
    "/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
)
async def forward(path: str, request: Request) -> Response:
    router_proxy = request.app.state.proxy
    assert isinstance(router_proxy, Proxy)
    return await router_proxy.forward(request)
