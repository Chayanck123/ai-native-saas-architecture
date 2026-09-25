"""The public API through the gateway, with every service running behind it."""

from __future__ import annotations

import httpx
import pytest

from devstack.stack import DevStack, build_devstack
from devstack.webhooks import WebhookReceiver
from gateway_service.webhooks import validate_webhook_url
from platform_core.clock import ManualClock
from platform_core.errors import ValidationFailed

from .conftest import HANDBOOK, client_for, signup


async def test_signup_login_and_me(stack: DevStack) -> None:
    owner = await signup(stack, "Founder@Acme.io", name="Ada")
    assert owner["role"] == "owner"
    assert owner["tenant"]["plan"] == "free"

    async with stack.client() as client:
        duplicate = await client.post(
            "/v1/auth/signup",
            json={
                "email": "founder@acme.io",
                "password": "another-password",
                "workspace_name": "X",
            },
        )
        weak = await client.post(
            "/v1/auth/signup",
            json={"email": "b@acme.io", "password": "short", "workspace_name": "X"},
        )
        wrong = await client.post(
            "/v1/auth/login", json={"email": "founder@acme.io", "password": "wrong-password"}
        )
        unknown = await client.post(
            "/v1/auth/login", json={"email": "nobody@acme.io", "password": "wrong-password"}
        )
        login = await client.post(
            "/v1/auth/login", json={"email": "founder@acme.io", "password": "correct-horse-battery"}
        )
    assert duplicate.status_code == 409
    assert weak.status_code == 422
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]  # no user enumeration
    assert login.status_code == 200

    async with client_for(stack, login.json()["access_token"]) as client:
        me = (await client.get("/v1/me")).json()
    assert me["email"] == "founder@acme.io"
    assert me["tenant"]["name"] == "Acme"
    assert "billing:manage" in me["permissions"]
    async with stack.client() as anonymous:
        assert (await anonymous.get("/v1/me")).status_code == 401
        assert (await anonymous.get("/v1/documents")).status_code == 401


async def test_members_roles_and_invitations(stack: DevStack) -> None:
    owner = await signup(stack, "owner@acme.io")
    async with client_for(stack, owner["access_token"]) as client:
        invite = await client.post("/v1/members", json={"email": "dev@acme.io", "role": "member"})
        assert invite.status_code == 201
        token = invite.json()["invitation_token"]
    async with stack.client() as client:
        accepted = await client.post(
            "/v1/auth/accept-invitation", json={"token": token, "password": "dev-password-123"}
        )
    assert accepted.status_code == 200
    member = accepted.json()
    assert member["role"] == "member" and member["tenant"]["id"] == owner["tenant"]["id"]

    async with client_for(stack, member["access_token"]) as client:
        forbidden = await client.post("/v1/members", json={"email": "x@acme.io"})
        assert forbidden.status_code == 403
        assert len((await client.get("/v1/members")).json()) == 2
    async with client_for(stack, owner["access_token"]) as client:
        promoted = await client.patch(f"/v1/members/{member['user']['id']}", json={"role": "admin"})
        assert promoted.json()["role"] == "admin"
        last_owner = await client.delete(f"/v1/members/{owner['user']['id']}")
        assert last_owner.status_code == 409  # a workspace always keeps an owner


async def test_free_plan_seat_limit_is_an_upgrade_moment(stack: DevStack) -> None:
    owner = await signup(stack, "owner@seats.io")
    async with client_for(stack, owner["access_token"]) as client:
        for index in range(2):
            ok = await client.post("/v1/members", json={"email": f"m{index}@seats.io"})
            assert ok.status_code == 201
        await stack.drain()
        blocked = await client.post("/v1/members", json={"email": "fourth@seats.io"})
    assert blocked.status_code == 402
    assert blocked.json()["upgrade_to"] == "pro"


async def test_scoped_api_keys(stack: DevStack) -> None:
    owner = await signup(stack, "owner@keys.io")
    async with client_for(stack, owner["access_token"]) as client:
        created = await client.post(
            "/v1/api-keys",
            json={"name": "ingestion pipeline", "scopes": ["documents:write", "search:query"]},
        )
        assert created.status_code == 201
        key = created.json()["key"]
        listed = (await client.get("/v1/api-keys")).json()
        assert "key" not in listed[0]  # plaintext is never shown again

    async with stack.client(**{"x-api-key": key}) as integration:
        upload = await integration.post(
            "/v1/documents", json={"title": "Handbook", "content": HANDBOOK}
        )
        assert upload.status_code == 202
        await stack.drain()
        search = await integration.post("/v1/search", json={"query": "refund window"})
        assert search.status_code == 200 and search.json()["results"]
        no_scope = await integration.post("/v1/agents/runs", json={"question": "hi"})
        assert no_scope.status_code == 403  # enforced by the agents service itself

    async with client_for(stack, owner["access_token"]) as client:
        await client.delete(f"/v1/api-keys/{created.json()['id']}")
    async with stack.client(**{"authorization": f"Bearer {key}"}) as revoked:
        assert (await revoked.get("/v1/documents")).status_code == 401


async def test_api_key_cannot_escalate_privileges(stack: DevStack) -> None:
    owner = await signup(stack, "owner@esc.io")
    async with client_for(stack, owner["access_token"]) as client:
        invite = await client.post("/v1/members", json={"email": "adm@esc.io", "role": "admin"})
    async with stack.client() as client:
        admin = (
            await client.post(
                "/v1/auth/accept-invitation",
                json={"token": invite.json()["invitation_token"], "password": "admin-password"},
            )
        ).json()
    async with client_for(stack, admin["access_token"]) as client:
        escalate = await client.post(
            "/v1/api-keys", json={"name": "x", "scopes": ["billing:manage"]}
        )
    assert escalate.status_code == 403


async def test_idempotency_keys_make_retries_safe(stack: DevStack) -> None:
    owner = await signup(stack, "owner@idem.io")
    headers = {"idempotency-key": "upload-42"}
    document = {"title": "Handbook", "content": HANDBOOK}
    async with client_for(stack, owner["access_token"]) as client:
        first = await client.post("/v1/documents", json=document, headers=headers)
        retry = await client.post("/v1/documents", json=document, headers=headers)
        misuse = await client.post(
            "/v1/documents", json={**document, "title": "Other"}, headers=headers
        )
        listed = (await client.get("/v1/documents")).json()
    assert first.status_code == retry.status_code == 202
    assert retry.json()["id"] == first.json()["id"]
    assert retry.headers["idempotent-replayed"] == "true"
    assert misuse.status_code == 422
    assert misuse.json()["code"] == "idempotency_key_reused"
    assert listed["total"] == 1


async def test_rate_limits_follow_the_plan(stack: DevStack, clock: ManualClock) -> None:
    owner = await signup(stack, "owner@rl.io")
    async with client_for(stack, owner["access_token"]) as client:
        statuses = [(await client.get("/v1/me")).status_code for _ in range(61)]
        assert statuses[:60] == [200] * 60
        assert statuses[60] == 429
        limited = await client.get("/v1/me")
        assert limited.headers["retry-after"] == "1"
        assert limited.json()["code"] == "rate_limited"
        clock.advance(2)
        recovered = await client.get("/v1/me")
    assert recovered.status_code == 200
    assert recovered.headers["x-ratelimit-limit"] == "60"


async def test_signed_webhooks_are_delivered_with_retries(
    stack: DevStack, receiver: WebhookReceiver, clock: ManualClock
) -> None:
    owner = await signup(stack, "owner@hooks.io")
    async with client_for(stack, owner["access_token"]) as client:
        created = await client.post(
            "/v1/webhooks",
            json={"url": "https://hooks.example.com/ainative", "event_types": ["document.indexed"]},
        )
        assert created.status_code == 201
        secret = created.json()["secret"]
        bad_type = await client.post(
            "/v1/webhooks",
            json={"url": "https://hooks.example.com/x", "event_types": ["billing.secret.event"]},
        )
        assert bad_type.status_code == 422
        await client.post("/v1/documents", json={"title": "Handbook", "content": HANDBOOK})
        await stack.drain()
        deliveries = (await client.get(f"/v1/webhooks/{created.json()['id']}/deliveries")).json()

    [webhook] = receiver.received
    assert webhook.headers["content-type"] == "application/cloudevents+json"
    assert webhook.event["type"] == "document.indexed"
    assert receiver.verify(webhook, secret)
    assert not receiver.verify(webhook, "whsec_wrong")
    assert deliveries[0]["status"] == "succeeded"


async def test_failing_webhook_receivers_are_retried_with_backoff(clock: ManualClock) -> None:
    receiver = WebhookReceiver(clock, statuses=[503, 502])
    stack = build_devstack(clock=clock, webhook_transport=receiver.transport)
    await stack.start()
    try:
        owner = await signup(stack, "owner@flaky.io")
        async with client_for(stack, owner["access_token"]) as client:
            hook = (
                await client.post(
                    "/v1/webhooks",
                    json={
                        "url": "https://flaky.example.com/hook",
                        "event_types": ["document.indexed"],
                    },
                )
            ).json()
            await client.post("/v1/documents", json={"title": "H", "content": HANDBOOK})
            for _ in range(4):
                await stack.drain()
                clock.advance(600)  # past the exponential backoff
            deliveries = (await client.get(f"/v1/webhooks/{hook['id']}/deliveries")).json()
        assert len(receiver.received) == 3  # 503, 502, then 200
        assert deliveries[0]["status"] == "succeeded"
        assert deliveries[0]["attempts"] == 3
    finally:
        await stack.stop()


def test_webhook_urls_are_protected_against_ssrf() -> None:
    for url in [
        "http://hooks.example.com",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/x",
        "https://localhost/x",
        "https://db.internal/x",
    ]:
        with pytest.raises(ValidationFailed):
            validate_webhook_url(url, allow_private=False)
    assert validate_webhook_url("https://hooks.example.com/x", allow_private=False)


async def test_platform_openapi_merges_every_service(stack: DevStack) -> None:
    async with stack.client() as client:
        schema = (await client.get("/v1/openapi.json")).json()
    for path in [
        "/v1/auth/signup",
        "/v1/documents",
        "/v1/search",
        "/v1/agents/runs",
        "/v1/billing/usage",
        "/v1/experiments",
        "/v1/analytics/funnel",
    ]:
        assert path in schema["paths"], path


async def test_unreachable_service_fails_fast_with_503(clock: ManualClock) -> None:
    stack = build_devstack(clock=clock)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    from gateway_service.proxy import Proxy

    router_proxy = stack.gateway.app.state.proxy
    assert isinstance(router_proxy, Proxy)
    router_proxy._clients["knowledge"] = httpx.AsyncClient(
        base_url="http://knowledge", transport=httpx.MockTransport(down)
    )
    await stack.start()
    try:
        owner = await signup(stack, "owner@down.io")
        async with client_for(stack, owner["access_token"]) as client:
            response = await client.get("/v1/documents")
            unaffected = await client.get("/v1/billing/subscription")
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
        assert unaffected.status_code == 200  # failure is isolated to one service
    finally:
        await stack.stop()
