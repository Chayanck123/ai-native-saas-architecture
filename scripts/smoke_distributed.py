"""Distributed smoke test: every service as its own process, like in production.

Starts 5 APIs + 5 workers + the Lambda emulator as separate OS processes
(exactly the commands the containers run), wired together only through HTTP,
PostgreSQL and Redis Streams, then drives a customer journey through the
public gateway and checks the results. Needs PostgreSQL (with pgvector) and Redis:

    ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \\
    REDIS_URL=redis://localhost:6379/0 \\
    uv run python scripts/smoke_distributed.py
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlparse

import asyncpg
import httpx

from platform_core.bootstrap import bootstrap

SERVICES = {
    "gateway": 18080,
    "knowledge": 18081,
    "agents": 18082,
    "billing": 18083,
    "growth": 18084,
}
FUNCTIONS_PORT = 18090
STRIPE_SECRET = "whsec_smoke"  # noqa: S105 - test-only value
HANDBOOK = """# Handbook
## Refund policy
Customers can request a full refund within 30 days of purchase. Refunds are paid to the
original payment method within five business days.
"""


def database_urls(admin_url: str) -> dict[str, str]:
    host = urlparse(admin_url.replace("postgresql://", "http://", 1))
    return {
        name: f"postgresql+asyncpg://smoke_{name}:smoke@{host.hostname}:{host.port or 5432}/"
        f"smoke_{name}"
        for name in SERVICES
    }


async def reset_databases(admin_url: str, urls: dict[str, str]) -> None:
    admin = await asyncpg.connect(admin_url)
    try:
        for name in urls:
            await admin.execute(f"DROP DATABASE IF EXISTS smoke_{name} WITH (FORCE)")
    finally:
        await admin.close()


class Cluster:
    def __init__(self, admin_url: str, redis_url: str, log_dir: Path) -> None:
        self.admin_url = admin_url
        self.redis_url = redis_url
        self.log_dir = log_dir
        self.processes: list[tuple[str, asyncio.subprocess.Process]] = []
        self.urls = database_urls(admin_url)
        # One key prefix shared by every process of this run, isolating it from others.
        self.redis_prefix = f"smoke{int(time.time())}"

    def env(self, service: str) -> dict[str, str]:
        env = {
            **os.environ,
            "ENVIRONMENT": "local",
            "LOG_LEVEL": "WARNING",
            "LOG_JSON": "false",
            "REDIS_URL": self.redis_url,
            "REDIS_PREFIX": self.redis_prefix,
            "INTERNAL_JWT_SECRET": "smoke-internal-secret-0123456789abcdef-xyz",
            "JWT_SECRET": "smoke-user-jwt-secret-0123456789abcdef-xyz",
            "STAFF_EMAILS": "staff@smoke.test",
            "DATABASE_URL": self.urls.get(service, ""),
            "AGENTS_LLM_PROVIDER": "offline",
            "STRIPE_WEBHOOK_SECRET": STRIPE_SECRET,
            "PORT": str(FUNCTIONS_PORT),
        }
        for name, port in SERVICES.items():
            env[f"{name.upper()}_URL"] = f"http://127.0.0.1:{port}"
        return env

    def _log_file(self, name: str) -> IO[bytes]:
        # Stays open for the child process's lifetime; the OS closes it on exit.
        return open(self.log_dir / f"{name}.log", "wb")

    async def start(self, name: str, *args: str, service: str) -> None:
        log = self._log_file(name)
        process = await asyncio.create_subprocess_exec(
            *args, env=self.env(service), stdout=log, stderr=asyncio.subprocess.STDOUT
        )
        self.processes.append((name, process))

    async def up(self) -> None:
        await reset_databases(self.admin_url, self.urls)
        await bootstrap(self.admin_url, self.urls)
        for index, (service, port) in enumerate(SERVICES.items()):
            await self.start(
                f"{service}-api", f"{service}-service", "api", "--port", str(port), service=service
            )
            await self.start(
                f"{service}-worker",
                f"{service}-service",
                "worker",
                "--metrics-port",
                str(19100 + index),
                service=service,
            )
        await self.start("functions", "serverless-functions", service="functions")
        async with httpx.AsyncClient() as client:
            for port in [*SERVICES.values(), FUNCTIONS_PORT]:
                await wait_for(partial(_ready, client, port), f"service on port {port}", seconds=60)

    async def down(self) -> None:
        for _, process in self.processes:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        for _, process in self.processes:
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()

    def crashed(self) -> list[str]:
        return [name for name, process in self.processes if process.returncode is not None]


async def _ready(client: httpx.AsyncClient, port: int) -> bool:
    try:
        response = await client.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


async def wait_for(check: Callable[[], Awaitable[Any]], what: str, seconds: float = 30) -> Any:
    """Poll ``check`` until it returns something truthy (the system is eventually consistent)."""
    try:
        async with asyncio.timeout(seconds):
            while True:
                if result := await check():
                    return result
                await asyncio.sleep(0.25)
    except TimeoutError:
        raise TimeoutError(f"timed out waiting for {what}") from None


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ok  {message}")


async def journey() -> None:
    gateway = f"http://127.0.0.1:{SERVICES['gateway']}"
    async with httpx.AsyncClient(base_url=gateway, timeout=30) as anon:
        signup = await anon.post(
            "/v1/auth/signup",
            json={
                "email": "founder@smoke.test",
                "password": "smoke-password-1",
                "workspace_name": "Smoke Co",
            },
        )
        expect(signup.status_code == 201, "self-serve signup through the gateway")
        token = signup.json()["access_token"]
        tenant_id = signup.json()["tenant"]["id"]

    headers = {"authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=gateway, headers=headers, timeout=30) as client:

        async def subscription_ready() -> bool:
            return (await client.get("/v1/billing/subscription")).status_code == 200

        await wait_for(subscription_ready, "billing to provision the tenant (event-driven)")
        expect(True, "tenant.created event provisioned a free subscription in billing")

        key = {"idempotency-key": "smoke-doc-1"}
        doc = await client.post(
            "/v1/documents", json={"title": "Handbook", "content": HANDBOOK}, headers=key
        )
        replay = await client.post(
            "/v1/documents", json={"title": "Handbook", "content": HANDBOOK}, headers=key
        )
        expect(
            doc.status_code == 202 and replay.headers.get("idempotent-replayed") == "true",
            "document upload accepted; retry with the same Idempotency-Key replayed",
        )

        async def indexed() -> bool:
            body = (await client.get(f"/v1/documents/{doc.json()['id']}")).json()
            return bool(body["status"] == "indexed")

        await wait_for(indexed, "document indexing by the knowledge worker")
        expect(True, "knowledge worker chunked + embedded the document (pgvector)")

        run = await client.post(
            "/v1/agents/runs", json={"question": "How many days do I have to get a refund?"}
        )
        expect(run.status_code == 202, "agent run queued")

        async def finished() -> dict[str, Any] | None:
            body: dict[str, Any] = (await client.get(f"/v1/agents/runs/{run.json()['id']}")).json()
            return body if body["status"] not in {"queued", "running"} else None

        result = await wait_for(finished, "the agents worker to finish the run")
        expect(
            result["status"] == "succeeded" and "30 days" in result["answer"],
            f"agent answered with citations: {result['answer'][:70]}...",
        )

        async def metered() -> dict[str, Any] | None:
            usage: dict[str, Any] = (await client.get("/v1/billing/usage")).json()
            return usage if "agent_runs" in usage["meters"] else None

        usage = await wait_for(metered, "usage events to reach billing")
        expect(
            usage["meters"]["agent_runs"]["quantity"] == 1,
            f"usage metered through Redis Streams: {usage['credits']['used']} credits",
        )

        event = {
            "id": "evt_smoke",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_smoke",
                    "customer": "cus_smoke",
                    "status": "active",
                    "metadata": {"tenant_id": tenant_id, "plan": "pro"},
                }
            },
        }
        body = json.dumps(event).encode()
        stamp = str(int(time.time()))
        signature = hmac.new(
            STRIPE_SECRET.encode(), f"{stamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        async with httpx.AsyncClient() as stripe:
            response = await stripe.post(
                f"http://127.0.0.1:{FUNCTIONS_PORT}/webhooks/stripe",
                content=body,
                headers={"stripe-signature": f"t={stamp},v1={signature}"},
            )
        expect(response.status_code == 200, "Stripe webhook verified by the Lambda handler")

        async def upgraded() -> bool:
            me = (await client.get("/v1/me")).json()
            return bool(me["tenant"]["plan"] == "pro")

        await wait_for(upgraded, "the plan change to reach billing and the gateway")
        expect(True, "billing applied the upgrade; the gateway synced the plan (events)")

    async with httpx.AsyncClient(base_url=gateway, timeout=30) as anon:
        staff = await anon.post(
            "/v1/auth/signup",
            json={
                "email": "staff@smoke.test",
                "password": "smoke-password-1",
                "workspace_name": "HQ",
            },
        )
    staff_headers = {"authorization": f"Bearer {staff.json()['access_token']}"}
    async with httpx.AsyncClient(base_url=gateway, headers=staff_headers, timeout=30) as ops:

        async def funnel_complete() -> dict[str, Any] | None:
            data: dict[str, Any] = (await ops.get("/v1/analytics/funnel")).json()
            counts = [s["count"] for s in data["steps"]]
            return data if counts[-1] >= 1 else None

        funnel = await wait_for(funnel_complete, "growth analytics to project the journey")
        expect(
            funnel["steps"][0]["count"] >= 1,
            "growth funnel: " + " -> ".join(f"{s['name']} {s['count']}" for s in funnel["steps"]),
        )
        revenue = (await ops.get("/v1/billing/revenue")).json()
        expect(revenue["arr_cents"] == 12 * 4_900, f"ARR ${revenue['arr_cents'] / 100:,.0f}")
        queues = (await ops.get("/v1/admin/queues")).json()
        expect(all(q["dead"] == 0 for q in queues.values()), "no dead-lettered tasks")


def print_log_tails(log_dir: Path, lines: int = 15) -> None:
    for log in sorted(log_dir.glob("*.log")):
        tail = log.read_text(errors="replace").splitlines()[-lines:]
        if tail:
            print(f"--- {log.name}\n" + "\n".join(tail))


async def main() -> None:
    admin_url = os.environ.get(
        "ADMIN_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
    )
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    log_dir = Path(tempfile.mkdtemp(prefix="smoke-logs-"))
    cluster = Cluster(admin_url, redis_url, log_dir)
    print(f"starting 11 processes (logs in {log_dir})")
    started = time.perf_counter()
    try:
        await cluster.up()
        print(f"cluster ready in {time.perf_counter() - started:.1f}s")
        await journey()
        crashed = cluster.crashed()
        expect(
            not crashed, "all processes still running" + (f": crashed {crashed}" if crashed else "")
        )
    except Exception:
        print_log_tails(log_dir)
        raise
    finally:
        await cluster.down()
    print(f"distributed smoke test passed in {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
